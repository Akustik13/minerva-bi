"""🎙 Повідомлення й тексти згоди: python manage.py test meetings --settings=tabele.settings_test

Повідомлення — лише дата, код проєкту, кількість пунктів (без назви, підсумку, цитат), один раз; тексти для читання
вголос — з meetings/consent/*.txt (без ремарок у дужках), примітка в запрошення — з числами строків наради."""
from unittest.mock import patch

from django.test import TestCase

from meetings import consent, notify, services
from meetings.models import Meeting, MeetingItem, MeetingSettings
from meetings.tests_services import ITEMS, MeetingBase


class NotifyTests(MeetingBase):
    def test_texts_pluralised(self):
        m, token = self.processed()
        self.moments(m, token, ITEMS[:1])
        m.refresh_from_db()
        msg = notify.compose(m)
        day = f"{m.day:%d.%m.%Y}"
        self.assertEqual(msg["head"], f"🎙 Нарада від {day} ({self.project.code}) розшифрована — 1 пункт чекає "
                                      f"перевірки (✓ / ✕).")
        self.assertEqual((msg["subject"], msg["summary"], msg["lines"]), ("🎙 Розшифровка наради готова", "", []))
        self.assertTrue(msg["link"].endswith(f"/{m.pk}/page/"))
        for n, words in ((2, "2 пункти чекають"), (5, "5 пунктів чекають"), (21, "21 пункт чекає"),
                         (12, "12 пунктів чекають")):
            MeetingItem.objects.filter(meeting=m).delete()
            for i in range(n):
                MeetingItem.objects.create(meeting=m, key=f"k{i}", title="t")
            self.assertIn(words, notify.compose(m)["head"])
        MeetingItem.objects.filter(meeting=m).delete()
        self.assertIn("ключових моментів не знайдено", notify.compose(m)["head"])
        Meeting.objects.filter(pk=m.pk).update(project=None, moments_error=services.MOMENTS_FAILED_TEXT)
        m.refresh_from_db()
        self.assertEqual(notify.compose(m)["head"],
                         f"🎙 Нарада від {day} розшифрована, але ключові моменти знайти не вдалося.")
        for text in (str(notify.compose(m)), str(msg)):
            self.assertNotIn("Abstimmung", text)
            self.assertNotIn("Freitag", text)
            self.assertNotIn("Lieferung", text)

    def test_once_and_channels(self):
        m, token = self.processed()
        MeetingSettings.objects.filter(pk=1).update(notify_telegram=False)
        Meeting.objects.filter(pk=m.pk).update(status=Meeting.REVIEW)
        self.assertEqual(notify.ready(m.pk), "🔔 Повідомлено: пошта")
        self.assertEqual(notify.ready(m.pk), "")
        self.assertEqual(self.delivered.call_count, 1)
        args, kwargs = self.delivered.call_args
        self.assertEqual((args[0], args[2], kwargs), (self.owner, "Відкрити нараду", {"mail": True,
                                                                                    "telegram": False}))

    def test_not_final_or_owner_inactive(self):
        m, _ = self.recording()
        self.assertEqual(notify.ready(m.pk), "")                    # ще не кінцевий стан
        Meeting.objects.filter(pk=m.pk).update(status=Meeting.FAILED)
        self.owner.is_active = False
        self.owner.save()
        self.assertEqual(notify.ready(m.pk), "")
        self.assertEqual(self.delivered.call_count, 0)

    def test_background_thread(self):
        m, _ = self.processed()
        Meeting.objects.filter(pk=m.pk).update(status=Meeting.FAILED)
        notify.BACKGROUND = True
        started = []
        with patch("meetings.notify.threading.Thread") as thread:
            thread.return_value.start.side_effect = lambda: started.append(True)
            with self.captureOnCommitCallbacks(execute=True):
                notify.schedule(m.pk)
        self.assertEqual(started, [True])
        self.assertTrue(thread.call_args.kwargs["daemon"])


class ConsentTests(TestCase):
    def test_builtin_texts_without_stage_directions(self):
        for lang in consent.LANGS:
            parts = consent.parts_for(lang)
            self.assertTrue(parts["ask"] and parts["after"])
            self.assertNotIn("[", parts["ask"] + parts["after"])
        self.assertIn("einverstanden", consent.parts_for("de")["ask"])
        self.assertEqual(consent.parts_for("de")["after"], "Danke, die Aufnahme läuft jetzt.")
        self.assertEqual(consent.parts_for("xx"), consent.parts_for("de"))
        self.assertTrue(consent.text_for("uk").endswith("Дякую, запис почався."))

    def test_settings_override(self):
        s = MeetingSettings.load()
        s.consent_text_en = "Is everyone fine with recording?\n---\nRecording started."
        s.consent_text_uk = "Усі згодні на запис?"
        s.save()
        self.assertEqual(consent.parts_for("en"), {"ask": "Is everyone fine with recording?",
                                                   "after": "Recording started."})
        self.assertEqual(consent.parts_for("uk")["ask"], "Усі згодні на запис?")
        self.assertEqual(consent.parts_for("uk")["after"], "Дякую, запис почався.")
        self.assertEqual(set(consent.texts()), {"de", "en", "uk"})

    def test_invitation_note_numbers_and_privacy_link(self):
        note = consent.invitation_note("de", audio_days=30, transcript_days=365, privacy_url="")
        self.assertIn("nach spätestens 30 Tagen gelöscht, die Mitschrift nach 12 Monaten.", note)
        self.assertNotIn("Art. 13", note)
        self.assertNotIn("{", note)
        note = consent.invitation_note("uk", audio_days=1, transcript_days=730,
                                       privacy_url="https://example.com/datenschutz")
        self.assertIn("через 1 день, розшифровка — через 24 місяці", note)
        self.assertTrue(note.endswith("Інформація за ст. 13 DSGVO: https://example.com/datenschutz"))
        self.assertEqual(consent.duration_text(45, "en"), "45 days")
        self.assertEqual(consent.duration_text(30, "de"), "einem Monat")
        s = MeetingSettings.load()
        s.privacy_url = "https://example.com/p"
        s.save()
        notes = consent.invitation_notes(Meeting(retention_days=400, audio_days=7))
        self.assertIn("after 7 days at the latest, the transcript after 400 days", notes["en"])
        self.assertIn("https://example.com/p", notes["en"])
