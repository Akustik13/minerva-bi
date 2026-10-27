"""Терміни з архіву пошти: python manage.py test calendar_app --settings=tabele.settings_test (AI підмінено)."""
import datetime
import json
from datetime import timezone as dt_tz
from unittest import mock

from calendar_app import deadlines
from calendar_app.models import CalendarEvent, CalendarSettings, DeadlineScanMark, FoundDeadline
from calendar_app.tests_invites import Base

AI = "email_assistant.ai_helper._call_ai_direct"


def answer(*items):
    return json.dumps(list(items))


class DeadlineTests(Base):
    def mail2(self, body, subject="Lieferung", frm="max@acme.example", day=20):
        self.uid += 1
        from email_assistant.sync import save_message
        return save_message(self.account, {
            "uid": self.uid, "message_id": f"<d{self.uid}@acme.example>", "in_reply_to": "", "references": [],
            "subject": subject, "from_email": frm, "from_name": "Max", "to_emails": ["me@sevskiy.example"],
            "cc_emails": [], "body_text": body, "body_html": "", "attachments": [], "is_read": False,
            "sent_at": datetime.datetime(2026, 10, day, 9, 0, tzinfo=dt_tz.utc)}, "INBOX")

    def test_mask_prefilter_extract_and_rescan(self):
        m = self.mail2("Bitte liefern Sie bis 30.10.2026 die Antennen.\nDanke")
        self.mail2("Danke für das Gespräch, schöne Grüße")                 # без дати — без AI
        self.mail2("Termin am 05.11.", frm="other@beta.example")            # не за маскою
        total, cands = deadlines.candidates(self.user, {"sender": "acme.example"})
        self.assertEqual((total, [c.pk for c in cands]), (2, [m.pk]))
        reply = answer({"title": "Lieferung Antennen", "date": "2026-10-30", "time": "",
                        "quote": "Bitte liefern Sie bis 30.10.2026 die Antennen."},
                       {"title": "Вигадане", "date": "2026-11-11", "time": "10:00", "quote": "цього немає в листі"},
                       {"title": "Далеко", "date": "2031-01-01", "time": "", "quote": "Bitte liefern"})
        with mock.patch(AI, return_value=reply) as ai:
            seen, found = deadlines.scan(self.user, {"sender": "acme.example"})
        self.assertEqual((seen, found, ai.call_count), (1, 1, 1))
        d = FoundDeadline.objects.get()
        self.assertEqual((d.title, d.all_day, d.due_at.date()), ("Lieferung Antennen", True, datetime.date(2026, 10, 30)))
        self.assertIn("2026-10-20", ai.call_args.args[0])   # дата листа для відносних термінів
        with mock.patch(AI, return_value=reply) as ai:     # повторно — лист уже перевірено
            self.assertEqual(deadlines.scan(self.user, {"sender": "acme.example"}), (0, 0))
        ai.assert_not_called()
        self.assertTrue(DeadlineScanMark.objects.filter(email_message=m, found=1).exists())
        ev = deadlines.accept(d)
        self.assertEqual((ev.event_type, ev.email_message), (CalendarEvent.TYPE_DEADLINE, m))
        self.assertIn("Bitte liefern", ev.description)

    def test_auto_mode_and_page(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(self.user)
        cfg = CalendarSettings.for_user(self.user)
        cfg.deadlines_auto = True
        cfg.save()
        self.mail2("Zahlung fällig bis 15.11.2026, bitte überweisen.")
        r = self.client.get("/calendar/deadlines/", {"count": "1", "date_from": "2026-10-01"})
        self.assertContains(r, "За маскою листів")
        reply = answer({"title": "Zahlung", "date": "2026-11-15", "time": "12:00",
                        "quote": "Zahlung fällig bis 15.11.2026"})
        with mock.patch(AI, return_value=reply), \
                mock.patch("calendar_app.deadlines.start_scan",
                           side_effect=lambda user, mask, limit: deadlines.scan(user, mask, limit)):
            self.client.post("/calendar/deadlines/", {"action": "start", "date_from": "2026-10-01", "limit": "50"})
        d = FoundDeadline.objects.get()
        self.assertEqual(d.status, FoundDeadline.ADDED)   # без підтвердження
        self.assertFalse(d.all_day)
        self.assertEqual(CalendarSettings.for_user(self.user).deadline_mask, {"date_from": "2026-10-01"})
        cfg.deadlines_auto = False
        cfg.save()
        m = self.mail2("Antwort bis 01.12.2026 erbeten.", day=21)
        with mock.patch(AI, return_value=answer({"title": "Antwort", "date": "2026-12-01", "time": "",
                                                 "quote": "Antwort bis 01.12.2026 erbeten."})):
            deadlines.scan(self.user, {})
        nd = FoundDeadline.objects.get(email_message=m)
        r = self.client.get("/calendar/?year=2026&month=12")
        self.assertContains(r, "Терміни з пошти на перевірку: 1")
        self.client.post("/calendar/deadlines/", {"action": "dismiss", "ids": [nd.pk]})
        nd.refresh_from_db()
        self.assertEqual(nd.status, FoundDeadline.DISMISSED)
