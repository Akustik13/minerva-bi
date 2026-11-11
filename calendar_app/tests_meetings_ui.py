"""🎙 Наради в календарі: «🎙 Записати нараду» / «🎙 Відкрити нараду» у вікні події й у рядках списку — лише для своїх
зустрічей з часом і лише тому, хто може записувати; у подію з наради нічого не пишеться.

    python manage.py test calendar_app.tests_meetings_ui --settings=tabele.settings_test
"""
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from calendar_app.models import CalendarEvent
from meetings.models import Meeting, MeetingSettings


def enable_recording(on=True):
    s = MeetingSettings.load()
    s.enabled = on
    s.texts_reviewed_at = timezone.now() if on else None
    s.save()


class CalendarMeetingTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        enable_recording()
        User = get_user_model()
        self.user = User.objects.create_user("anna", "a@x.y", "x", is_staff=True, is_superuser=True)
        self.bob = User.objects.create_user("bob", "b@x.y", "x", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)
        self.start = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)

    def event(self, title="Telko Muster", event_type="meeting", user=None, **kw):
        return CalendarEvent.objects.create(user=user or self.user, title=title, event_type=event_type,
                                            start_at=self.start, **kw)

    def detail(self, ev):
        r = self.client.get(f"/calendar/event/{ev.pk}/")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_record_url_for_own_timed_meeting(self):
        ev = self.event()
        d = self.detail(ev)
        self.assertEqual(d["record_url"], f"/admin/meetings/meeting/new/?event={ev.pk}")
        self.assertEqual(d["meeting_url"], "")
        for other in (self.event("Muster senden", event_type="deadline"), self.event(all_day=True)):
            self.assertEqual(self.detail(other)["record_url"], "", other.title)    # термін / весь день — ні
        call = self.event("Call mit Kunde", event_type="other")                    # назва каже «зустріч» — так
        self.assertTrue(self.detail(call)["record_url"])
        enable_recording(False)                                                    # записування не ввімкнено
        self.assertEqual(self.detail(ev)["record_url"], "")
        self.assertEqual(self.client.get(f"/calendar/event/{self.event(user=self.bob).pk}/").status_code, 404)

    def test_meeting_url_newest_own_meeting_only(self):
        ev = self.event()
        Meeting.objects.create(owner=self.user, title="Erste Runde", calendar_event=ev)
        newest = Meeting.objects.create(owner=self.user, title="Zweite Runde", calendar_event=ev)
        Meeting.objects.create(owner=self.bob, title="Bobs Runde", calendar_event=ev)  # чужа — ніколи
        d = self.detail(ev)
        self.assertEqual(d["meeting_url"], f"/admin/meetings/meeting/{newest.pk}/page/")
        self.assertTrue(d["record_url"])                                          # «записати ще одну»
        ev.refresh_from_db()
        self.assertEqual((ev.title, ev.description), ("Telko Muster", ""))        # у подію з наради нічого

    def test_list_rows_and_popup_markup(self):
        ev = self.event()
        self.event("Muster senden", event_type="deadline")
        html = self.client.get(f"/calendar/?year={self.start.year}&month={self.start.month}").content.decode()
        self.assertIn(f'href="/admin/meetings/meeting/new/?event={ev.pk}"', html)
        self.assertEqual(html.count('class="cal-admin-link cal-mt"'), 1)
        self.assertIn('id="cal-popup-rec"', html)
        self.assertIn("записати ще одну", html)
        m = Meeting.objects.create(owner=self.user, title="Runde", calendar_event=ev)
        html = self.client.get(f"/calendar/?year={self.start.year}&month={self.start.month}").content.decode()
        self.assertIn(f'href="/admin/meetings/meeting/{m.pk}/page/"', html)
