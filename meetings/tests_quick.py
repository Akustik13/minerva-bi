"""⚡ Швидкий запис наради (meetings/services.quick_start, quick_events; admin_pages.quick_context; meetings/admin.py
quick_view і блок угорі списку нарад): python manage.py test meetings.tests_quick --settings=tabele.settings_test

POST quick/ — нарада «Готово до запису» й одразу 302 на вікно запису з #minerva=<квиток>; зустріч календаря — її
підготовлена нарада, учасники лише відмічені; повтор за 2 хв — та сама нарада; відмова, поки записування вимкнено
або без права; чужа подія — помилка; ?from=bench — форми без нової вкладки; зустрічі дня без скасованих / відхилених /
виконаних / на весь день / чужих.
"""
from datetime import timedelta
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

from calendar_app.models import CalendarEvent
from meetings import admin_pages, services
from meetings.models import Meeting, MeetingSettings
from meetings.tests_pages import PagesBase, staff

QUICK = "admin:meetings_meeting_quick"
BENCH = "https://192.168.2.68:8791/#minerva="


class QuickBase(PagesBase):
    def event(self, user=None, title="Weekly mit Client GmbH", start=None, **extra):
        return CalendarEvent.objects.create(user=user or self.owner, title=title,
                                            start_at=start or timezone.now() - timedelta(minutes=1),
                                            event_type=extra.pop("event_type", CalendarEvent.TYPE_MEETING), **extra)

    def post(self, **data):
        self.login(self.owner)
        return self.client.post(reverse(QUICK), data)

    def ticket_of(self, response):
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(BENCH), response["Location"])
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        return response["Location"].split("#minerva=", 1)[1]


class QuickStartTests(QuickBase):
    def test_url_has_no_layer4_suffix(self):
        self.assertEqual(reverse(QUICK), "/admin/meetings/meeting/quick/")

    def test_post_creates_ready_meeting_and_opens_bench(self):
        r = self.post()
        m = Meeting.objects.get(owner=self.owner)
        self.assertEqual(m.status, Meeting.READY)
        self.assertTrue(m.title.startswith(f"Нарада {timezone.localdate():%d.%m.%Y} "), m.title)   # з часом
        self.assertEqual((m.project, m.customer, m.participants), (None, None, []))
        self.assertEqual(services.redeem(self.ticket_of(r))["meeting"]["id"], m.pk)

    def test_title_and_project(self):
        r = self.post(title="  Abstimmung   Antenne ", project=self.project.pk)
        self.ticket_of(r)
        m = Meeting.objects.get(owner=self.owner)
        self.assertEqual((m.title, m.project, m.customer), ("Abstimmung Antenne", self.project, self.customer))
        m.delete()
        self.post(project=self.project.pk)
        self.assertEqual(Meeting.objects.get(owner=self.owner).title,
                         f"Нарада {self.project.code} · {timezone.localdate():%d.%m.%Y}")

    def test_repeat_within_two_minutes_is_the_same_meeting(self):
        first = self.ticket_of(self.post(title="Doppelklick"))
        second = self.ticket_of(self.post(title="Doppelklick"))
        self.assertEqual(Meeting.objects.filter(owner=self.owner).count(), 1)
        self.assertNotEqual(first, second)                         # новий квиток — попередній уже не діє
        with self.assertRaises(services.TicketError):
            services.redeem(first)
        Meeting.objects.filter(owner=self.owner).update(created_at=timezone.now() - timedelta(minutes=3))
        self.ticket_of(self.post(title="Doppelklick"))
        self.assertEqual(Meeting.objects.filter(owner=self.owner).count(), 2)

    def test_event_uses_its_prepared_meeting_and_ticked_people(self):
        ev = self.event()
        people = [{"name": "Org", "email": "org@client.example", "source": "invite", "checked": True},
                  {"name": "Maybe", "email": "maybe@client.example", "source": "invite", "checked": False}]
        with patch("meetings.services._invite_people", return_value=people):
            self.ticket_of(self.post(event=ev.pk))
        m = Meeting.objects.get(owner=self.owner)
        self.assertEqual((m.title, m.calendar_event), ("Weekly mit Client GmbH", ev))
        self.assertEqual([p["email"] for p in m.participants], ["org@client.example"])
        Meeting.objects.filter(pk=m.pk).update(created_at=timezone.now() - timedelta(hours=1))
        self.ticket_of(self.post(event=ev.pk))                     # підготовлена нарада зустрічі — та сама
        self.assertEqual(Meeting.objects.filter(owner=self.owner).count(), 1)
        Meeting.objects.filter(pk=m.pk).update(status=Meeting.REVIEW)
        self.ticket_of(self.post(event=ev.pk))                     # уже записана — нова нарада
        self.assertEqual(Meeting.objects.filter(owner=self.owner, calendar_event=ev).count(), 2)

    def test_someone_elses_event_is_refused(self):
        ev = self.event(user=self.other)
        r = self.post(event=ev.pk)
        self.assertRedirects(r, reverse(QUICK), fetch_redirect_response=False)
        self.assertFalse(Meeting.objects.exists())
        page = self.client.get(reverse(QUICK))
        self.assertContains(page, "Подію календаря не знайдено")

    def test_refused_when_disabled_or_without_right(self):
        MeetingSettings.objects.filter(pk=1).update(enabled=False)
        r = self.post()
        self.assertRedirects(r, reverse(QUICK), fetch_redirect_response=False)
        self.assertFalse(Meeting.objects.exists())
        page = self.client.get(reverse(QUICK))
        self.assertContains(page, services.DISABLED_TEXT)
        self.assertNotContains(page, "⚡ Записати зараз</button>")
        MeetingSettings.objects.filter(pk=1).update(enabled=True)
        viewer = staff("viewer", module_operations={"meetings": ["view"]})
        self.login(viewer)
        r = self.client.post(reverse(QUICK) + "?from=bench", {"from": "bench"})
        self.assertRedirects(r, reverse(QUICK) + "?from=bench", fetch_redirect_response=False)
        self.assertFalse(Meeting.objects.exists())
        self.assertContains(self.client.get(reverse(QUICK)), services.NO_RIGHT_TEXT)
        self.assertNotContains(self.client.get(reverse("admin:meetings_meeting_changelist")), 'id="mt-quick"')

    def test_project_needs_right(self):
        manager = staff("mgr", module_operations={"meetings": ["view", "add", "change"]})
        self.login(manager)
        with patch.object(type(manager), "has_perm", lambda u, perm, obj=None: perm != "projects.view_project"):
            r = self.client.post(reverse(QUICK), {"project": self.project.pk})
        self.assertRedirects(r, reverse(QUICK), fetch_redirect_response=False)
        self.assertFalse(Meeting.objects.exists())


class QuickPageTests(QuickBase):
    def test_page_and_list_card(self):
        ready = self.meeting(title="Vorbereitet")
        ev = self.event(title="Kickoff Antenne")
        self.login(self.owner)
        for url in (reverse(QUICK), reverse("admin:meetings_meeting_changelist")):
            r = self.client.get(url)
            self.assertContains(r, 'id="mt-quick"')
            self.assertContains(r, "⚡ Записати зараз")
            self.assertContains(r, 'target="_blank"')
            self.assertContains(r, "data-mt-reload-after")
            self.assertContains(r, "Kickoff Antenne")
            self.assertContains(r, f'name="event" value="{ev.pk}"')
            self.assertContains(r, reverse("admin:meetings_meeting_record", args=[ready.pk]))
            self.assertContains(r, "meetings/meetings.js?v=4")
            self.assertContains(r, "RAG-ПК ще не підключено до Minerva")    # ще ні разу не був на зв'язку
            self.assertContains(r, "disabled")

    def test_from_bench_opens_in_the_same_tab(self):
        self.login(self.owner)
        self.meeting(title="Vorbereitet")
        self.event(title="Kickoff")
        r = self.client.get(reverse(QUICK) + "?from=bench")
        card = r.content.decode().split('id="mt-quick"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(card.count('<input type="hidden" name="from" value="bench">'), 2)   # зараз і зустріч
        self.assertEqual(card.count("<form"), 3)
        self.assertNotIn('target="_blank"', card)
        self.assertNotIn("data-mt-reload-after", card)
        self.assertIn("autofocus", card)

    def test_event_with_finished_meeting_shows_state_not_button(self):
        ev = self.event(title="Erledigt")
        m = self.meeting(title="Erledigt", calendar_event=ev.pk)
        Meeting.objects.filter(pk=m.pk).update(status=Meeting.REVIEW)
        ctx = admin_pages.quick_context(self.owner)
        row = ctx["events"][0]
        self.assertEqual((row["state"], row["page"]), ("✅ записано", services.page_path(m)))
        self.assertEqual(ctx["prepared"], [])
        self.login(self.owner)
        self.assertNotContains(self.client.get(reverse(QUICK)), f'name="event" value="{ev.pk}"')

    def test_prepared_meeting_of_an_event_listed_once(self):
        ev = self.event()
        m = self.meeting(title="Weekly", calendar_event=ev.pk)
        ctx = admin_pages.quick_context(self.owner)
        self.assertEqual(ctx["events"][0]["meeting"], m)
        self.assertEqual(ctx["prepared"], [])


class QuickEventsTests(QuickBase):
    def test_only_own_meetings_of_today(self):
        now = timezone.now()
        keep = self.event(title="Jetzt", start=now - timedelta(hours=1))
        self.event(title="✖ Скасовано: Alt")
        self.event(title="✕ Відхилено: Nein")
        self.event(title="Erledigt", is_done=True)
        self.event(title="Ganztägig", all_day=True)
        self.event(title="Frist", event_type=CalendarEvent.TYPE_DEADLINE)
        self.event(title="Gestern", start=now - timedelta(hours=5))
        self.event(title="Morgen", start=now + timedelta(days=1, hours=1))
        self.event(user=self.other, title="Fremd")
        titles = [ev.title for ev in services.quick_events(self.owner, now)]
        self.assertIn(keep.title, titles)
        self.assertNotIn("Fremd", titles)
        for title in ("✖ Скасовано: Alt", "✕ Відхилено: Nein", "Erledigt", "Ganztägig", "Frist", "Gestern", "Morgen"):
            self.assertNotIn(title, titles)

    def test_quick_title_with_time(self):
        now = timezone.make_aware(timezone.datetime(2026, 11, 16, 14, 5))
        self.assertEqual(services.quick_title(now=now), "Нарада 16.11.2026 14:05")
