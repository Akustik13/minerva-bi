"""Пошта сайту з Minerva (shop/site_mail.py, GET /api/v1/shop/site-mail/): сайт бере SMTP скриньки магазину,
звіт сайту про власні налаштування, таблиця в картці. python manage.py test shop --settings=tabele.settings_test"""
from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from api.models import APIKey
from email_assistant.models import EmailAccount

from .models import Shop

URL = "/api/v1/shop/site-mail/"


class SiteMailTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("anna", "anna@sevskiy.de", "pw")
        self.acc = EmailAccount.objects.create(
            user=self.user, display_name="Sevskiy Shop", email_address="info@sevskiy.de", imap_username="info@sevskiy.de",
            imap_password="imap-secret", smtp_host="smtp.ionos.de", smtp_port=465, smtp_use_ssl=True, smtp_use_tls=False,
            smtp_username="info@sevskiy.de", smtp_password="smtp-secret")
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True, url="https://shop.example.com",
                                        mail_sender=self.acc, mail_reply_to="service@sevskiy.de",
                                        mail_bcc="archiv@sevskiy.de", team_emails="team@sevskiy.de, chef@sevskiy.de")
        self.key = APIKey.objects.create(name="Сайт", scopes=["products:read"], shop=self.shop)
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION="Token " + self.key.key)

    def get(self, client=None, **params):
        return (client or self.c).get(URL, params).json()

    def test_off_by_default(self):
        self.assertEqual(self.get(), {"shop": "webshop", "managed": False})

    def test_managed_gives_shop_mailbox(self):
        Shop.objects.filter(pk=self.shop.pk).update(site_mail_managed=True, site_order_confirmation=False)
        d = self.get()
        self.assertTrue(d["managed"])
        self.assertEqual(d["smtp"], {"host": "smtp.ionos.de", "port": 465, "security": "ssl", "user": "info@sevskiy.de",
                                     "password": "smtp-secret"})
        self.assertEqual((d["from"], d["from_name"], d["reply_to"], d["bcc"], d["notify"], d["confirmation"]),
                         ("info@sevskiy.de", "Sevskiy Shop", "service@sevskiy.de", ["archiv@sevskiy.de"],
                          ["team@sevskiy.de", "chef@sevskiy.de"], False))

    def test_password_only_for_bound_key_and_needs_sender(self):
        Shop.objects.filter(pk=self.shop.pk).update(site_mail_managed=True)
        loose = APIKey.objects.create(name="RAG", scopes=["products:read"])   # без прив'язки — нічого не отримує
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Token " + loose.key)
        self.assertFalse(self.get(c)["managed"])
        Shop.objects.filter(pk=self.shop.pk).update(mail_sender=None)
        self.assertFalse(self.get()["managed"])
        self.shop.refresh_from_db()
        from . import site_mail
        self.assertIn("Відправник", site_mail.problem(self.shop))

    def test_site_report_and_card_table(self):
        self.get(mail_site="web.example.com", mail_source="file", mail_host="smtp.ionos.de", mail_user="info@sevskiy.de",
                 mail_from="info@sevskiy.de", mail_password="1", mail_notify="sergey@sevskiy.de",
                 mail_confirmation="1", mail_recipient="sergey@sevskiy.de")
        self.get(mail_site="bad host!", mail_host="x")
        self.shop.refresh_from_db()
        rep = self.shop.site_mail_reports["web.example.com"]
        self.assertEqual((rep["host"], rep["password"], rep["notify"], rep["confirmation"]),
                         ("smtp.ionos.de", True, "sergey@sevskiy.de", True))
        self.assertEqual(list(self.shop.site_mail_reports), ["web.example.com"])
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        page = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        self.assertContains(page, "файли сайту: info@sevskiy.de (smtp.ionos.de)")
        self.assertContains(page, "Сайт надсилає листи з цієї ж скриньки")
        self.assertNotContains(page, "smtp-secret")
        Shop.objects.filter(pk=self.shop.pk).update(site_mail_managed=True)
        page = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        self.assertContains(page, "скринька Minerva info@sevskiy.de (smtp.ionos.de)")
        self.assertContains(page, "team@sevskiy.de, chef@sevskiy.de")
