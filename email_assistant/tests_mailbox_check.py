"""Скринька (Email → Акаунти): «🔌 Перевірити підключення» (IMAP + SMTP) і «✉️ Надіслати тестовий лист».
python manage.py test email_assistant.tests_mailbox_check --settings=tabele.settings_test — мережі немає."""
import imaplib
import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from core.smtp_utf8 import check_imap

from .models import EmailAccount


class CheckImapTests(TestCase):
    def fake(self, login_error=None):
        conn = mock.Mock()
        if login_error:
            conn.login.side_effect = imaplib.IMAP4.error(login_error)
        conn.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\Sent) "/" "Sent"'])
        conn.select.return_value = ("OK", [b"42"])
        return conn

    def test_login_folders_and_inbox(self):
        conn = self.fake()
        with mock.patch("imaplib.IMAP4_SSL", return_value=conn) as ssl_cls:
            r = check_imap("imap.ionos.de", 993, use_ssl=True, user="shop@sevskiy.de", password="p")
        ssl_cls.assert_called_once_with("imap.ionos.de", 993, timeout=15)
        self.assertTrue(r["ok"], r)
        texts = " | ".join(t for _, t in r["steps"])
        self.assertIn("Вхід як shop@sevskiy.de — успішно", texts)
        self.assertIn("Папок на сервері: 2", texts)
        self.assertIn("Вхідні: листів 42", texts)
        conn.select.assert_called_once_with("INBOX", readonly=True)   # нічого не змінює
        conn.logout.assert_called_once()

    def test_wrong_password(self):
        with mock.patch("imaplib.IMAP4_SSL", return_value=self.fake(b"AUTHENTICATIONFAILED")):
            r = check_imap("imap.ionos.de", 993, use_ssl=True, user="shop@sevskiy.de", password="bad")
        self.assertFalse(r["ok"])
        self.assertIn("сервер відмовив (AUTHENTICATIONFAILED)", r["steps"][-1][1])
        self.assertIn("Логін або пароль", r["hint"])


class MailboxCheckViewTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.user = User.objects.create_superuser("adm", "adm@example.com", "p")
        self.client.force_login(self.user)
        self.acc = EmailAccount.objects.create(
            user=self.user, email_address="sales01@sevskiy.de", display_name="sales", imap_username="sales01@sevskiy.de",
            imap_password="imap-saved", smtp_host="smtp.ionos.de", smtp_port=465, smtp_use_ssl=True, smtp_use_tls=False,
            smtp_username="sales01@sevskiy.de", smtp_password="smtp-saved")

    def test_page_has_the_buttons(self):
        page = self.client.get(f"/admin/email_assistant/emailaccount/{self.acc.pk}/change/")
        for text in ("🔌 Перевірити підключення", "✉️ Надіслати тестовий лист", f"/{self.acc.pk}/check/",
                     f"/{self.acc.pk}/test-mail/"):
            self.assertContains(page, text)
        add = self.client.get("/admin/email_assistant/emailaccount/add/")
        self.assertContains(add, "/0/check/")                       # нову скриньку можна перевірити до збереження
        self.assertContains(add, "Тестовий лист — після збереження скриньки.")

    def test_check_uses_form_values_and_saved_passwords(self):
        ok = {"ok": True, "steps": [(True, "x")], "hint": ""}
        with mock.patch("core.smtp_utf8.check_imap", return_value=ok) as imap, \
                mock.patch("core.smtp_utf8.check_smtp", return_value=ok) as smtp:
            r = self.client.post(f"/admin/email_assistant/emailaccount/{self.acc.pk}/check/", json.dumps({
                "imap_host": "imap.ionos.de", "imap_port": "993", "imap_use_ssl": True, "imap_username": "sales01@sevskiy.de",
                "imap_password": "", "smtp_host": "smtp.ionos.de", "smtp_port": "587", "smtp_use_tls": True,
                "smtp_use_ssl": False, "smtp_username": "sales01@sevskiy.de", "smtp_password": "neu"}),
                content_type="application/json")
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertEqual([s[0] for s in d["sections"]], ["📥 IMAP (читання)", "📤 SMTP (відправка)"])
        self.assertEqual(imap.call_args.kwargs["password"], "imap-saved")   # порожнє — збережений
        self.assertEqual(smtp.call_args.args[1], 587)                       # значення з форми, ще не збережені
        self.assertEqual((smtp.call_args.kwargs["password"], smtp.call_args.kwargs["use_tls"]), ("neu", True))
        self.assertEqual(self.client.get(f"/admin/email_assistant/emailaccount/{self.acc.pk}/check/").status_code, 403)

    def test_test_mail_from_saved_settings(self):
        with mock.patch("email_assistant.smtp_client.SMTPClient.send",
                        return_value={"ok": True, "via": "sales01@sevskiy.de · smtp.ionos.de:465 · SSL"}) as send:
            r = self.client.post(f"/admin/email_assistant/emailaccount/{self.acc.pk}/test-mail/", json.dumps({"to": ""}),
                                 content_type="application/json").json()
        self.assertTrue(r["ok"], r)
        self.assertEqual(send.call_args.args[0], ["adm@example.com"])        # порожньо — email того, хто натиснув
        self.assertIn("Лист надіслано на adm@example.com", r["message"])
        r = self.client.post(f"/admin/email_assistant/emailaccount/{self.acc.pk}/test-mail/", json.dumps({"to": "kein"}),
                             content_type="application/json").json()
        self.assertFalse(r["ok"])
        with mock.patch("email_assistant.smtp_client.SMTPClient.send",
                        return_value={"ok": False, "error": "Помилка автентифікації SMTP. Перевір логін і пароль."}):
            r = self.client.post(f"/admin/email_assistant/emailaccount/{self.acc.pk}/test-mail/",
                                 json.dumps({"to": "x@example.com"}), content_type="application/json").json()
        self.assertEqual(r, {"ok": False, "error": "Помилка автентифікації SMTP. Перевір логін і пароль."})

    def test_needs_change_permission(self):
        viewer = User.objects.create_user("v", "v@example.com", "p", is_staff=True)
        self.client.force_login(viewer)
        with mock.patch("email_assistant.smtp_client.SMTPClient.send") as send:
            r = self.client.post(f"/admin/email_assistant/emailaccount/{self.acc.pk}/test-mail/", json.dumps({}),
                                 content_type="application/json")
        self.assertIn(r.status_code, (302, 403))
        send.assert_not_called()
