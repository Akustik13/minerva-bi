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


class MailMapTests(TestCase):
    """«Що звідки йде» (shop/mail_map.py): хто надсилає який лист, з якої адреси, кому і де змінити."""

    def setUp(self):
        from email_assistant.models import EmailAccount
        self.user = User.objects.create_superuser("adm", "adm@example.com", "p")
        self.acc = EmailAccount.objects.create(
            user=self.user, display_name="Sevskiy Shop", email_address="info@sevskiy.de", imap_username="info@sevskiy.de",
            imap_password="imap-secret", smtp_host="smtp.ionos.de", smtp_port=587, smtp_use_ssl=False, smtp_use_tls=True,
            smtp_username="info@sevskiy.de", smtp_password="smtp-secret")
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True, url="https://www.sevskiy.de/shop/",
                                        site_mail_reports={"www.sevskiy.de": {
                                            "at": "2026-11-05T10:45:00+00:00", "from": "info@sevskiy.de",
                                            "host": "smtp.ionos.de", "notify": "sergey@sevskiy.de", "confirmation": True,
                                            "recipient": "kontakt@sevskiy.de"}})

    def rows(self):
        from . import mail_map
        self.shop.refresh_from_db()
        return {r["mail"]: r for r in mail_map.rows(self.shop)}

    def test_without_sender_and_from_site_files(self):
        r = self.rows()
        self.assertIn("❌ не задано", r["Підтвердження email, новий пароль, запрошення від менеджера"]["sender"])
        self.assertIn("нікому", r["Команді: оплата отримана, новий акаунт покупця"]["to"])
        order = r["Підтвердження замовлення покупцю"]
        self.assertEqual((order["who"], order["to"]), ("сайт www.sevskiy.de", "покупцю"))
        self.assertIn("info@sevskiy.de (smtp.ionos.de) — файли сайту", order["sender"])
        self.assertEqual(r["Команді: нове замовлення з сайту"]["to"], "sergey@sevskiy.de")
        contact = r["Контактна форма сайту"]
        self.assertEqual(contact["to"], "kontakt@sevskiy.de")
        self.assertEqual(contact["change"][0]["url"], "https://www.sevskiy.de/admin/")   # адмінка — корінь домену

    def test_shop_mailbox_team_and_site_from_minerva(self):
        Shop.objects.filter(pk=self.shop.pk).update(
            mail_sender=self.acc, mail_from_name="Sevskiy Shop", mail_reply_to="vertrieb@sevskiy.de",
            mail_bcc="archiv@sevskiy.de", team_emails="sergey@sevskiy.de, slava@sevskiy.de",
            site_mail_managed=True, site_order_confirmation=False)
        r = self.rows()
        verify = r["Підтвердження email, новий пароль, запрошення від менеджера"]
        self.assertEqual(verify["sender"], "Sevskiy Shop <info@sevskiy.de>")
        self.assertIn(f"/admin/email_assistant/emailaccount/{self.acc.pk}/change/", [c.get("url") for c in verify["change"]])
        customer = r["«Willkommen», пароль змінено, акаунт видалено"]
        self.assertIn("відповіді — на vertrieb@sevskiy.de", customer["sender"])
        self.assertIn("прихована копія: archiv@sevskiy.de", customer["to"])
        self.assertEqual(r["Команді: оплата отримана, новий акаунт покупця"]["to"], "sergey@sevskiy.de, slava@sevskiy.de")
        order = r["Підтвердження замовлення покупцю"]
        self.assertEqual((order["sender"], order["to"]), ("Sevskiy Shop <info@sevskiy.de> (з Minerva)", "не надсилається"))
        self.assertIn("site_order_confirmation", [c.get("go") for c in order["change"]])
        self.assertEqual(r["Команді: нове замовлення з сайту"]["to"], "sergey@sevskiy.de, slava@sevskiy.de")

    def test_card_shows_the_map(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(self.user)
        page = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        for text in ("Що звідки йде", "Хто надсилає", 'data-go="mail_sender"', "Контактна форма сайту",
                     'href="https://www.sevskiy.de/admin/" target="_blank" rel="noopener"'):
            self.assertContains(page, text)


class PurposeSenderTests(TestCase):
    """Скринька для кожного призначення (shop/mail.py PURPOSES): кабінет, оплати, команді, сайт; порожньо — основна."""

    def setUp(self):
        from email_assistant.models import EmailAccount
        self.user = User.objects.create_superuser("adm", "adm@example.com", "p")

        def box(addr):
            return EmailAccount.objects.create(
                user=self.user, display_name=addr, email_address=addr, imap_username=addr, imap_password="i",
                smtp_host="smtp.ionos.de", smtp_port=587, smtp_use_tls=True, smtp_use_ssl=False,
                smtp_username=addr, smtp_password="s")
        self.main, self.kasse, self.team, self.site = (box(a) for a in (
            "shop@sevskiy.de", "kasse@sevskiy.de", "team@sevskiy.de", "web@sevskiy.de"))
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True, url="https://www.sevskiy.de",
                                        mail_sender=self.main, mail_from_name="Sevskiy Shop",
                                        team_emails="sergey@sevskiy.de")

    def set(self, **kw):
        Shop.objects.filter(pk=self.shop.pk).update(**kw)
        self.shop.refresh_from_db()

    def test_each_purpose_falls_back_to_the_main_box(self):
        for purpose in mail.PURPOSES:
            self.assertEqual(mail.account_for(self.shop, purpose), self.main, purpose)
        self.set(mail_sender_payments=self.kasse, mail_sender_team=self.team, mail_sender_site=self.site)
        self.assertEqual([mail.account_for(self.shop, p) for p in ("customer", "payment", "team", "site")],
                         [self.main, self.kasse, self.team, self.site])
        type(self.kasse).objects.filter(pk=self.kasse.pk).update(is_active=False)   # вимкнена — знову основна
        self.shop.refresh_from_db()
        self.assertEqual(mail.account_for(self.shop, "payment"), self.main)

    def test_mails_go_from_their_box(self):
        self.set(mail_sender_payments=self.kasse, mail_sender_team=self.team)
        mail.send(self.shop, "kunde@firma.de", "paid", "de", name="Anna", number="WS-1", amount="10 EUR")
        self.assertIn("kasse@sevskiy.de", outbox.outbox[-1].from_email)
        mail.send(self.shop, "kunde@firma.de", "verify", "de", name="Anna", link=LINK + "#x")
        self.assertIn("shop@sevskiy.de", outbox.outbox[-1].from_email)
        mail.send_team(self.shop, "team_registered", email="a@b.de", company="X", name="Y")
        self.assertIn("team@sevskiy.de", outbox.outbox[-1].from_email)
        self.assertEqual(outbox.outbox[-1].to, ["sergey@sevskiy.de"])

    def test_site_takes_its_box(self):
        from api.models import APIKey
        from . import site_mail
        key = APIKey.objects.create(name="Сайт", scopes=["products:read"], shop=self.shop)
        self.set(site_mail_managed=True)
        self.assertEqual(site_mail.public_config(self.shop, key)["from"], "shop@sevskiy.de")
        self.set(mail_sender_site=self.site)
        cfg = site_mail.public_config(self.shop, key)
        self.assertEqual((cfg["from"], cfg["smtp"]["user"]), ("web@sevskiy.de", "web@sevskiy.de"))

    def test_test_mail_once_per_box_and_card_buttons(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(self.user)
        self.set(mail_sender_payments=self.kasse)
        outbox.outbox.clear()
        self.client.post(f"/admin/shop/shop/{self.shop.pk}/test-mail/")
        senders = sorted(m.from_email for m in outbox.outbox)
        self.assertEqual(len(senders), 2)   # основна (кабінет + команді) і окрема для оплат
        self.assertTrue(any("kasse@" in x for x in senders) and any("shop@" in x for x in senders))
        page = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        for field in ("mail_sender", "mail_sender_payments", "mail_sender_team", "mail_sender_site"):
            self.assertContains(page, f'id="add_id_{field}"')   # «＋» — нова скринька прямо з картки
        self.assertContains(page, "«Zahlung erhalten» — оплату отримано")
