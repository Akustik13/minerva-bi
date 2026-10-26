"""Пошта магазину: відповіді (Reply-To), прихована копія, сповіщення команді на адреси магазину, «Ласкаво просимо»,
новий акаунт — команді (shop/mail.py, shop/accounts.py). python manage.py test shop --settings=tabele.settings_test"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail as outbox
from django.test import TestCase

from . import accounts, mail
from .models import Shop, ShopCustomer, ShopPayment
from .tests_accounts import LINK, PASSWORD, REG, AccountTestCase, token_from


class MailHeadersTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        Shop.objects.filter(pk=self.shop.pk).update(mail_reply_to="info@sevskiy.de",
                                                    mail_bcc="archiv@sevskiy.de, chef@sevskiy.de")
        self.shop.refresh_from_db()

    def test_reply_to_and_bcc_only_without_one_time_links(self):
        mail.send(self.shop, "kunde@firma.de", "paid", "de", name="Anna", number="WS-1", amount="10 EUR")
        m = outbox.outbox[-1]
        self.assertEqual((m.reply_to, m.bcc), (["info@sevskiy.de"], ["archiv@sevskiy.de", "chef@sevskiy.de"]))
        for kind in ("verify", "reset", "invite"):
            mail.send(self.shop, "kunde@firma.de", kind, "de", name="Anna", link=LINK + "#x")
            self.assertEqual((outbox.outbox[-1].reply_to, outbox.outbox[-1].bcc), (["info@sevskiy.de"], []), kind)

    def test_team_mail_goes_to_shop_team_addresses_or_notifications(self):
        self.assertEqual(mail.send_team(self.shop, "team_registered", email="a@b.de", company="X", name="Y"), "")
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
        ns.email_enabled, ns.email_to = True, "zentrale@sevskiy.de"
        ns.save()
        outbox.outbox.clear()
        with mock.patch("dashboard.notifications._send_event_email") as sent:   # загальний SMTP сповіщень Minerva
            self.assertEqual(mail.send_team(self.shop, "team_registered", email="a@b.de", company="X", name="Y"),
                             "notifications")
        self.assertIn("a@b.de", sent.call_args.args[1])
        Shop.objects.filter(pk=self.shop.pk).update(team_emails="shop-team@sevskiy.de, vertrieb@sevskiy.de")
        self.shop.refresh_from_db()
        self.assertEqual(mail.send_team(self.shop, "team_registered", email="a@b.de", company="Firma X", name="Y"),
                         "team")
        m = outbox.outbox[-1]
        self.assertEqual(m.to, ["shop-team@sevskiy.de", "vertrieb@sevskiy.de"])
        self.assertIn("Firma X", m.subject)
        self.assertEqual((m.reply_to, m.bcc), ([], []))   # команді — без Reply-To покупцям і без копії

    def test_addresses_helper(self):
        self.assertEqual(mail.addresses(" a@b.de; c@d.de ,a@B.de,, "), ["a@b.de", "c@d.de"])


class WelcomeAndRegistrationTests(AccountTestCase):
    def test_welcome_after_verification(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.register_and_verify()
        self.assertIn("Willkommen bei", outbox.outbox[-1].subject)
        self.assertIn(LINK, outbox.outbox[-1].body)   # посилання на кабінет

    def test_no_welcome_when_disabled(self):
        Shop.objects.filter(pk=self.shop.pk).update(mail_welcome=False)
        with self.captureOnCommitCallbacks(execute=True):
            self.register_and_verify()
        self.assertFalse([m for m in outbox.outbox if "Willkommen" in m.subject])

    def test_invite_first_password_sends_welcome_instead_of_changed(self):
        customer = ShopCustomer.objects.create(shop=self.shop, email="neu@firma.de", password="!", lang="de",
                                               name="Neu")
        accounts.remember_links(self.shop, {"de": LINK})
        self.assertTrue(accounts.send_invite(customer, LINK))
        token = token_from(outbox.outbox[-1], "reset")
        outbox.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            r = self.post("password/reset/", {"token": token, "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.content)
        subjects = [m.subject for m in outbox.outbox]
        self.assertTrue(any("Willkommen" in s for s in subjects), subjects)
        self.assertFalse(any("geändert" in s for s in subjects), subjects)

    def test_team_notified_about_new_registration(self):
        Shop.objects.filter(pk=self.shop.pk).update(team_notify_registration=True, team_emails="team@sevskiy.de")
        self.assertEqual(self.post("register/", REG).status_code, 202)
        team = [m for m in outbox.outbox if m.to == ["team@sevskiy.de"]]
        self.assertEqual(len(team), 1)
        self.assertIn("anna@firma.de", team[0].subject)
        outbox.outbox.clear()
        self.post("register/", REG)   # повторна (непідтверджена) реєстрація — команді вдруге не пишемо
        self.assertFalse([m for m in outbox.outbox if m.to == ["team@sevskiy.de"]])


class PaymentTeamMailTests(AccountTestCase):
    def test_paid_team_mail_to_shop_team(self):
        from sales.models import SalesOrder
        Shop.objects.filter(pk=self.shop.pk).update(team_emails="team@sevskiy.de")
        self.shop.refresh_from_db()
        order = SalesOrder.objects.create(source="webshop", order_number="WS-77", email="k@firma.de", client="Firma")
        payment = ShopPayment.objects.create(shop=self.shop, order=order, session_id="cs_test_77",
                                             amount=Decimal("119.00"), currency="EUR")
        from .payments import _notify_team
        _notify_team(payment)
        m = [x for x in outbox.outbox if x.to == ["team@sevskiy.de"]][-1]
        self.assertIn("WS-77", m.subject)


class MailAdminTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True, team_emails="team@sevskiy.de")
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))

    def test_mail_block_and_validation(self):
        page = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        for text in ("✉️ Пошта магазину", "Відповіді покупців", "Сповіщення команді: team@sevskiy.de",
                     "✉️ Надіслати тестовий лист собі", "➕ Додати скриньку в Minerva"):
            self.assertContains(page, text)
        from .admin_payments import ShopForm
        f = ShopForm(instance=self.shop)
        f.cleaned_data = {"team_emails": "a@b.de; c@d.de", "mail_bcc": "kein-email"}
        self.assertEqual(f.clean_team_emails(), "a@b.de, c@d.de")
        with self.assertRaises(Exception):
            f.clean_mail_bcc()
