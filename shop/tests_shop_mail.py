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
        customer = r["«Willkommen», «Zahlung erhalten», пароль змінено, акаунт видалено"]
        self.assertIn("відповіді — на vertrieb@sevskiy.de", customer["sender"])
        self.assertIn("прихована копія: archiv@sevskiy.de", customer["to"])
        self.assertEqual(r["Команді: оплата отримана, новий акаунт покупця"]["to"], "sergey@sevskiy.de, slava@sevskiy.de")
        order = r["Підтвердження замовлення покупцю"]
        self.assertEqual((order["sender"], order["to"]), ("info@sevskiy.de (з Minerva)", "не надсилається"))
        self.assertIn("site_order_confirmation", [c.get("go") for c in order["change"]])
        self.assertEqual(r["Команді: нове замовлення з сайту"]["to"], "sergey@sevskiy.de, slava@sevskiy.de")

    def test_card_shows_the_map(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(self.user)
        page = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        for text in ("Що звідки йде", "Хто надсилає", 'name="route_account"', "Контактна форма сайту",
                     'href="https://www.sevskiy.de/admin/" target="_blank" rel="noopener"'):
            self.assertContains(page, text)


class MailRouteTests(TestCase):
    """Меню «З якої адреси» в «Що звідки йде»: скринька для кожного листа (Shop.mail_routes, shop/mail.py account_for)."""

    def setUp(self):
        from email_assistant.models import EmailAccount
        from config.models import NotificationSettings, SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        ns = NotificationSettings.get()
        ns.email_enabled, ns.email_host, ns.email_host_user, ns.email_from = True, "smtp.ionos.de", "shop@sevskiy.de", "shop@sevskiy.de"
        ns.save()
        self.user = User.objects.create_superuser("adm", "adm@example.com", "p")

        def box(addr):
            return EmailAccount.objects.create(
                user=self.user, display_name=addr, email_address=addr, imap_username=addr, imap_password="i",
                smtp_host="smtp.ionos.de", smtp_port=587, smtp_use_tls=True, smtp_use_ssl=False,
                smtp_username=addr, smtp_password="s")
        self.sales, self.kasse = box("sales01@sevskiy.de"), box("kasse@sevskiy.de")
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True, url="https://www.sevskiy.de",
                                        mail_sender=self.sales, mail_from_name="Sevskiy Shop",
                                        team_emails="sergey@sevskiy.de")
        self.client.force_login(self.user)

    def post(self, **routes):
        """Зберегти картку магазину з меню «З якої адреси» (решта полів — як у формі)."""
        page = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        form = page.context["adminform"].form
        data = {}
        for name, field in form.fields.items():
            value = form.initial.get(name, field.initial)
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                data[name] = list(value)
            elif isinstance(value, bool):
                if value:
                    data[name] = "on"
            elif isinstance(value, (dict,)):
                import json
                data[name] = json.dumps(value)
            else:
                data[name] = value
        for fs in page.context["inline_admin_formsets"]:
            mgmt = fs.formset.management_form
            for k, v in mgmt.initial.items():
                data[f"{mgmt.prefix}-{k}"] = v
        data.update(routes)
        r = self.client.post(f"/admin/shop/shop/{self.shop.pk}/change/", data)
        self.shop.refresh_from_db()
        return r

    def test_each_mail_from_its_box(self):
        from . import mail_map
        mail_map.save_routes(self.shop, {"route_notices": f"acc:{self.kasse.pk}", "route_team": "ns"})
        self.shop.save()
        self.assertEqual(mail.account_for(self.shop, "account"), self.sales)
        self.assertEqual(mail.account_for(self.shop, "notices"), self.kasse)
        self.assertIsNone(mail.account_for(self.shop, "team"))        # SMTP сповіщень
        mail.send(self.shop, "kunde@firma.de", "paid", "de", name="Anna", number="WS-1", amount="10 EUR")
        self.assertIn("kasse@sevskiy.de", outbox.outbox[-1].from_email)
        mail.send(self.shop, "kunde@firma.de", "verify", "de", name="Anna", link=LINK + "#x")
        self.assertIn("sales01@sevskiy.de", outbox.outbox[-1].from_email)
        mail.send_team(self.shop, "team_registered", email="a@b.de", company="X", name="Y")
        self.assertIn("shop@sevskiy.de", outbox.outbox[-1].from_email)
        type(self.kasse).objects.filter(pk=self.kasse.pk).update(is_active=False)   # вимкнена — як основна
        self.shop.refresh_from_db()
        self.assertEqual(mail.account_for(self.shop, "notices"), self.sales)

    def test_site_route_turns_site_mail_on_and_off(self):
        from . import mail_map, site_mail
        mail_map.save_routes(self.shop, {"route_site": f"acc:{self.kasse.pk}"})
        self.assertTrue(self.shop.site_mail_managed)
        self.assertEqual(site_mail.sender_account(self.shop), self.kasse)
        mail_map.save_routes(self.shop, {"route_site": "files"})
        self.assertFalse(self.shop.site_mail_managed)
        self.assertNotIn("site", self.shop.mail_routes)
        mail_map.save_routes(self.shop, {"route_account": "", "route_site": "acc:99999"})   # невідома — без змін
        self.assertIsNone(self.shop.mail_sender)
        self.assertFalse(self.shop.site_mail_managed)

    def test_card_menus_save_with_the_form(self):
        page = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        for name in ("route_account", "route_notices", "route_team", "route_site"):
            self.assertContains(page, f'name="{name}"')
        self.assertContains(page, "shop@sevskiy.de — SMTP сповіщень Minerva")
        self.assertContains(page, f'<option value="acc:{self.sales.pk}" selected>')
        r = self.post(route_account="", route_notices=f"acc:{self.kasse.pk}", route_team="", route_site="files")
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["adminform"].form.errors)
        self.assertIsNone(self.shop.mail_sender)                            # основні — SMTP сповіщень
        self.assertEqual(self.shop.mail_routes, {"notices": f"acc:{self.kasse.pk}"})

    def test_test_mail_once_per_box(self):
        from . import mail_map
        mail_map.save_routes(self.shop, {"route_notices": f"acc:{self.kasse.pk}"})
        self.shop.save()
        outbox.outbox.clear()
        self.client.post(f"/admin/shop/shop/{self.shop.pk}/test-mail/")
        senders = sorted(m.from_email for m in outbox.outbox)
        self.assertEqual(len(senders), 2, senders)   # sales01 (кабінет, команді) і kasse (Willkommen / оплати)


class TeamTelegramTests(AccountTestCase):
    """Сповіщення команді — і в Telegram: чати магазину або основний чат (shop/mail.py notify_team)."""

    def setUp(self):
        super().setUp()
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
        ns.telegram_enabled, ns.telegram_bot_token, ns.telegram_chat_id = True, "1:abc", "-100"
        ns.save()
        p = mock.patch("dashboard.notifications._send_telegram")
        self.tg = p.start()
        self.addCleanup(p.stop)

    def chats(self):
        return [c.kwargs.get("chat_id_override") for c in self.tg.call_args_list]

    def test_shop_chats_or_default_chat(self):
        mail.notify_team(self.shop, "team_registered", email="a@b.de", company="X & Y <GmbH>", name="Y")
        self.assertEqual(self.chats(), ["-100"])                               # свого немає — основний чат
        self.assertNotIn("<GmbH>", self.tg.call_args.args[1])                  # текст екранований для HTML Telegram
        self.tg.reset_mock()
        Shop.objects.filter(pk=self.shop.pk).update(team_telegram="555, @lager_chan, ???")
        self.shop.refresh_from_db()
        self.assertEqual(mail.notify_team(self.shop, "team_paid", number="WS-1", amount="1 EUR", customer="K",
                                          reference="pi"), ["telegram"])
        self.assertEqual(self.chats(), ["555", "@lager_chan"])
        self.tg.reset_mock()
        Shop.objects.filter(pk=self.shop.pk).update(team_notify_telegram=False)
        self.shop.refresh_from_db()
        mail.notify_team(self.shop, "team_paid", number="WS-1", amount="1 EUR", customer="K", reference="pi")
        self.tg.assert_not_called()

    def test_card_edits_who_gets_what(self):
        from config.models import SystemSettings
        from . import mail_map
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        team = next(r for r in mail_map.rows(self.shop) if r["mail"].startswith("Команді: оплата"))
        self.assertEqual([e.get("sync") for e in team["edit"]],
                         ["", "team_notify_registration", "team_emails", "team_notify_telegram", "team_telegram"])
        self.client.force_login(User.objects.create_superuser("tgadm", "tgadm@example.com", "p"))
        page = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        for text in ('data-sync="id_team_emails"', 'data-sync="id_team_telegram"', 'data-sync="id_mail_bcc"',
                     'name="team_telegram"', "data-nt-hide=", "mv_notify_pick.js"):
            self.assertContains(page, text)
