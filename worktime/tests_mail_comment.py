"""Опис часу на пошту для askDANTE (мова, теми, AI, свій текст) і ♻ перезапис:
python manage.py test worktime --settings=tabele.settings_test"""
import datetime
from unittest import mock

from django.urls import reverse

from email_assistant.models import EmailMessage
from worktime import mail_time
from worktime.models import Booking, MailTimeEntry, TimeRecord
from worktime.tests_mail_time import MailTimeBase, MON


class CommentTests(MailTimeBase):
    def row(self):
        return mail_time.estimate(self.du, MON, MON)[MON]["rows"][0]

    def test_topics_without_reply_prefixes_and_languages(self):
        self.msg(subject="AW: Neue Testboards", folder=EmailMessage.FOLDER_SENT)
        self.msg(subject="Neue Testboards")
        self.msg(subject="WG: Fehler im LTE Board")
        r = self.row()
        self.assertEqual(r["topics"], ["Neue Testboards", "Fehler im LTE Board"])
        self.assertEqual(mail_time.default_comment(r, "de"),
                         "E-Mails DigiKey: Neue Testboards; Fehler im LTE Board (1 Antwort, 2 E-Mails)")
        self.assertEqual(mail_time.default_comment(r, "en"),
                         "Emails DigiKey: Neue Testboards; Fehler im LTE Board (1 reply, 2 emails)")
        long = mail_time.default_comment({**r, "topics": ["x" * 400]}, "de")
        self.assertLessEqual(len(long), mail_time.COMMENT_MAX)
        self.assertTrue(long.endswith("(1 Antwort, 2 E-Mails)"))

    def test_ai_summary_cached_and_used_when_booking(self):
        self.msg(subject="Neue Testboards", folder=EmailMessage.FOLDER_SENT)
        with mock.patch("email_assistant.ai_helper._call_ai_direct",
                        return_value="Abstimmung Liefertermin der neuen Testboards.\n") as ai:
            self.assertEqual(mail_time.describe(self.du, self.row(), "de"), "Abstimmung Liefertermin der neuen Testboards.")
            mail_time.describe(self.du, self.row(), "de")
        self.assertEqual(ai.call_count, 1)                       # другий раз — з кешу
        self.assertIn("German", ai.call_args.args[0])
        self.assertEqual(ai.call_args.kwargs["model"], mail_time.AI_MODEL)
        mail_time.book(self.du, MON, self.dp.pk)
        self.assertEqual(self.fake.calls[-1]["projectData"]["comment"],
                         "E-Mails DigiKey: Abstimmung Liefertermin der neuen Testboards. (1 Antwort)")

    def test_own_text_wins(self):
        self.msg(folder=EmailMessage.FOLDER_SENT)
        mail_time.book(self.du, MON, self.dp.pk, comment="Rückfrage zu Bestellung 🟥  4711")
        self.assertEqual(self.fake.calls[-1]["projectData"]["comment"], "Rückfrage zu Bestellung 4711")

    def test_ai_off_or_unavailable_falls_back_to_topics(self):
        self.st.comment_ai = False
        self.st.save()
        self.msg(subject="Neue Testboards", folder=EmailMessage.FOLDER_SENT)
        with mock.patch("email_assistant.ai_helper._call_ai_direct") as ai:
            mail_time.book(self.du, MON, self.dp.pk, st=self.st)
        ai.assert_not_called()
        self.assertEqual(self.fake.calls[-1]["projectData"]["comment"], "E-Mails DigiKey: Neue Testboards (1 Antwort)")


class RewriteTests(MailTimeBase):
    def test_rewrite_only_after_deleted_in_askdante(self):
        self.msg(folder=EmailMessage.FOLDER_SENT)
        b = mail_time.book(self.du, MON, self.dp.pk, comment="alt")
        self.fake.records = [{"id": "r1", "person_id": "1108", "day": MON, "duration": 15, "start": "", "stop": "",
                              "description": "alt", "project_id": "77", "project_name": "DigiKey Marketplace",
                              "item_id": "", "item_name": "", "activity_id": "", "activity_name": "",
                              "hourly_rate": None, "cost_center": ""}]
        with self.assertRaises(mail_time.DanteError) as err:
            mail_time.rewrite(self.du, MON, self.dp.pk, comment="neu")
        self.assertIn("Спершу видаліть в askDANTE", err.exception.message)
        self.assertEqual(len(self.fake.calls), 1)               # нічого не дописано — без дубля
        self.fake.records = []                                  # видалили в askDANTE
        b2 = mail_time.rewrite(self.du, MON, self.dp.pk, comment="neu")
        self.assertEqual((b2.status, b2.duration, b2.comment), (Booking.SENT, b.duration, "neu"))
        self.assertEqual(MailTimeEntry.objects.get().minutes_booked, 15)
        self.assertFalse(TimeRecord.objects.filter(description="alt").exists())

    def test_page_shows_text_and_rewrite(self):
        self.user.is_superuser = True
        self.user.save()
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(self.user)
        self.msg(subject="Neue Testboards", folder=EmailMessage.FOLDER_SENT)
        url = reverse("admin:worktime_workday_mailtime")
        q = {"u": self.du.pk, "from": MON.isoformat(), "to": MON.isoformat()}
        key = f"{MON.isoformat()}|{self.dp.pk}"
        page = self.client.get(url, q)
        self.assertContains(page, f'name="c_{key}" value="E-Mails DigiKey: Neue Testboards (1 Antwort)"')
        self.client.post(url, {**q, "action": "book", "pick": [key], f"c_{key}": "Mein Text"})
        self.assertEqual(self.fake.calls[-1]["projectData"]["comment"], "Mein Text")
        page = self.client.get(url, q)
        self.assertContains(page, "«Mein Text»")
        self.assertContains(page, "Перезаписати з іншим описом")
        self.client.post(url, {**q, "rewrite": key, f"c_{key}": "Neuer Text"})   # у копії askDANTE запису немає
        self.assertEqual(self.fake.calls[-1]["projectData"]["comment"], "Neuer Text")
        with mock.patch("email_assistant.ai_helper._call_ai_direct", return_value="Kurz: Testboards."):
            self.client.post(url, {**q, "action": "describe"})
        self.assertContains(self.client.get(url, q), "E-Mails DigiKey: Kurz: Testboards. (1 Antwort)")   # у полі перезапису
