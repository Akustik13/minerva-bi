"""Заплановані листи: send_scheduled відправляє кожен лист не більше одного разу."""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from email_assistant.models import EmailAccount, ScheduledEmail

ME = "sales@sevskiy.de"


class SendScheduledTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("slava", "s@x.y", "p")
        self.account = EmailAccount.objects.create(
            user=self.user, email_address=ME, imap_username=ME, imap_password="x",
            smtp_username=ME, smtp_password="x")
        self.mail = ScheduledEmail.objects.create(
            account=self.account, subject="Erinnerung", to_emails=["buyer@client.example"], body="Hallo",
            scheduled_at=timezone.now() - timedelta(minutes=1))

    def run_command(self):
        call_command("send_scheduled", stdout=StringIO())

    def test_parallel_run_does_not_send_twice(self):
        calls = []

        def send(client, **kw):
            calls.append(kw["subject"])
            if len(calls) == 1:
                self.run_command()   # другий запуск стартує, поки перший ще відправляє
            return {"ok": True}

        with patch("email_assistant.smtp_client.SMTPClient.send", send):
            self.run_command()
        self.assertEqual(calls, ["Erinnerung"])
        self.mail.refresh_from_db()
        self.assertEqual(self.mail.status, ScheduledEmail.STATUS_SENT)
        self.assertIsNotNone(self.mail.sent_at)

    def test_interrupted_send_is_not_repeated(self):
        ScheduledEmail.objects.filter(pk=self.mail.pk).update(sent_at=timezone.now() - timedelta(minutes=20))
        with patch("email_assistant.smtp_client.SMTPClient.send") as send:
            self.run_command()
        send.assert_not_called()
        self.mail.refresh_from_db()
        self.assertEqual(self.mail.status, ScheduledEmail.STATUS_FAILED)
        self.assertIn("невідомо, чи лист пішов", self.mail.error_msg)

    def test_fresh_claim_by_other_run_is_skipped(self):
        ScheduledEmail.objects.filter(pk=self.mail.pk).update(sent_at=timezone.now())
        with patch("email_assistant.smtp_client.SMTPClient.send") as send:
            self.run_command()
        send.assert_not_called()
        self.mail.refresh_from_db()
        self.assertEqual(self.mail.status, ScheduledEmail.STATUS_PENDING)   # ще відправляється іншим запуском

    def test_smtp_failure_and_requeue_from_admin(self):
        with patch("email_assistant.smtp_client.SMTPClient.send", return_value={"ok": False, "error": "535 auth"}):
            self.run_command()
        self.mail.refresh_from_db()
        self.assertEqual((self.mail.status, self.mail.error_msg, self.mail.sent_at),
                         (ScheduledEmail.STATUS_FAILED, "535 auth", None))
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        ScheduledEmail.objects.filter(pk=self.mail.pk).update(sent_at=timezone.now() - timedelta(hours=1))
        self.client.force_login(self.user)
        r = self.client.post(f"/admin/email_assistant/scheduledemail/{self.mail.pk}/change/", {
            "account": self.account.pk, "to_raw": "buyer@client.example", "cc_raw": "", "subject": "Erinnerung",
            "body": "Hallo", "body_html": "", "scheduled_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "status": ScheduledEmail.STATUS_PENDING, "trigger": "manual"})   # так шле datetime-local браузера
        self.assertRedirects(r, "/admin/email_assistant/scheduledemail/", fetch_redirect_response=False)
        self.mail.refresh_from_db()   # знову в черзі — і без броні попередньої спроби
        self.assertEqual((self.mail.status, self.mail.sent_at), (ScheduledEmail.STATUS_PENDING, None))
        with patch("email_assistant.smtp_client.SMTPClient.send", return_value={"ok": True}) as send:
            self.run_command()
        send.assert_called_once()
