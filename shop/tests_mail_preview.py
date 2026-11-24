"""«👁 Приклад» листа в таблиці «Хто що кому шле» (shop/mail_preview.py, POST …/shop/<pk>/mail-preview/).
python manage.py test shop.tests_mail_preview --settings=tabele.settings_test"""
import json
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.test import TestCase

from . import mail_preview
from .models import Shop


class _Resp:
    def __init__(self, code, data=None):
        self.status_code, self._data = code, data

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


SITE_LETTERS = {"order_customer": {"subject": "Bestellnummer PO-TEST-001 – Sevskiy GmbH", "body": "Vielen Dank\nIBAN: …",
                                   "from": "Site <own@x.de>", "to": "", "to_customer": True, "active": True,
                                   "cc": [], "bcc": [], "warnings": ["Увага: не всі реквізити заповнені"]},
                "order_team": {"subject": "Neue Shop-Bestellung", "body": "Bestellung …", "to": "site-team@x.de",
                               "cc": ["<script>"], "bcc": []},
                "evil": {"subject": "x"}}


class PreviewTests(TestCase):
    def setUp(self):
        from config.models import NotificationSettings, SystemSettings
        from email_assistant.models import EmailAccount
        from core.shared_cache import cache
        cache.clear()
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        ns = NotificationSettings.get()
        ns.email_enabled, ns.email_host, ns.email_from, ns.email_to = True, "smtp.x", "minerva@x.de", "zentrale@x.de"
        ns.telegram_enabled, ns.telegram_bot_token, ns.telegram_chat_id = True, "1:a", "-100"
        ns.save()
        self.user = User.objects.create_superuser("adm", "adm@example.com", "p")
        self.acc = EmailAccount.objects.create(
            user=self.user, display_name="Shop", email_address="shop@x.de", imap_username="shop@x.de", imap_password="i",
            smtp_host="smtp.x", smtp_port=587, smtp_use_tls=True, smtp_use_ssl=False, smtp_username="shop@x.de",
            smtp_password="s")
        self.shop = Shop.objects.create(
            name="Web", slug="webshop", is_default=True, url="https://www.sevskiy.de", mail_sender=self.acc,
            mail_from_name="Sevskiy Shop", mail_reply_to="vertrieb@x.de", team_emails="team@x.de",
            account_links={"de": "https://www.sevskiy.de/de/shop/konto"},
            mail_letters={"welcome": {"cc": "chef@x.de", "bcc": "archiv@x.de"}, "team_paid": {"cc": "buch@x.de"},
                          "invoice": {"bcc": "buch@x.de"}, "order_customer": {"cc": "sales@x.de", "bcc": "archiv@x.de"},
                          "order_team": {"cc": "lager@x.de"}},
            site_mail_reports={"www.sevskiy.de": {"at": "2026-11-24T08:00:00+00:00", "from": "own@x.de",
                                                  "host": "smtp.ionos.de", "notify": "site-team@x.de",
                                                  "confirmation": True, "recipient": "kontakt@x.de", "letters": True}})

    def test_minerva_customer_letter(self):
        d = mail_preview.preview(self.shop, "welcome")
        self.assertEqual((d["from"], d["to"], d["cc"], d["bcc"], d["reply_to"]),
                         ("Sevskiy Shop <shop@x.de>", [mail_preview.BUYER], ["chef@x.de"], ["archiv@x.de"], "vertrieb@x.de"))
        self.assertIn("Willkommen", d["subject"])
        self.assertIn("https://www.sevskiy.de/de/shop/konto", d["html"])
        self.assertTrue(d["active"] and d["to_example"] and d["bcc_allowed"])
        self.assertEqual([x[0] for x in d["langs"]], ["de", "en"])
        Shop.objects.filter(pk=self.shop.pk).update(mail_welcome=False)
        self.shop.refresh_from_db()
        self.assertFalse(mail_preview.preview(self.shop, "welcome")["active"])

    def test_one_time_links_without_copies(self):
        d = mail_preview.preview(self.shop, "", variant="reset")
        self.assertEqual((d["variant"], d["cc"], d["bcc"]), ("reset", [], []))
        self.assertIn("#reset=BEISPIEL", d["text"])
        self.assertTrue(any("одноразове посилання" in n for n in d["notes"]))
        self.assertEqual(mail_preview.preview(self.shop, "", variant="evil")["variant"], "verify")

    def test_team_letter(self):
        d = mail_preview.preview(self.shop, "team_paid")
        self.assertEqual((d["to"], d["cc"], d["bcc_allowed"], d["to_example"]), (["team@x.de"], ["buch@x.de"], False, False))
        self.assertTrue(d["telegram"])
        Shop.objects.filter(pk=self.shop.pk).update(team_emails="")
        self.shop.refresh_from_db()
        self.assertEqual(mail_preview.preview(self.shop, "team_paid")["to"], ["zentrale@x.de"])   # як «Кому» сповіщень

    def test_invoice(self):
        d = mail_preview.preview(self.shop, "invoice")
        self.assertTrue(d["subject"])
        self.assertTrue(d["attachments"][0].endswith(".pdf"))
        self.assertEqual(d["bcc"], ["buch@x.de"])

    def test_unknown_letter(self):
        with self.assertRaises(mail_preview.PreviewError):
            mail_preview.preview(self.shop, "nope")

    def _hook(self):
        from api.models import Webhook
        return Webhook.objects.create(name="Сайт", url="https://www.sevskiy.de/api/shop/webhook.php",
                                      secret="whsec_test", events=["stock.changed"])

    def test_site_letter_text_from_site(self):
        Shop.objects.filter(pk=self.shop.pk).update(site_mail_managed=True)
        self.shop.refresh_from_db()
        hook = self._hook()
        with mock.patch("requests.post", return_value=_Resp(200, {"ok": True, "letters": SITE_LETTERS})) as post:
            d = mail_preview.preview(self.shop, "order_customer", "en", "invoice")
            mail_preview.preview(self.shop, "order_team", "en", "invoice")   # той самий запит — з кешу
        self.assertEqual(post.call_count, 1)
        args, kw = post.call_args
        self.assertEqual(args[0], hook.url)
        self.assertFalse(kw["allow_redirects"])
        body = kw["data"].decode()
        from api.webhooks import verify
        self.assertTrue(verify("whsec_test", body, kw["headers"]["X-Minerva-Signature"]))
        self.assertEqual(json.loads(body)["data"], {"lang": "en", "method": "invoice"})
        self.assertEqual((d["subject"], d["text"]), ("Bestellnummer PO-TEST-001 – Sevskiy GmbH", "Vielen Dank\nIBAN: …"))
        self.assertEqual((d["cc"], d["bcc"], d["reply_to"]), (["sales@x.de"], ["archiv@x.de"], "vertrieb@x.de"))
        self.assertEqual(d["from"], "Sevskiy Shop <shop@x.de>")
        self.assertIn("Увага: не всі реквізити заповнені", d["notes"])
        self.assertEqual([x[0] for x in d["methods"]], ["prepayment", "invoice", "cod", "stripe"])
        team = mail_preview.preview(self.shop, "order_team", "en", "invoice")
        self.assertEqual((team["to"], team["cc"]), (["team@x.de"], ["lager@x.de"]))
        self.assertNotIn("evil", mail_preview.site_texts(self.shop, "en", "invoice")["letters"])

    def test_site_problems(self):
        Shop.objects.filter(pk=self.shop.pk).update(site_mail_managed=True)
        self.shop.refresh_from_db()
        d = mail_preview.preview(self.shop, "order_customer")   # вебхука немає
        self.assertEqual(d["subject"], "")
        self.assertTrue(any("немає вебхука" in n for n in d["notes"]))
        self._hook()
        from core.shared_cache import cache
        cache.clear()
        with mock.patch("requests.post", return_value=_Resp(401)):
            d = mail_preview.preview(self.shop, "quote_customer")
        self.assertTrue(any("HTTP 401" in n for n in d["notes"]))
        cache.clear()
        with mock.patch("requests.post", return_value=_Resp(200, {"ok": True, "event": "letters.preview"})):
            d = mail_preview.preview(self.shop, "quote_team")   # старий сайт — без letters
        self.assertTrue(any("оновлення файлів сайту" in n for n in d["notes"]))

    def test_site_files_mode(self):
        self._hook()
        with mock.patch("requests.post", return_value=_Resp(200, {"ok": True, "letters": SITE_LETTERS})):
            d = mail_preview.preview(self.shop, "order_team")   # сайт пише зі своїх налаштувань
        self.assertEqual((d["to"], d["cc"]), (["site-team@x.de"], ["<script>"]))
        self.assertTrue(any("з налаштувань сайту" in n for n in d["notes"]))

    def test_apply_form_unsaved_values(self):
        post = {"mail_letters_form": "1", "mlc__welcome": "neu@x.de", "mlb__welcome": "", "mail_reply_to": "a@x.de",
                "team_emails": "t2@x.de", "mail_welcome": "on", "route_site": f"acc:{self.acc.pk}"}
        now, errors = mail_preview.apply_form(self.shop, post)
        self.assertEqual((now.mail_letters, now.mail_reply_to, now.team_emails, now.site_mail_managed, errors),
                         ({"welcome": {"cc": "neu@x.de"}}, "a@x.de", "t2@x.de", True, []))
        self.assertTrue(mail_preview.unsaved(self.shop, now))
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.mail_letters["welcome"]["cc"], "chef@x.de")   # у базі — як було
        self.assertFalse(mail_preview.apply_form(self.shop, {"mail_letters_form": "1"})[0].mail_welcome)   # знято
        self.assertEqual(mail_preview.apply_form(self.shop, {"mlc__welcome": "x@y.de"})[0].mail_letters,
                         self.shop.mail_letters)   # без таблиці — як збережено
        self.assertTrue(mail_preview.apply_form(self.shop, {"mail_letters_form": "1", "mlc__paid": "kaputt"})[1])

    def test_admin_endpoint(self):
        url = f"/admin/shop/shop/{self.shop.pk}/mail-preview/"
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(url).status_code, 403)
        r = self.client.post(url, {"pv_letter": "welcome", "mail_letters_form": "1", "mlc__welcome": "neu@x.de",
                                   "mail_welcome": "on", "mail_reply_to": "vertrieb@x.de", "team_emails": "team@x.de"})
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["cc"], ["neu@x.de"])
        self.assertIn("ще не збережено", d["notes"][0])
        self.assertEqual(self.client.post(url, {"pv_letter": "nope"}).status_code, 400)
        page = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        self.assertContains(page, 'class="mm-pv" data-letter="welcome"')
        self.assertContains(page, f'id="mm-pv-root" data-url="{url}"')
        self.assertContains(page, "mv_mail_preview.js")

    def test_viewer_sees_saved_values(self):
        viewer = User.objects.create_user("v", "v@x.de", "p", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename="view_shop"))
        self.client.force_login(viewer)
        url = f"/admin/shop/shop/{self.shop.pk}/mail-preview/"
        r = self.client.post(url, {"pv_letter": "welcome", "mail_letters_form": "1", "mlc__welcome": "fremd@x.de"})
        if r.status_code == 403:   # перегляд магазинів закрито ролями Minerva — теж правильно
            return
        self.assertEqual(r.json()["cc"], ["chef@x.de"])   # без права змінювати — лише збережене
