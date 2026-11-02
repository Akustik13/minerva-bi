"""Захист (пакет H3): старі адреси, що змінюють дані або показують чуже, — лише з правом.

• замовлення: «📧 Надіслати клієнту» й «📥 Підтвердження замовлення» — лише з правом змінювати замовлення й коли лист
  увімкнено в «Налаштуваннях сповіщень» (кнопки й самі листи), текст листа екранується; документи (🗑️, 📤, 💾 — тепер
  лише POST), «🔄 Синхронізувати склад», «🔄 Трекінг з DigiKey», «📦 Відстежувати», рахунок EU, повідомлення DigiKey —
  POST і право змінювати; хто замовлення лише переглядає, цих кнопок не бачить;
• CRM: /crm/customer/<pk>/… — лише з розділом CRM (раніше — будь-який співробітник); у хронології тема й текст листа —
  лише з власних скриньок; нотатка, лист з картки, «Застосувати» стратегію AI — право змінювати клієнтів, лист без
  пошти Minerva — лише на адреси клієнта;
• склад: дії списку рухів і товарів, що змінюють дані, — лише з правом змінювати;
• «⏱ Час askDANTE» у картках клієнта й проєкту — лише з розділом «Робочий час», опис запису — його автору,
  вартість — адміністраторам.

python manage.py test core.tests_hardening3 --settings=tabele.settings_test
"""
import datetime
import io
import json
import shutil
import sys
import tempfile
import types
import uuid
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib import admin
from django.contrib.auth.models import User
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

NO_ORDER_RIGHT = "Немає права змінювати замовлення"
CLIENT = "einkauf@client.example"
LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
REAL_GET_CONNECTION = mail.get_connection


def person(username, role="manager", **profile):
    user = User.objects.create_user(username, f"{username}@x.y", "p", is_staff=True)
    user.profile.role = role
    for key, value in profile.items():
        setattr(user.profile, key, value)
    user.profile.save()
    return User.objects.get(pk=user.pk)


def accountant():
    """Продажі й склад — лише перегляд (і експорт)."""
    return person("bea", "accountant")


def locmem(*args, **kwargs):
    """SMTP фірми в тестах — пам'ять (mail.outbox)."""
    return REAL_GET_CONNECTION(LOCMEM)


def pdf_generators(generate=None):
    """sales.doc_generators (reportlab) підмінено цілком, як у sales/tests_eu_invoice.py; за замовчуванням — PDF-заглушка,
    generate — свій (напр. той, що падає: перевірка, що генерації не було)."""
    gen = generate or mock.Mock(side_effect=lambda order, overrides=None: io.BytesIO(b"%PDF-1.4 test"))
    module = types.SimpleNamespace(generate_proforma=gen, generate_customs=gen, generate_packing_list=gen,
                                   generate_order_confirmation=gen)
    return mock.patch.dict(sys.modules, {"sales.doc_generators": module})


def ai_service():
    """ai_assistant.service без бібліотеки anthropic (як crm/tests_mailbox_privacy.py)."""
    fake = types.ModuleType("ai_assistant.service")
    fake.reset_conversation = mock.Mock()
    return mock.patch.dict(sys.modules, {"ai_assistant.service": fake}), fake


def notification_settings(**fields):
    from config.models import NotificationSettings
    ns = NotificationSettings.get()
    ns.email_enabled = True
    ns.email_host_user = ns.email_from = "noreply@firma.example"
    for key, value in fields.items():
        setattr(ns, key, value)
    ns.save()
    return ns


class _Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})


class NotifyCustomerTests(_Base):
    """Листи клієнту з картки замовлення — SMTP фірми (NotificationSettings.email_from)."""

    def setUp(self):
        super().setUp()
        from sales.models import SalesOrder
        notification_settings(customer_notify_enabled=True, order_confirm_notify_enabled=True,
                              order_confirm_notify_sources="")
        self.order = SalesOrder.objects.create(source="manual", order_number="A-100", email="kunde@client.example")
        base = f"/admin/sales/salesorder/{self.order.pk}"
        self.change = f"{base}/change/"
        self.urls = {"customer": f"{base}/notify-customer/", "order_confirm": f"{base}/notify-order-confirm/"}
        smtp = mock.patch("dashboard.notifications._smtp_connection", side_effect=locmem)
        smtp.start()
        self.addCleanup(smtp.stop)

    def _post(self, url, body="Hallo"):
        return self.client.post(url, json.dumps({"to": "kunde@client.example", "cc": "", "subject": "Ihre Bestellung",
                                                 "body": body}), content_type="application/json")

    def _sent_marks(self):
        self.order.refresh_from_db()
        return self.order.ship_notify_sent_at, self.order.order_confirm_sent_at

    def test_view_only_refused(self):
        self.client.force_login(accountant())
        for url in self.urls.values():
            r = self.client.get(url)
            self.assertEqual(r.status_code, 403, url)
            self.assertNotIn("kunde@client.example", r.content.decode())
            r = self._post(url, body="Spam über die Firmenadresse")
            self.assertEqual(r.status_code, 403, url)
            self.assertEqual(r.json(), {"ok": False, "error": NO_ORDER_RIGHT})
        self.assertEqual(mail.outbox, [])
        self.assertEqual(self._sent_marks(), (None, None))

    def test_buttons_only_for_editors(self):
        self.client.force_login(accountant())
        r = self.client.get(self.change)
        self.assertEqual(r.status_code, 200)
        for url in self.urls.values():
            self.assertNotContains(r, url)
        self.client.force_login(person("olga"))
        r = self.client.get(self.change)
        for url in self.urls.values():
            self.assertContains(r, url)

    def test_settings_are_enforced_on_post(self):
        self.client.force_login(person("olga"))
        notification_settings(customer_notify_enabled=False, order_confirm_notify_sources="digikey, webshop")
        for url in self.urls.values():
            self.assertEqual(self._post(url).status_code, 403, url)
        self.assertIn("джерела «manual»", self._post(self.urls["order_confirm"]).json()["error"])
        r = self.client.get(self.change)
        for url in self.urls.values():
            self.assertNotContains(r, url)                                 # і кнопок немає
        notification_settings(customer_notify_enabled=True, order_confirm_notify_sources="", email_enabled=False)
        for url in self.urls.values():
            self.assertEqual(self._post(url).status_code, 403, url)
        self.assertEqual(mail.outbox, [])
        self.assertEqual(self._sent_marks(), (None, None))

    def test_editor_sends_escaped_text(self):
        self.client.force_login(person("olga"))
        r = self.client.get(self.urls["customer"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["to"], "kunde@client.example")
        r = self._post(self.urls["customer"], body='<img src=x onerror="alert(1)">\nZeile 2')
        self.assertEqual(r.json(), {"ok": True})
        self.assertEqual(len(mail.outbox), 1)
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;<br>Zeile 2", html)
        self.assertNotIn("<img", html)
        self.assertEqual(mail.outbox[0].from_email, "noreply@firma.example")
        self.assertIsNotNone(self._sent_marks()[0])
        self.assertEqual(self._post(self.urls["order_confirm"]).json(), {"ok": True})
        self.assertIsNotNone(self._sent_marks()[1])

    def test_other_methods_not_allowed(self):
        self.client.force_login(person("olga"))
        for url in self.urls.values():
            r = self.client.put(url)
            self.assertEqual(r.status_code, 405, url)
        self.assertEqual(mail.outbox, [])


class OrderActionTests(_Base):
    """Інші дії картки замовлення, що його змінюють: лише POST (з CSRF) і з правом змінювати."""

    def setUp(self):
        super().setUp()
        from sales.models import SalesOrder
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        over = override_settings(MEDIA_ROOT=Path(self.root))
        over.enable()
        self.addCleanup(over.disable)
        self.order = SalesOrder.objects.create(source="digikey", order_number="DK-777", tracking_number="",
                                               shipping_courier="", email="kunde@client.example")
        self.folder = Path(self.root) / "orders" / "digikey" / "DK-777"
        self.folder.mkdir(parents=True)
        (self.folder / "Rechnung.pdf").write_bytes(b"%PDF-1.4")
        self.base = f"/admin/sales/salesorder/{self.order.pk}"

    def _files(self):
        return sorted(p.name for p in self.folder.iterdir())

    def test_view_only_refused_and_nothing_changes(self):
        from shipping.models import Invoice, Shipment
        self.client.force_login(accountant())
        posts = [
            ("doc/delete/", {"filename": "Rechnung.pdf"}),
            ("upload-docs/", {"documents": SimpleUploadedFile("Neu.pdf", b"%PDF-1.4 neu")}),
            ("sync-inventory/", {}),
            ("digikey-fetch-tracking/", {}),
            ("push-to-shipping/", {}),
            ("doc/invoice-save/", {}),
            ("eu-invoice/generate/", {}),
            ("doc/proforma/?action=save", {}),
            ("digikey-messages/T-1/reply/", {}),
            ("digikey-messages/new-topic/", {}),
        ]
        with mock.patch("bots.services.digikey.get_marketplace_token", side_effect=AssertionError("DigiKey")), \
                pdf_generators(mock.Mock(side_effect=AssertionError("PDF"))):
            for path, data in posts:
                r = self.client.post(f"{self.base}/{path}", data)
                self.assertEqual(r.status_code, 403, path)
                self.assertEqual(r.json()["error"], NO_ORDER_RIGHT, path)
        self.assertEqual(self._files(), ["Rechnung.pdf"])
        self.order.refresh_from_db()
        self.assertEqual((self.order.tracking_number, self.order.eu_invoice_number), ("", None))
        self.assertFalse(Shipment.objects.exists())
        self.assertFalse(Invoice.objects.exists())

    def test_get_changes_nothing(self):
        """💾 інвойс і 💾 PDF у папку замовлення раніше працювали GET — тепер лише POST (і решта дій теж)."""
        from shipping.models import Invoice
        for user in (accountant(), person("olga")):
            self.client.force_login(user)
            for path in ("doc/invoice-save/", "doc/proforma/?action=save", "doc/customs/?action=save",
                         "doc/delete/", "upload-docs/", "push-to-shipping/", "eu-invoice/generate/",
                         "digikey-fetch-tracking/", "digikey-messages/new-topic/"):
                r = self.client.get(f"{self.base}/{path}")
                self.assertEqual(r.status_code, 405, path)
                self.assertEqual(r["Allow"], "POST", path)
            self.assertEqual(self.client.put(f"{self.base}/sync-inventory/").status_code, 405)
        self.assertFalse(Invoice.objects.exists())
        self.assertEqual(self._files(), ["Rechnung.pdf"])

    def test_view_only_still_reads(self):
        self.client.force_login(accountant())
        self.assertEqual(self.client.get(f"{self.base}/sync-inventory/").status_code, 200)   # перегляд розбіжностей
        with pdf_generators():
            r = self.client.get(f"{self.base}/doc/proforma/")
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "application/pdf"))
        r = self.client.get(f"{self.base}/doc/list/")
        self.assertContains(r, "Rechnung.pdf")
        self.assertNotContains(r, "data-del-url")
        r = self.client.get(f"{self.base}/card-expand/")
        self.assertContains(r, "Rechnung.pdf")
        self.assertNotContains(r, "_cexUpload")
        self.assertEqual(self._files(), ["Rechnung.pdf"])

    def test_editor_still_works(self):
        from shipping.models import Invoice
        self.client.force_login(person("olga"))
        with pdf_generators():
            r = self.client.post(f"{self.base}/doc/proforma/?action=save")
        self.assertTrue(r.json()["ok"])
        r = self.client.post(f"{self.base}/upload-docs/", {"documents": SimpleUploadedFile("Neu.pdf", b"%PDF neu")})
        self.assertEqual(r.json()["results"][0]["status"], "saved")
        r = self.client.post(f"{self.base}/doc/delete/", {"filename": "Rechnung.pdf"})
        self.assertEqual(r.json(), {"ok": True, "deleted": "Rechnung.pdf"})
        self.assertEqual(self._files(), ["Neu.pdf", "ProformaInvoice-DK-777.pdf"])
        r = self.client.get(f"{self.base}/doc/list/")
        self.assertContains(r, "data-del-url")

        Invoice.objects.create(digikey_order_no="DK-777", sales_order=self.order, invoice_number="7001",
                               order_date=datetime.date(2026, 10, 1), shipped_to_company="Client GmbH")
        r = self.client.post(f"{self.base}/doc/invoice-save/")
        self.assertTrue(r.json()["already_exists"])

        with mock.patch("bots.services.digikey.get_marketplace_token", return_value="tok"), \
                mock.patch("bots.services.digikey._fetch_marketplace_order", return_value={}), \
                mock.patch("bots.services.digikey._extract_tracking", return_value="1Z999"), \
                mock.patch("bots.services.digikey._extract_carrier", return_value="UPS"):
            r = self.client.post(f"{self.base}/digikey-fetch-tracking/")
        self.assertEqual(r.json()["tracking_number"], "1Z999")
        with mock.patch("shipping.services.import_tracking.ensure_shipment_for_order",
                        return_value=(types.SimpleNamespace(pk=5), True)) as ensure:
            r = self.client.post(f"{self.base}/push-to-shipping/")
        self.assertTrue(r.json()["ok"])
        ensure.assert_called_once()
        self.assertEqual(self.client.post(f"{self.base}/sync-inventory/").status_code, 200)
        r = self.client.post(f"{self.base}/digikey-messages/new-topic/", "{}", content_type="application/json")
        self.assertEqual(r.status_code, 400)                              # право є — далі звичайна перевірка
        self.assertIn("content", r.json()["error"])

    def test_view_only_card_has_no_change_buttons(self):
        markers = ('data-del-url=', 'id="docFiles"', 'onclick="_docSave(this)"', 'id="sv-sync-inv-btn"',
                   'id="dk-fetch-tracking-btn"', 'id="dkm-reply-ta"')
        self.client.force_login(accountant())
        r = self.client.get(f"{self.base}/change/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("Rechnung.pdf", html)                               # документи видно, ⬇️ — теж
        self.assertIn("data-dl-url=", html)
        for marker in markers:
            self.assertNotIn(marker, html, marker)
        self.client.force_login(person("olga"))
        html = self.client.get(f"{self.base}/change/").content.decode()
        for marker in markers:
            self.assertIn(marker, html, marker)

    def test_document_names_are_escaped(self):
        (self.folder / "R&D 'Notiz'.pdf").write_bytes(b"%PDF-1.4")         # < > " у назвах Windows не дозволяє
        self.client.force_login(person("olga"))
        html = self.client.get(f"{self.base}/doc/list/").content.decode()
        self.assertIn("R&amp;D &#x27;Notiz&#x27;.pdf", html)
        self.assertNotIn("R&D 'Notiz'", html)


class CrmTimelineTests(_Base):
    """Хронологія клієнта /crm/customer/<pk>/timeline/: розділ CRM; тема й текст листа — лише з власних скриньок."""

    def setUp(self):
        super().setUp()
        from crm.models import Customer, CustomerTimeline
        self.customer = Customer.objects.create(name="Client GmbH", email=CLIENT)
        self.olga, self.petro = person("olga"), person("petro")
        self.uid = 0
        olga_mail = self.mail(self.account(self.olga, "olga@firma.example"), "Angebot Olga")
        petro_mail = self.mail(self.account(self.petro, "petro@firma.example"), "Geheim Petro")
        CustomerTimeline.objects.filter(customer=self.customer).delete()   # події листів — лише ці (без сигналу)

        def event(**kw):
            CustomerTimeline.objects.create(customer=self.customer, **kw)
        event(event_type="email_in", title="Angebot Olga", body="Text von Olga", ai_summary="AI Olga",
              email_message=olga_mail, user=self.olga)
        event(event_type="email_in", title="Geheim Petro", body="Text von Petro", ai_summary="AI Petro",
              email_message=petro_mail, user=self.petro)
        event(event_type="email_out", title="Aus CRM von Olga", body="CRM-Text Olga", user=self.olga)
        event(event_type="note", title="Rückruf", body="Bitte anrufen", user=self.petro)
        self.base = f"/crm/customer/{self.customer.pk}"
        self.url = f"{self.base}/timeline/"

    def account(self, user, address):
        from email_assistant.models import EmailAccount, EmailSettings
        acc = EmailAccount.objects.create(user=user, email_address=address, imap_username=address,
                                          imap_password="x", smtp_username=address, smtp_password="x")
        es = EmailSettings.get_for_user(user)
        es.deadline_detection = es.telegram_notify_new = es.mark_read_on_server = False
        es.save()
        return acc

    def mail(self, acc, subject):
        from email_assistant.models import EmailMessage
        self.uid += 1
        return EmailMessage.objects.create(
            account=acc, imap_uid=self.uid, folder=EmailMessage.FOLDER_INBOX, subject=subject, from_email=CLIENT,
            to_emails=[acc.email_address], body_text=subject, sent_at=timezone.now())

    def events(self, user):
        self.client.force_login(user)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        return r.json()["events"], r.content.decode()

    def test_mail_text_only_from_own_mailboxes(self):
        events, raw = self.events(self.olga)
        shown = {e["title"]: e for e in events if not e["private"]}
        self.assertEqual(set(shown), {"Angebot Olga", "Aus CRM von Olga", "Rückruf"})
        self.assertEqual((shown["Angebot Olga"]["body"], shown["Angebot Olga"]["ai_summary"]),
                         ("Text von Olga", "AI Olga"))
        self.assertTrue(shown["Angebot Olga"]["email_url"])
        hidden = [e for e in events if e["private"]]
        self.assertEqual(len(hidden), 1)
        self.assertEqual((hidden[0]["title"], hidden[0]["ai_summary"], hidden[0]["email_url"], hidden[0]["thread"]),
                         ("Лист з іншої скриньки", "", "", ""))
        for secret in ("Geheim Petro", "Text von Petro", "AI Petro"):
            self.assertNotIn(secret, raw)

        events, raw = self.events(self.petro)
        self.assertEqual({e["title"] for e in events if not e["private"]}, {"Geheim Petro", "Rückruf"})
        for secret in ("Angebot Olga", "Text von Olga", "AI Olga", "CRM-Text Olga"):
            self.assertNotIn(secret, raw)

    def test_view_role_sees_notes_but_no_mail_text(self):
        events, raw = self.events(person("ai", "ai"))                     # CRM — лише перегляд, без скриньок
        self.assertEqual([e["title"] for e in events if not e["private"]], ["Rückruf"])
        self.assertEqual(sum(e["private"] for e in events), 3)
        self.assertNotIn("Text von", raw)

    def test_without_crm_module_refused(self):
        self.client.force_login(person("wanda", "warehouse"))                # склад, без CRM
        for path in ("timeline/", "ai-analysis/", "mailbox-emails/", "ai-strategy/", "ai-strategy-advisor/"):
            r = self.client.get(f"{self.base}/{path}")
            self.assertEqual(r.status_code, 403, path)
            self.assertEqual(r.json()["error"], "Немає доступу до клієнтів CRM")
        for path in ("timeline/", "ai-email/", "reset-ai/", "send-email/", "ai-strategy/apply/"):
            r = self.client.post(f"{self.base}/{path}", "{}", content_type="application/json")
            self.assertEqual(r.status_code, 403, path)
        self.assertEqual(self.client.get("/crm/customer/999999/timeline/").status_code, 403)

    def test_notes_need_change_right(self):
        from crm.models import CustomerTimeline
        before = CustomerTimeline.objects.count()
        self.client.force_login(person("ai", "ai"))
        r = self.client.post(self.url, json.dumps({"event_type": "note", "title": "Hack"}),
                             content_type="application/json")
        self.assertEqual((r.status_code, r.json()["error"]), (403, "Немає права змінювати клієнтів"))
        self.assertEqual(CustomerTimeline.objects.count(), before)
        self.client.force_login(self.olga)
        r = self.client.post(self.url, json.dumps({"event_type": "email_in", "title": "Gefälscht"}),
                             content_type="application/json")
        self.assertEqual(r.json(), {"ok": True})
        event = CustomerTimeline.objects.get(title="Gefälscht")
        self.assertEqual((event.event_type, event.user), ("note", self.olga))   # лист вручну не підробити

    def test_card_hides_note_bar_for_view_only(self):
        change = f"/admin/crm/customer/{self.customer.pk}/change/"
        self.client.force_login(person("ai", "ai"))
        r = self.client.get(change)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'id="note-text"')
        self.assertNotContains(r, 'id="btn-send-email"')
        self.assertContains(r, "const CRM_CAN_ASSIGN = false;")
        self.client.force_login(self.olga)
        r = self.client.get(change)
        self.assertContains(r, 'id="note-text"')
        self.assertContains(r, 'id="btn-send-email"')
        self.assertContains(r, "const CRM_CAN_ASSIGN = true;")


class CrmWriteTests(_Base):
    """Лист з картки клієнта, «Застосувати» стратегію AI, скинути пам'ять AI."""

    def setUp(self):
        super().setUp()
        from crm.models import Customer
        notification_settings()
        self.customer = Customer.objects.create(name="Client GmbH", email=CLIENT)
        self.base = f"/crm/customer/{self.customer.pk}"
        smtp = mock.patch("django.core.mail.get_connection", side_effect=locmem)
        smtp.start()
        self.addCleanup(smtp.stop)

    def _send(self, to, cc=""):
        return self.client.post(f"{self.base}/send-email/", json.dumps(
            {"to": to, "cc": cc, "subject": "Angebot", "body": "<p>Hallo</p>", "body_text": "Hallo"}),
            content_type="application/json")

    def test_view_only_cannot_send(self):
        from crm.models import CustomerTimeline
        self.client.force_login(person("ai", "ai"))
        r = self._send(CLIENT)
        self.assertEqual((r.status_code, r.json()["error"]), (403, "Немає права змінювати клієнтів"))
        self.assertEqual(mail.outbox, [])
        self.assertFalse(CustomerTimeline.objects.filter(event_type="email_out").exists())

    def test_without_mail_module_only_customer_addresses(self):
        from sales.models import SalesOrder
        SalesOrder.objects.create(source="manual", order_number="A-1", customer_key=self.customer.external_key,
                                  email=CLIENT, ship_email="lager@client.example")
        clerk = person("ivan", "readonly", allowed_modules=["crm"], module_operations={"crm": ["view", "change"]})
        self.client.force_login(clerk)
        for to, cc in (("fremd@evil.example", ""), (CLIENT, "kollege@other.example")):
            r = self._send(to, cc)
            self.assertEqual(r.status_code, 403, to)
            self.assertIn("evil.example" if to != CLIENT else "other.example", r.json()["error"])
        self.assertEqual(mail.outbox, [])
        r = self._send(CLIENT.upper(), cc="lager@client.example, ivan@x.y")   # адреси клієнта й своя
        self.assertEqual(r.json()["ok"], True)
        self.assertEqual(len(mail.outbox), 1)

    def test_with_mail_module_sends_anywhere(self):
        self.client.force_login(person("olga"))                              # менеджер: пошта Minerva є
        r = self._send("kollege@partner.example")
        self.assertEqual(r.json()["ok"], True)
        self.assertEqual(mail.outbox[0].to, ["kollege@partner.example"])

    def test_apply_strategy_needs_change_and_strategy_rights(self):
        from strategy.models import CustomerStrategy
        body = json.dumps({"strategy_name": "Test", "behavior_type": "nurturing",
                           "steps": [{"step_type": "email", "title": "Hallo", "delay_days": 0}]})
        url = f"{self.base}/ai-strategy/apply/"
        for user in (person("ai", "ai"),
                     person("ivan", "readonly", allowed_modules=["crm"], module_operations={"crm": ["view", "change"]})):
            self.client.force_login(user)
            self.assertEqual(self.client.post(url, body, content_type="application/json").status_code, 403)
        self.assertFalse(CustomerStrategy.objects.exists())
        self.client.force_login(person("olga"))
        r = self.client.post(url, body, content_type="application/json")
        self.assertTrue(r.json()["ok"])
        self.assertEqual(CustomerStrategy.objects.filter(customer=self.customer).count(), 1)

    def test_reset_ai_is_own_history_and_needs_crm(self):
        patch, service = ai_service()
        with patch:
            self.client.force_login(person("wanda", "warehouse"))
            self.assertEqual(self.client.post(f"{self.base}/reset-ai/").status_code, 403)
            service.reset_conversation.assert_not_called()
            ai = person("ai", "ai")                                          # перегляд CRM — свою пам'ять AI можна
            self.client.force_login(ai)
            self.assertEqual(self.client.post(f"{self.base}/reset-ai/").json(), {"ok": True})
        self.assertEqual({c.args[0].user_id for c in service.reset_conversation.call_args_list}, {ai.pk})


class InventoryActionTests(_Base):
    """Дії списку рухів складу й товарів, що змінюють дані, — лише з правом змінювати."""

    TX_ACTIONS = {"mark_as_reserved", "release_reservations", "mark_as_incoming", "mark_as_adjustment",
                  "fix_outgoing_sign"}
    PRODUCT_ACTIONS = {"action_set_category", "action_set_lifecycle", "action_shared_media", "action_base_from_dk",
                       "action_sale_price_from_base", "bulk_sync_digikey_attrs"}
    PAGES = {"action_base_editor", "action_base_to_home", "action_media_links", "action_dk_prices",
             "action_dk_from_stock"}                                     # переходи на сторінки — ті перевіряють самі

    def actions(self, model, user):
        request = RequestFactory().get("/admin/")
        request.user = user
        return set(admin.site._registry[model].get_actions(request))

    def test_list_actions_follow_rights(self):
        from inventory.models import InventoryTransaction, Product
        bea = accountant()                                                # склад — перегляд і експорт
        self.assertFalse(self.TX_ACTIONS & self.actions(InventoryTransaction, bea))
        got = self.actions(Product, bea)
        self.assertFalse(self.PRODUCT_ACTIONS & got)
        self.assertTrue(self.PAGES <= got)
        wanda = person("wanda", "warehouse")                              # склад — змінювати
        self.assertTrue(self.TX_ACTIONS <= self.actions(InventoryTransaction, wanda))
        self.assertTrue(self.PRODUCT_ACTIONS <= self.actions(Product, wanda))

    def test_view_only_post_changes_nothing(self):
        from inventory.models import InventoryTransaction, Location, Product
        TX = InventoryTransaction.TxType
        product = Product.objects.create(sku="FT220407", name="Filter")
        location = Location.objects.create(code="MAIN", name="Lager", location_type="other")
        tx = InventoryTransaction.objects.create(tx_type=TX.INCOMING, qty=Decimal("5"), product=product,
                                                 location=location, external_key=f"test:in:{uuid.uuid4()}")
        moves = {"action": "mark_as_reserved", "_selected_action": [tx.pk], "index": 0}
        self.client.force_login(accountant())
        self.client.post("/admin/inventory/inventorytransaction/", moves)
        tx.refresh_from_db()
        self.assertEqual((tx.tx_type, tx.qty), (TX.INCOMING, Decimal("5")))
        self.client.force_login(person("wanda", "warehouse"))
        self.client.post("/admin/inventory/inventorytransaction/", moves)
        tx.refresh_from_db()
        self.assertEqual((tx.tx_type, tx.qty), (TX.RESERVED, Decimal("-5")))


class DanteHoursTests(_Base):
    """«⏱ Час askDANTE» у картках клієнта й проєкту (worktime.panels)."""

    def setUp(self):
        super().setUp()
        from crm.models import Customer
        from projects.models import Project
        from worktime import mapping
        from worktime.models import DanteProject, DanteUser, TimeRecord
        self.customer = Customer.objects.create(name="Client GmbH", email=CLIENT)
        project = Project.objects.create(name="Antenne", customer=self.customer)
        self.olga, self.petro, self.boss = person("olga"), person("petro"), person("boss", "admin")
        dante_project = DanteProject.objects.create(dante_id="363", name="Antenne", crm_customer=self.customer)
        mapping.link(project, dante_project)
        for n, (user, text) in enumerate(((self.olga, "Olga: Angebot geschrieben"),
                                          (self.petro, "Petro: E-Mails Geheimprojekt"))):
            du = DanteUser.objects.create(dante_id=str(100 + n), first_name=user.username.title(), user=user)
            TimeRecord.objects.create(dante_id=f"r{n}", dante_user=du, person_id=du.dante_id,
                                      day=datetime.date(2026, 10, 1 + n), duration=60, dante_project=dante_project,
                                      activity_name="Vertrieb", description=text, hourly_rate=Decimal("80"))
        self.cards = (f"/admin/crm/customer/{self.customer.pk}/change/",
                      f"/admin/projects/project/{project.pk}/change/")

    def card(self, user, url):
        self.client.force_login(user)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, url)
        return r.content.decode()

    def test_own_description_only_and_cost_for_admins(self):
        for url in self.cards:
            html = self.card(self.olga, url)
            self.assertIn("Olga: Angebot geschrieben", html)
            self.assertNotIn("Petro: E-Mails Geheimprojekt", html)
            self.assertIn("Vertrieb", html)                                  # вид роботи й години — усім
            self.assertNotIn("за ставками askDANTE", html)
            html = self.card(self.boss, url)
            self.assertIn("за ставками askDANTE", html)
            self.assertNotIn("Petro: E-Mails Geheimprojekt", html)           # опис — лише автору, і адміністратору

    def test_without_worktime_module_no_block(self):
        viewer = person("vera", "readonly", allowed_modules=["crm", "projects"])
        for url in self.cards:
            html = self.card(viewer, url)
            self.assertNotIn("⏱ Час askDANTE", html)
            self.assertNotIn("Olga: Angebot", html)
            self.assertNotIn("Petro: E-Mails", html)

    def test_panel_needs_a_viewer(self):
        from worktime import panels
        self.assertEqual(panels.customer_panel(self.customer, None), "")
        self.assertIn("2:00", panels.customer_panel(self.customer, self.olga))
