"""Шкала й «потребує уваги»: терміни й зустрічі з листів (минула зустріч — відбулась, термін з нашою відповіддю —
ймовірно виконано, однакові — один раз) і файли з листів на шкалі (будь-які, крім картинок у тексті й .ics;
чужі скриньки — лише кількість).
python manage.py test projects --settings=tabele.settings_test"""
from datetime import timedelta

from django.utils import timezone

from calendar_app.models import CalendarEvent
from email_assistant.models import EmailMessage
from projects import overview, terms, timeline
from projects.tests import ME
from projects.tests_ai import AIBase


class TermsBase(AIBase):
    def event(self, title, days, kind="deadline", message=None, done=False, user=None):
        return CalendarEvent.objects.create(user=user or self.user, title=title, event_type=kind,
                                            start_at=timezone.now() + timedelta(days=days),
                                            email_message=message or self.ask, is_done=done)

    def terms(self):
        return {t.event.title: t for t in overview.collect(self.project)["terms"]}

    def attention(self):
        return [a["text"] for a in overview.collect(self.project)["attention"]]


class TermsTests(TermsBase):
    def test_past_meetings_are_held_not_overdue(self):
        self.event("Telko щодо серійного виробництва 2500 шт", -3)               # тип «дедлайн», але це дзвінок
        self.event("Телефонна розмова щодо замовлення", -2)
        self.event("Besprechung Muster", -1, kind="meeting")
        t = self.terms()
        self.assertTrue(all(x.meeting and x.state == terms.HELD for x in t.values()))
        self.assertFalse([a for a in self.attention() if a.startswith("Прострочено")])

    def test_person_decides_the_kind_and_video_is_not_a_meeting(self):
        from django.urls import reverse
        telko = self.event("Telko Serie 2500 Stk", -3)                           # за назвою — зустріч
        self.event("Produktvideo senden", -2)                                    # «video» — ще не дзвінок
        t = self.terms()
        self.assertEqual((t["Telko Serie 2500 Stk"].state, t["Produktvideo senden"].state),
                         (terms.HELD, terms.OVERDUE))
        r = self.client.post(reverse("calendar_app:event_set_type", args=[telko.pk]),
                             '{"event_type": "deadline"}', content_type="application/json")
        self.assertEqual(r.status_code, 200)                                     # «⏰ Це термін, не зустріч»
        self.assertEqual(self.terms()["Telko Serie 2500 Stk"].state, terms.OVERDUE)
        self.client.post(reverse("calendar_app:event_set_type", args=[telko.pk]), '{"event_type": "meeting"}',
                         content_type="application/json")
        self.assertEqual(self.terms()["Telko Serie 2500 Stk"].state, terms.HELD)

    def test_deadline_with_our_reply_is_probably_met(self):
        self.mail("Re: Anfrage 5G Antenne", from_email=ME, to=("buyer@client.example",), sent=self.ago(1),
                  in_reply_to=self.reply.message_id, references=(self.ask.message_id, self.reply.message_id),
                  body="Liefertermine anbei.")
        self.event("Дедлайн: надати терміни поставки", -2, message=self.reply)
        t = self.terms()["Дедлайн: надати терміни поставки"]
        self.assertEqual(t.state, terms.MET)
        self.assertIn("ми відповіли", t.note)
        self.assertFalse([a for a in self.attention() if "терміни поставки" in a])

    def test_early_acknowledgement_is_not_fulfilment(self):
        self.mail("Re: Anfrage 5G Antenne", from_email=ME, to=("buyer@client.example",), sent=self.ago(9, hour=23),
                  in_reply_to=self.ask.message_id, references=(self.ask.message_id,), body="Danke, wir melden uns.")
        self.event("Angebot senden", -1, message=self.ask)                  # «дякуємо» за 8+ днів до терміну
        self.assertEqual(self.terms()["Angebot senden"].state, terms.OVERDUE)

    def test_later_mail_from_them_asks_to_check_and_silence_is_overdue(self):
        self.event("Доставка модулів Cavli", -7, message=self.ask)      # після нього — лист клієнта 5 днів тому
        self.event("Сплатити рахунок", -1, message=self.reply)          # після нього листів немає
        t = self.terms()
        self.assertEqual(t["Доставка модулів Cavli"].state, terms.MOVED)
        self.assertEqual(t["Сплатити рахунок"].state, terms.OVERDUE)
        texts = self.attention()
        self.assertTrue(any(a.startswith("Перевірте термін: Доставка модулів Cavli") for a in texts))
        self.assertTrue(any(a.startswith("Прострочено: Сплатити рахунок") for a in texts))

    def test_done_soon_and_duplicates(self):
        self.event("Muster senden", -4, done=True)
        self.event("Termin Musterfreigabe", 2)
        self.event("Termin Musterfreigabe", 2, message=self.reply)
        t = self.terms()
        self.assertEqual(t["Muster senden"].state, terms.DONE)
        self.assertEqual((t["Termin Musterfreigabe"].state, t["Termin Musterfreigabe"].count), (terms.SOON, 2))
        self.assertEqual(sum(1 for a in self.attention() if "Termin Musterfreigabe" in a), 1)

    def test_timeline_diamond_carries_calendar_actions_only_for_the_owner(self):
        e = self.event("Telko Serie", -3)
        svg = timeline.render(self.project, overview.collect(self.project), self.user)
        self.assertIn(f"&quot;ids&quot;: [{e.pk}]", svg)
        self.assertIn("відбулась", svg)
        olga, _ = self.colleague()
        self.assertNotIn("Telko Serie", timeline.render(self.project, overview.collect(self.project), olga))


class LaneLayoutTests(TermsBase):
    def test_marker_without_room_for_a_label_gets_its_own_row(self):
        from datetime import date
        day = date(2026, 11, 3)
        items = [{"shape": "diamond", "start": day + timedelta(days=i), "end": None, "label": f"🤝 Langer Termin {i} " * 3,
                  "tip": "", "url": "", "cls": "", "lane": "terms"} for i in range(4)]
        x = lambda d: 150 + (d - day).days * 6                                  # noqa: E731 — 6 px на день
        height, parts = timeline._draw_lane(items, 0, x, x, lambda v: v)
        ys = {round(float(p.split('points="')[1].split()[0].split(",")[1])) for p in parts}
        self.assertEqual(len(ys), 4)                                            # 3 рядки з підписами + 1 лише позначка
        self.assertNotIn('class="pjt-t">🤝 Langer Termin 3', "".join(parts))   # текст — лише в підказці
        self.assertEqual(height, timeline.LANE_PAD * 2 + 4 * timeline.ROW_H)


class FileLaneTests(TermsBase):
    def files(self, message, items):
        EmailMessage.objects.filter(pk=message.pk).update(attachments=items)

    def test_sent_zip_and_received_files_are_on_the_timeline(self):
        sent = self.mail("Re: Anfrage 5G Antenne", from_email=ME, to=("buyer@client.example",), sent=self.ago(2),
                         in_reply_to=self.reply.message_id, references=(self.ask.message_id,))
        self.files(sent, [{"name": "Gerber_v3.zip", "content_type": "application/zip", "size": 2400000},
                          {"name": "logo.png", "content_type": "image/png", "size": 4000, "is_inline": True}])
        self.files(self.reply, [{"name": "Spec.pdf", "content_type": "application/pdf", "size": 120000},
                                {"name": "invite.ics", "content_type": "text/calendar", "size": 900}])
        evs = [e for e in timeline.events(self.project, overview.collect(self.project), self.user)
               if e["lane"] == "files"]
        by = {e["cls"]: e for e in evs}
        self.assertEqual(set(by), {"in", "out"})
        self.assertEqual(by["out"]["label"], "📎 Gerber_v3.zip")
        self.assertEqual(by["out"]["url"], f"/email/message/{sent.pk}/attachment/0/")
        self.assertIn("2,3 МБ", by["out"]["tip"])
        self.assertEqual(by["in"]["label"], "📎 Spec.pdf")                     # без .ics
        svg = timeline.render(self.project, overview.collect(self.project), self.user)
        self.assertIn("📎 Файли з листів", svg)

    def test_other_mailbox_shows_only_a_count_and_saved_files_are_not_repeated(self):
        self.files(self.reply, [{"name": "Preisliste.xlsx", "content_type": "application/vnd.ms-excel", "size": 10},
                                {"name": "Spec.pdf", "content_type": "application/pdf", "size": 10,
                                 "saved": [f"project:{self.project.pk}"]}])
        olga, _ = self.colleague()
        evs = [e for e in timeline.events(self.project, overview.collect(self.project), olga) if e["lane"] == "files"]
        self.assertEqual([(e["label"], e["url"]) for e in evs], [("📎 1", "")])
        self.assertNotIn("Preisliste", evs[0]["tip"])
        mine = [e for e in timeline.events(self.project, overview.collect(self.project), self.user)
                if e["lane"] == "files"]
        self.assertEqual(mine[0]["label"], "📎 Preisliste.xlsx")

    def test_many_files_of_a_day_open_a_menu(self):
        self.files(self.reply, [{"name": f"Datei{i}.zip", "content_type": "application/zip", "size": 10}
                                for i in range(3)])
        e = [x for x in timeline.events(self.project, overview.collect(self.project), self.user)
             if x["lane"] == "files"][0]
        self.assertEqual((e["label"], e["url"], len(e["files"])), ("📎 Datei0.zip +2", "", 3))
        self.assertIn("data-files=", timeline.render(self.project, overview.collect(self.project), self.user))


class EventLinkTests(TermsBase):
    def test_link_own_calendar_event_and_unlink(self):
        from django.urls import reverse
        own = CalendarEvent.objects.create(user=self.user, title="Werksbesuch Shenzhen", event_type="meeting",
                                           start_at=timezone.now() + timedelta(days=5))
        mailed = self.event("Muster senden", 3)                               # уже в проєкті через лист
        r = self.client.get(reverse("projects:project_events", args=[self.project.pk]))
        ids = [e["id"] for e in r.json()["events"]]
        self.assertIn(own.pk, ids)
        self.assertNotIn(mailed.pk, ids)
        r = self.post(reverse("projects:project_event_link", args=[self.project.pk]), {"event": own.pk})
        self.assertIn("📅 Подію додано до проєкту: Werksbesuch Shenzhen", r.json()["html"])
        self.assertIn("Werksbesuch Shenzhen", self.terms())
        svg = timeline.render(self.project, overview.collect(self.project), self.user)
        self.assertIn("&quot;linked&quot;: true", svg)
        self.assertNotIn(own.pk, [e["id"] for e in self.client.get(
            reverse("projects:project_events", args=[self.project.pk])).json()["events"]])
        self.post(reverse("projects:project_event_link", args=[self.project.pk]), {"event": own.pk, "link": False})
        self.assertNotIn("Werksbesuch Shenzhen", self.terms())

    def test_someone_elses_event_cannot_be_linked(self):
        from django.urls import reverse
        olga, _ = self.colleague()
        theirs = CalendarEvent.objects.create(user=olga, title="Privat", event_type="meeting",
                                              start_at=timezone.now() + timedelta(days=1))
        r = self.post(reverse("projects:project_event_link", args=[self.project.pk]), {"event": theirs.pk})
        self.assertEqual(r.status_code, 400)
