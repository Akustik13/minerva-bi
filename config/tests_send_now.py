"""«Надіслати зараз» у налаштуваннях сповіщень — спершу «Кому»: адреси з налаштувань, можна змінити.

python manage.py test config.tests_send_now --settings=tabele.settings_test
"""
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.mail.backends.locmem import EmailBackend
from django.test import TestCase

from config.models import NotificationSettings
from dashboard import notifications

CRITICAL = [{"pk": 1, "sku": "AN220203-02A", "name": "Antenna", "stock": 1.0, "monthly": 4.0, "months_left": 0.25,
             "is_critical": True}]


class SendNowTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        ns = NotificationSettings.get()
        ns.email_enabled, ns.email_host, ns.email_to = True, "smtp.example", "team@firma.example, boss@firma.example"
        ns.telegram_enabled, ns.telegram_bot_token, ns.telegram_chat_id = True, "123:abc", "-100"
        ns.digest_email, ns.digest_telegram = True, True
        ns.save()
        self.client.force_login(User.objects.create_superuser("root", "me@firma.example", "p"))
        for target, kw in (("django.core.mail.get_connection", {"side_effect": lambda *a, **k: EmailBackend()}),
                           ("dashboard.notifications._smtp_connection", {"side_effect": lambda ns: EmailBackend()}),
                           ("dashboard.notifications._get_critical_stock", {"return_value": CRITICAL}),
                           ("dashboard.notifications._get_overdue_orders", {"return_value": []})):
            p = mock.patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)
        self.tg = mock.patch("dashboard.notifications._send_telegram")
        self.telegram = self.tg.start()
        self.addCleanup(self.tg.stop)
        self.url = "/admin/config/notificationsettings/1/send-now/"

    def test_page_prefills_configured_addresses(self):
        r = self.client.get(self.url)
        self.assertContains(r, "team@firma.example, boss@firma.example</textarea>", html=False)
        self.assertContains(r, "Лише мені (me@firma.example)")
        self.assertContains(r, 'name="telegram" value="1" checked')
        self.assertContains(r, "критичних залишків — 1")
        self.assertEqual(len(mail.outbox), 0)                                   # GET нічого не надсилає

    def test_send_only_to_chosen_address_without_telegram(self):
        r = self.client.post(self.url, {"to": "me@firma.example"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual([m.to for m in mail.outbox], [["me@firma.example"]])
        self.telegram.assert_not_called()
        self.assertIsNone(NotificationSettings.get().last_alert_sent)           # розклад команди не зсунуто

    def test_bad_address_is_not_sent(self):
        r = self.client.post(self.url, {"to": "me@firma.example, not-an-email"})
        self.assertContains(r, "Неправильні адреси: not-an-email")
        self.assertEqual(len(mail.outbox), 0)
        r = self.client.post(self.url, {"to": ""})
        self.assertContains(r, "Кому надсилати?")

    def test_daily_report_to_own_address_and_telegram(self):
        r = self.client.post("/admin/config/notificationsettings/1/send-digest/",
                             {"to": "me@firma.example; other@x.example", "telegram": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(mail.outbox[0].to, ["me@firma.example", "other@x.example"])
        self.assertEqual(self.telegram.call_count, 1)
        self.assertIsNone(NotificationSettings.get().digest_last_sent)

    def test_test_email_has_no_telegram_choice(self):
        r = self.client.get("/admin/config/notificationsettings/1/test-email/")
        self.assertNotContains(r, 'name="telegram"')
        self.client.post("/admin/config/notificationsettings/1/test-email/", {"to": "me@firma.example",
                                                                              "telegram": "1"})
        self.assertEqual(mail.outbox[0].to, ["me@firma.example"])
        self.telegram.assert_not_called()

    def test_schedule_still_goes_to_everyone(self):
        res = notifications.run_alerts(force=True)
        self.assertTrue(res["sent"])
        self.assertEqual(mail.outbox[0].to, ["team@firma.example", "boss@firma.example"])
        self.assertIsNotNone(NotificationSettings.get().last_alert_sent)

    def test_parse_recipients(self):
        self.assertEqual(notifications.parse_recipients("a@x.de, A@x.de;\nb@y.com <c@z.org>"),
                         (["a@x.de", "b@y.com", "c@z.org"], []))
