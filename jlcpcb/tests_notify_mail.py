"""✉️ Листи JLCPCB: «Кому» і «Копія (CC)» (jlcpcb/notifications.email_targets), сторінка налаштувань з полем адрес."""
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings

from config.models import NotificationSettings
from jlcpcb.models import JLCConfig, JLCOrder
from jlcpcb.notifications import email_targets, notify_jlc_status_change


class TargetsTests(TestCase):
    def test_own_to_and_cc_without_duplicates(self):
        cfg = JLCConfig.get()
        cfg.notify_email_to = 'a@firma.de, b@firma.de'
        cfg.notify_email_cc = 'B@firma.de; c@firma.de'
        self.assertEqual(email_targets(cfg), (['a@firma.de', 'b@firma.de'], ['c@firma.de']))

    def test_empty_to_uses_general_notification_recipients(self):
        ns = NotificationSettings.get()
        ns.email_to = 'team@firma.de'
        ns.save()
        cfg = JLCConfig.get()
        self.assertEqual(email_targets(cfg), (['team@firma.de'], []))     # раніше — [] (неіснуюче поле)

    def test_only_cc_becomes_to(self):
        cfg = JLCConfig.get()
        cfg.notify_email_cc = 'c@firma.de'
        self.assertEqual(email_targets(cfg), (['c@firma.de'], []))

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_status_mail_goes_to_and_cc(self):
        cfg = JLCConfig.get()
        cfg.notify_email, cfg.notify_telegram = True, False
        cfg.notify_email_to, cfg.notify_email_cc = 'a@firma.de', 'boss@firma.de'
        cfg.save()
        order = JLCOrder.objects.create(jlc_order_id='W1', jlc_order_number='W1', local_status='shipped')
        notify_jlc_status_change(order, 'in_production', 'shipped', force=True)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual((mail.outbox[0].to, mail.outbox[0].cc), (['a@firma.de'], ['boss@firma.de']))


class ConfigPageTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        self.admin = User.objects.create_superuser('boss', 'boss@firma.de', 'x')
        self.client.force_login(self.admin)

    def test_page_has_address_fields_with_accounts(self):
        cfg = JLCConfig.get()
        cfg.notify_email_to = 'a@firma.de, b@firma.de'
        cfg.save()
        r = self.client.get(f'/admin/jlcpcb/jlcconfig/{cfg.pk}/change/')
        html = r.content.decode()
        self.assertEqual(html.count('class="mvel" data-mvel>'), 2)   # «Кому» і «Копія (CC)»
        self.assertIn('Копія (CC)', html)
        self.assertIn('data-email="boss@firma.de"', html)        # «＋ З акаунтів» — я
        self.assertIn('a@firma.de, b@firma.de</textarea>', html)
