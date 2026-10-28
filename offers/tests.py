"""Angebote: python manage.py test offers --settings=tabele.settings_test"""
import io
import json
import shutil
import tempfile
from datetime import datetime, timezone as dt_tz
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from crm.models import Customer
from email_assistant.models import EmailAccount, EmailSettings, EmailThread
from email_assistant.sync import save_message
from inventory.models import Product
from offers import pricing, services
from offers.models import Offer, OfferLine, OfferRevision
from sales.models import SalesOrder, SalesOrderLine

ME = "sales@sevskiy.de"
D = Decimal


def _all_text(path) -> str:
    """Увесь текст DOCX, включно з вкладеними таблицями бланка."""
    import re
    import zipfile
    return " ".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", zipfile.ZipFile(path).read("word/document.xml").decode()))


class OffersBase(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        self.media = tempfile.mkdtemp()
        self._media = override_settings(MEDIA_ROOT=self.media)
        self._media.enable()
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.default_currency = "EUR"
        s.save()
        self.user = User.objects.create_superuser("slava", "s@x.y", "p")
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(name="Alex Buyer", company="Client GmbH",
                                                email="buyer@client.example", addr_street="Hauptstr. 1",
                                                addr_zip="10115", addr_city="Berlin", country="DE")
        self.ant = Product.objects.create(sku="ANT-100", name="Antenna 2.4 GHz", sale_price=D("12.50"),
                                          purchase_price=D("6.00"))
        self.cab = Product.objects.create(sku="CAB-7", name="RF Cable 0.5 m", sale_price=D("3.10"))

    def tearDown(self):
        self._media.disable()
        shutil.rmtree(self.media, ignore_errors=True)


class PricingTests(OffersBase):
    def test_sources_in_order(self):
        from shop.models import Shop, ShopListing, ShopPriceTier, ShopSettings
        shop = Shop.objects.create(name="Website", slug="webshop", is_default=True, currency="EUR")
        listing = ShopListing.objects.create(shop=shop, product=self.ant, price=D("10.00"))
        ShopPriceTier.objects.create(listing=listing, min_qty=100, unit_price=D("8.00"))
        q = pricing.quote_price(self.ant, 150, "EUR")
        self.assertEqual((q.unit_price, q.source), (D("8.0000"), OfferLine.SHOP))
        self.assertIn("від 100 шт", q.note)

        q = pricing.quote_price(self.cab, 5, "EUR")                     # немає позиції магазину
        self.assertEqual((q.unit_price, q.source), (D("3.1000"), OfferLine.SALE_PRICE))

        chip = Product.objects.create(sku="IC-55", name="Chip", base_price_currency="USD",
                                      base_prices=[{"min_qty": 1, "unit_price": "2.00"},
                                                   {"min_qty": 500, "unit_price": "1.50"}])
        q = pricing.quote_price(chip, 600, "EUR")
        self.assertEqual(q.source, OfferLine.MANUAL)                       # курсу USD ще немає
        self.assertIn("немає курсу USD→EUR", q.note)
        ss = ShopSettings.get()
        ss.fx_rates = {"USD": "0.92"}
        ss.save()
        q = pricing.quote_price(chip, 600, "EUR")
        self.assertEqual((q.unit_price, q.source), (D("1.3800"), OfferLine.BASE))
        self.assertIn("від 500 шт", q.note)
        nothing = Product.objects.create(sku="X-1", name="No price")
        self.assertIsNone(pricing.quote_price(nothing, 1, "EUR").unit_price)
        self.assertIsNone(pricing.quote_price(self.cab, 1, "USD").unit_price)   # інша валюта — без вигадок

    def test_totals_are_exact(self):
        offer = services.new_offer(customer=self.customer, user=self.user, shipping_cost=D("15.00"))
        services.add_line(offer, product=self.ant, qty=3, unit_price=D("12.345"), discount_percent=D("10"))
        services.add_line(offer, sku="SERVICE", description="Kalibrierung", qty=1, unit_price=D("99.99"))
        offer.refresh_from_db()
        # 3 × 12.345 = 37.035 − 10 % = 33.3315 → 33.33; + 99.99 + 15.00 = 148.32; ПДВ 19 % = 28.18
        self.assertEqual([l.line_total for l in offer.lines.all()], [D("33.33"), D("99.99")])
        self.assertEqual((offer.net_total, offer.vat_total, offer.gross_total), (D("148.32"), D("28.18"), D("176.50")))


class LifecycleTests(OffersBase):
    def test_number_document_lock_and_new_version(self):
        year = timezone.localdate().year
        offer = services.new_offer(customer=self.customer, user=self.user)
        self.assertEqual(offer.number, f"A-{year}-001")
        self.assertEqual(services.new_offer(user=self.user).number, f"A-{year}-002")
        self.assertIn("Hauptstr. 1", offer.billing_address)
        services.add_line(offer, product=self.ant, qty=10)
        rev = services.generate_document(offer, self.user)
        self.assertTrue(rev.docx_file)
        self.assertEqual(rev.snapshot["lines"][0]["unit_price"], "12.5000")
        services.mark_sent(rev, "<m1@x>")
        offer.refresh_from_db()
        self.assertTrue(offer.is_locked)
        with self.assertRaises(services.OfferError):
            services.add_line(offer, product=self.cab)
        self.ant.sale_price = D("99")                                         # каталог змінився після відправки
        self.ant.save()
        self.assertEqual(services.generate_document(offer).snapshot["lines"][0]["unit_price"], "12.5000")
        services.new_version(offer, self.user)
        self.assertEqual((offer.revision, offer.status), (2, Offer.DRAFT))
        services.add_line(offer, product=self.cab, qty=2)
        rev2 = services.generate_document(offer, self.user)
        self.assertEqual((rev2.revision, len(rev2.snapshot["lines"])), (2, 2))
        rev.refresh_from_db()
        self.assertEqual((len(rev.snapshot["lines"]), rev.sent_at is not None), (1, True))
        self.assertEqual(rev2.file_name, f"Angebot_{offer.number}-V2")

    def test_document_content(self):
        from docx import Document
        offer = services.new_offer(customer=self.customer, user=self.user, shipping_cost=D("1000"))
        services.add_line(offer, product=self.ant, qty=100, unit_price=D("12.5"), lead_time="3 Wochen")
        services.add_line(offer, sku="X", description="Sonderanfertigung", qty=1)          # без ціни
        rev = services.generate_document(offer)
        text = "\n".join(p.text for p in Document(rev.docx_file.path).paragraphs)
        cells = "\n".join(c.text for t in Document(rev.docx_file.path).tables for r in t.rows for c in r.cells)
        self.assertIn(f"Angebotsnummer: {offer.number}", text)                # бланк Commercial Invoice
        self.assertIn("Kunde:", cells)
        self.assertIn("Anbieter:", _all_text(rev.docx_file.path))          # блок продавця — вкладена таблиця
        self.assertIn("Summe netto: 2 250,00 EUR", text)                     # 1 250 + доставка 1 000
        self.assertIn("Gesamtbetrag: 2 677,50 EUR", text)
        self.assertIn("ANT-100", cells)
        self.assertIn("auf Anfrage", cells)
        self.assertIn("Lieferzeit: 3 Wochen", cells)
        self.assertNotIn("{{", text + cells)

    def test_accept_creates_order(self):
        offer = services.new_offer(customer=self.customer, user=self.user)
        services.add_line(offer, product=self.ant, qty=4, unit_price=D("10"), discount_percent=D("5"))
        services.add_line(offer, sku="SVC", description="Service", qty=1, unit_price=D("50"))
        with self.assertRaises(services.OfferError):
            services.accept(offer)                                            # ще не надіслано
        services.mark_sent(services.generate_document(offer))
        order, skipped = services.accept(Offer.objects.get(pk=offer.pk), self.user)
        self.assertEqual((order.source, order.order_number, skipped), ("angebot", offer.number, [2]))
        line = SalesOrderLine.objects.get(order=order)
        self.assertEqual((line.qty, line.total_price, line.unit_price, line.currency),
                         (D("4.000"), D("38.00"), D("9.5000"), "EUR"))
        offer.refresh_from_db()
        self.assertEqual((offer.status, offer.order), (Offer.ACCEPTED, order))
        self.assertEqual(services.accept(offer)[0], order)                   # повтор — без другого замовлення
        import os
        self.assertTrue(os.path.exists(os.path.join(self.media, "orders", "angebot", offer.number,
                                                    f"Angebot_{offer.number}.docx")))

    def test_invalid_status_transition(self):
        offer = services.new_offer(user=self.user)
        with self.assertRaises(services.OfferError):
            services.set_status(offer, Offer.REJECTED)
        services.set_status(offer, Offer.REVIEW)
        services.set_status(offer, Offer.APPROVED)
        self.assertEqual(offer.status, Offer.APPROVED)


class FromMailAndQuoteTests(OffersBase):
    def setUp(self):
        super().setUp()
        self.account = EmailAccount.objects.create(
            user=self.user, email_address=ME, imap_username=ME, imap_password="x",
            smtp_username=ME, smtp_password="x", imap_folder_inbox="INBOX")
        es = EmailSettings.get_for_user(self.user)
        es.deadline_detection = es.telegram_notify_new = es.mark_read_on_server = False
        es.save()

    def mail(self, subject, body, frm="buyer@client.example"):
        return save_message(self.account, {
            "uid": 1, "message_id": "<q1@client.example>", "in_reply_to": "", "references": [],
            "subject": subject, "from_email": frm, "from_name": "Alex Buyer", "to_emails": [ME], "cc_emails": [],
            "body_text": body, "body_html": "", "attachments": [], "is_read": False,
            "sent_at": datetime(2026, 10, 1, 9, 0, tzinfo=dt_tz.utc)}, "INBOX")

    def test_detect_items(self):
        found = services.detect_items("Please quote 500 pcs ANT-100\nCAB-7 x 20\nalso IC-99999 (unknown)\nant-100 again")
        self.assertEqual([(p.sku, q) for p, q in found], [("ANT-100", D("500")), ("CAB-7", D("20"))])
        self.assertEqual([(p.sku, q) for p, q in services.detect_items("Bitte Angebot für CAB-7")], [("CAB-7", None)])

    def test_from_thread_button(self):
        from projects import services as project_services
        m = self.mail("Anfrage Antennen", "Hello,\nplease quote 500 pcs ANT-100 and CAB-7 x 20.\nThanks")
        project = project_services.create_project(name="Antennen", user=self.user, threads=[m.thread])
        r = self.client.post(f"/offers/api/from-thread/{m.thread_id}/")
        d = r.json()
        self.assertTrue(d["ok"])
        offer = Offer.objects.get(pk=d["id"])
        self.assertEqual((offer.customer, offer.project, offer.source_message), (self.customer, project, m))
        self.assertEqual([(l.sku, l.qty, l.unit_price) for l in offer.lines.all()],
                         [("ANT-100", D("500.000"), D("12.5000")), ("CAB-7", D("20.000"), D("3.1000"))])
        self.assertEqual(offer.contact_email, "buyer@client.example")
        pv = self.client.get(f"/email/thread/{m.thread_id}/preview/")
        self.assertContains(pv, "pvOfferFromThread")
        other = User.objects.create_user("bob", "b@x.y", "p", is_staff=True)
        self.client.force_login(other)
        self.assertEqual(self.client.post(f"/offers/api/from-thread/{m.thread_id}/").status_code, 404)

    def test_from_quote_action(self):
        quote = SalesOrder.objects.create(source="webshop", order_number="Q-77", document_type="QUOTE",
                                          email="buyer@client.example", customer_key=self.customer.external_key,
                                          affects_stock=False)
        SalesOrderLine.objects.create(order=quote, product=self.ant, qty=D("50"), unit_price=D("11"), currency="EUR")
        r = self.client.post("/admin/sales/salesorder/", {"action": "create_offer_from_quote",
                                                          "_selected_action": [quote.pk]})
        offer = Offer.objects.get(source_quote=quote)
        self.assertRedirects(r, f"/admin/offers/offer/{offer.pk}/change/", fetch_redirect_response=False)
        line = offer.lines.get()
        self.assertEqual((line.qty, line.unit_price), (D("50.000"), D("12.5000")))   # поточна ціна Minerva
        self.assertIn("у запиті: 11 EUR", line.price_note)

    def test_send_by_mail_marks_revision_sent(self):
        offer = services.new_offer(customer=self.customer, user=self.user, contact_name="Alex Buyer")
        services.add_line(offer, product=self.ant, qty=2)
        r = self.client.get("/email/compose/", {"offer": offer.pk})
        rev = OfferRevision.objects.get(offer=offer, revision=1)
        self.assertContains(r, f'name="att_ref" value="offer:{rev.pk}"')
        self.assertContains(r, 'value="buyer@client.example"')
        self.assertContains(r, f"Angebot {offer.number}")
        self.assertContains(r, "Sehr geehrte/r Alex Buyer")
        sent = {}

        def fake_send(client, **kw):
            sent.update(kw)
            return {"ok": True, "message_id": "<offer-mail@sevskiy.de>", "via": "test"}
        with patch("email_assistant.smtp_client.SMTPClient.send", fake_send):
            d = self.client.post("/email/send/", {"to": "buyer@client.example", "subject": f"Angebot {offer.number}",
                                                  "body": "anbei", "att_ref": [f"offer:{rev.pk}"]}).json()
        self.assertTrue(d["ok"], d)
        self.assertEqual(sent["attachments"][0]["name"], f"Angebot_{offer.number}.docx")
        rev.refresh_from_db()
        offer.refresh_from_db()
        self.assertEqual((rev.sent_message_id, offer.status), ("<offer-mail@sevskiy.de>", Offer.SENT))


class QuoteMailBase(OffersBase):
    """Скринька, клієнт, постачальник і помічник для листів — для тестів запитів пропозиції."""

    def setUp(self):
        super().setUp()
        from inventory.models import Supplier
        self.account = EmailAccount.objects.create(
            user=self.user, email_address=ME, imap_username=ME, imap_password="x",
            smtp_username=ME, smtp_password="x", imap_folder_inbox="INBOX", imap_folder_sent="Sent")
        es = EmailSettings.get_for_user(self.user)
        es.deadline_detection = es.telegram_notify_new = es.mark_read_on_server = False
        es.save()
        Supplier.objects.create(name="Parts Ltd", email="sales@parts.example")
        self._uid = 0

    def mail(self, subject, body, frm="buyer@client.example", folder="INBOX", to=(ME,), in_reply_to=""):
        self._uid += 1
        return save_message(self.account, {
            "uid": self._uid, "message_id": f"<qr{self._uid}@x.example>", "in_reply_to": in_reply_to,
            "references": [in_reply_to] if in_reply_to else [], "subject": subject, "from_email": frm,
            "from_name": "", "to_emails": list(to), "cc_emails": [], "body_text": body, "body_html": "",
            "attachments": [], "is_read": False, "sent_at": datetime(2026, 10, 1, 9, self._uid, tzinfo=dt_tz.utc)},
            folder)

    def qr(self, msg):
        from offers.models import QuoteRequest
        return QuoteRequest.objects.filter(message=msg).first()


class QuoteRequestTests(QuoteMailBase):
    """Запити пропозиції в пошті: розпізнавання, підказка в прев'ю, мітка й фільтр у списку, рішення людини."""

    def test_requests_in_several_languages(self):
        de = self.mail("Anfrage Antennen", "Guten Tag,\nbitte senden Sie uns Ihr Angebot für 500 Stück ANT-100.")
        en = self.mail("Inquiry", "Hello, could you please send us a quotation for CAB-7 x 20?", frm="ops@newco.example")
        uk = self.mail("Запит", "Просимо надіслати комерційну пропозицію на 50 шт ANT-100", frm="zakup@firma.example")
        price = self.mail("Frage", "Was kostet 200 Stk CAB-7?", frm="info@shop.example")
        for m in (de, en, uk, price):
            self.assertIsNotNone(self.qr(m), m.subject)
        self.assertEqual(self.qr(de).items, [{"product_id": self.ant.pk, "sku": "ANT-100", "qty": "500"}])
        self.assertIn("позиції: ANT-100 × 500", self.qr(de).reason)
        self.assertIn("артикули з кількістю й питання про ціну", self.qr(price).reason)

    def test_not_requests(self):
        thanks = self.mail("Re: Angebot", "Vielen Dank für Ihr Angebot, wir bestellen 500 Stück ANT-100.")
        supplier = self.mail("Unser Angebot", "Bitte beachten Sie unser Angebot für CAB-7.", frm="sales@parts.example")
        own = self.mail("Angebot A-2026-001", "Bitte finden Sie anbei unser Angebot.", frm=ME, folder="Sent",
                        to=("buyer@client.example",))
        news = save_message(self.account, {
            "uid": 900, "message_id": "<news@x.example>", "in_reply_to": "", "references": [], "subject": "Deals",
            "from_email": "news@shop.example", "from_name": "", "to_emails": [ME], "cc_emails": [],
            "body_text": "Request for quotation? Please quote us!", "body_html": "", "attachments": [],
            "is_read": False, "list_unsubscribe": True, "sent_at": datetime(2026, 10, 1, 8, 0, tzinfo=dt_tz.utc)},
            "INBOX")
        quoted = self.mail("Re: Anfrage", "Ok, passt so.\n\nAm 30.09.2026 schrieb Vertrieb:\n> Bitte Angebot für 500 Stück")
        chat = self.mail("Hallo", "Wie geht es Ihnen? Bis bald.")
        for m in (thanks, supplier, own, news, quoted, chat):
            self.assertIsNone(self.qr(m), m.subject)
        first = self.mail("Anfrage CAB", "Bitte Angebot für CAB-7 x 10")
        services.create_from_thread(first.thread, self.user)
        follow = self.mail("Re: Anfrage CAB", "Bitte auch ein Angebot für ANT-100", in_reply_to=first.message_id)
        self.assertEqual(follow.thread_id, first.thread_id)
        self.assertIsNone(self.qr(follow))                    # Angebot по гілці вже є — це переговори

    def test_preview_hint_list_tag_filter_and_dismiss(self):
        req = self.mail("Anfrage Antennen", "Bitte um ein Angebot für 500 Stück ANT-100")
        other = self.mail("Hallo", "Nur eine Info.")
        pv = self.client.get(f"/email/thread/{req.thread_id}/preview/")
        self.assertContains(pv, "Схоже на запит пропозиції")
        self.assertContains(pv, f"pvQuoteDismiss({self.qr(req).pk})")
        inbox = self.client.get("/email/")
        self.assertContains(inbox, "em-tag quote")
        only = self.client.get("/email/", {"quote": 1})
        self.assertContains(only, f'id="row-{req.pk}"')
        self.assertNotContains(only, f'id="row-{other.pk}"')
        r = self.client.post(f"/offers/api/quote-request/{self.qr(req).pk}/dismiss/")
        self.assertTrue(r.json()["ok"])
        self.assertEqual(self.qr(req).status, "dismissed")
        self.assertNotContains(self.client.get(f"/email/thread/{req.thread_id}/preview/"), "Схоже на запит пропозиції")
        self.assertNotContains(self.client.get("/email/", {"quote": 1}), f'id="row-{req.pk}"')
        from offers.detection import scan
        self.assertEqual(scan(req).status, "dismissed")       # рішення людини автоматика не перебиває
        stranger = User.objects.create_user("bob", "b@x.y", "p", is_staff=True)
        self.client.force_login(stranger)
        self.assertEqual(self.client.post(f"/offers/api/quote-request/{self.qr(req).pk}/dismiss/").status_code, 404)

    def test_angebot_from_thread_closes_request_and_backfill(self):
        from django.core.management import call_command
        from offers.models import QuoteRequest
        req = self.mail("Anfrage", "Please quote 500 pcs ANT-100")
        d = self.client.post(f"/offers/api/from-thread/{req.thread_id}/").json()
        self.assertEqual((self.qr(req).status, self.qr(req).offer_id), ("done", d["id"]))
        late = self.mail("RFQ", "RFQ: CAB-7 x 30", frm="ops@other.example")
        QuoteRequest.objects.filter(message=late).delete()     # як лист до появи розпізнавання
        out = io.StringIO()
        with patch("offers.detection.timezone.now", return_value=datetime(2026, 10, 15, tzinfo=dt_tz.utc)):
            call_command("detect_quote_requests", "--days", "30", stdout=out)
        self.assertIn("Знайдено запитів пропозиції: 1", out.getvalue())
        self.assertEqual(self.qr(late).status, "new")


class QuoteScanSettingsTests(QuoteMailBase):
    """«⚙️ Налаштування Angebote» → «Знайти запити в уже отриманій пошті»: глибина, фон, поле знову «—»."""

    def setUp(self):
        super().setUp()
        background = patch("offers.detection._start_background", lambda fn: fn())
        background.start()
        self.addCleanup(background.stop)

    def save_settings(self, depth):
        from offers.models import OfferSettings
        OfferSettings.get()
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(f"/admin/offers/offersettings/1/change/", {
                "number_prefix": "A", "validity_days": 30, "default_currency": "EUR", "default_vat_rate": "19.00",
                "default_language": "de", "email_subject": "Angebot {number}", "quote_scan_depth": depth})

    def test_scan_old_mail_by_depth_and_switch_off(self):
        from offers.models import OfferSettings, QuoteRequest
        recent = self.mail("Anfrage", "Bitte Angebot für 500 Stück ANT-100")
        old = save_message(self.account, {
            "uid": 777, "message_id": "<old@x.example>", "in_reply_to": "", "references": [], "subject": "RFQ CAB-7",
            "from_email": "ops@old.example", "from_name": "", "to_emails": [ME], "cc_emails": [],
            "body_text": "Please quote 30 pcs CAB-7", "body_html": "", "attachments": [], "is_read": True,
            "sent_at": datetime(2024, 3, 1, 9, 0, tzinfo=dt_tz.utc)}, "INBOX")
        QuoteRequest.objects.all().delete()                     # як пошта, отримана до появи розпізнавання
        r = self.save_settings(30)
        self.assertEqual(r.status_code, 302, r.content[:400])
        s = OfferSettings.get()
        self.assertEqual(s.quote_scan_depth, OfferSettings.SCAN_OFF)          # одноразово — знову «—»
        self.assertTrue(s.quote_scan_status.startswith("✅"), s.quote_scan_status)
        self.assertIn("30 днів", s.quote_scan_status)
        self.assertIn("знайдено нових запитів 1", s.quote_scan_status)
        self.assertIsNotNone(self.qr(recent))
        self.assertIsNone(self.qr(old))                                        # старіший за 30 днів
        self.save_settings(OfferSettings.SCAN_ALL)
        self.assertIsNotNone(self.qr(old))
        self.assertIn("Уся пошта", OfferSettings.get().quote_scan_status)
        page = self.client.get("/admin/offers/offersettings/1/change/")
        self.assertContains(page, "Знайти запити в уже отриманій пошті")
        self.assertContains(page, "📝 Відкриті запити: 2")

    def test_dismissed_requests_are_not_reopened(self):
        from offers.models import OfferSettings
        req = self.mail("Anfrage", "Bitte Angebot für 500 Stück ANT-100")
        self.client.post(f"/offers/api/quote-request/{self.qr(req).pk}/dismiss/")
        self.save_settings(OfferSettings.SCAN_ALL)
        self.assertEqual(self.qr(req).status, "dismissed")
        self.assertIn("знайдено нових запитів 0", OfferSettings.get().quote_scan_status)


class AdminTests(OffersBase):
    def test_add_with_inline_fills_catalog_price(self):
        from projects.models import Project
        project = Project.objects.create(name="Antennen", customer=self.customer)
        r = self.client.get("/admin/offers/offer/add/", {"project": project.pk})
        self.assertContains(r, "Client GmbH")                                  # клієнт із проєкту
        data = {
            "status": "draft", "customer": self.customer.pk, "contact_name": "Alex", "contact_email": "a@b.example",
            "billing_address": "x", "project": project.pk, "owner": self.user.pk, "language": "de",
            "currency": "EUR", "vat_rate": "19", "shipping_cost": "0", "valid_until": "2026-12-31",
            "intro_text": "", "payment_terms": "", "delivery_terms": "", "outro_text": "", "internal_notes": "",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-position": "1", "lines-0-product": self.ant.pk, "lines-0-sku": "", "lines-0-description": "",
            "lines-0-qty": "4", "lines-0-unit": "Stk", "lines-0-unit_price": "", "lines-0-discount_percent": "0",
            "lines-0-lead_time": "",
        }
        r = self.client.post("/admin/offers/offer/add/", data)
        self.assertEqual(r.status_code, 302, r.content[:800])
        offer = Offer.objects.get()
        line = offer.lines.get()
        self.assertEqual((line.sku, line.unit_price, line.price_source), ("ANT-100", D("12.5000"), "sale_price"))
        self.assertEqual(offer.gross_total, D("59.50"))
        page = self.client.get(f"/admin/offers/offer/{offer.pk}/change/")
        self.assertContains(page, "Сформувати документ")
        self.assertContains(page, "52.0 %")                                    # маржа (внутрішня)
        r = self.client.post(f"/admin/offers/offer/{offer.pk}/generate/")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(OfferRevision.objects.filter(offer=offer, revision=1).exists())
        proj = self.client.get(f"/admin/projects/project/{project.pk}/change/")
        self.assertContains(proj, offer.number)


class ForwardedQuoteRequestTests(QuoteMailBase):
    """Колега переслав запит клієнта (FW / WG): відправник і текст — з пересланого листа."""

    FW = ("Mit freundlichen Grüßen\nSergey\n\n-------- Ursprüngliche Nachricht --------\n"
          "Von: Denys Repetynskyi <{frm}>\nDatum: 28.10.26 14:23\nAn: \"Sergey\" <" + ME + ">\nBetreff: Filters\n\n"
          "Good afternoon, we'd like to place a larger order.\n"
          "Please provide pricing and production lead times for 35 units and 100 units of ANT-100.\n")

    def test_forwarded_request_is_found(self):
        m = self.mail("FW: Filters", self.FW.format(frm="procurement10@moduletp.example"), frm=ME)
        self.assertEqual(m.category, "internal")
        qr = self.qr(m)
        self.assertIsNotNone(qr)
        self.assertTrue(qr.reason.startswith("переслано від procurement10@moduletp.example"))
        self.assertIn("Please provide pricing", qr.reason)

    def test_forwarded_from_supplier_or_colleague_is_not(self):
        supplier = self.mail("FW: Offer", self.FW.format(frm="sales@parts.example"), frm=ME)
        colleague = self.mail("FW: intern", self.FW.format(frm=ME), frm=ME)
        plain = self.mail("Notiz", "Please provide pricing — intern, ohne Weiterleitung", frm=ME)
        for m in (supplier, colleague, plain):
            self.assertIsNone(self.qr(m), m.subject)

    def test_angebot_contact_and_customer_from_forwarded_sender(self):
        from crm.models import Customer
        buyer = Customer.objects.create(name="Denys", company="SL Tire", email="procurement10@moduletp.example")
        m = self.mail("FW: Filters", self.FW.format(frm="procurement10@moduletp.example"), frm=ME)
        offer = services.create_from_thread(m.thread, self.user)
        self.assertEqual((offer.contact_email, offer.contact_name, offer.customer),
                         ("procurement10@moduletp.example", "Denys", buyer))   # ім'я — з картки клієнта CRM


class QuickOfferTests(QuoteMailBase):
    """⚡ Легка форма Angebot: бланк Commercial Invoice, ступені (Staffelpreise), Deutsch + English, з листа."""

    def setUp(self):
        super().setUp()
        from shop.models import Shop, ShopListing, ShopPriceTier
        shop = Shop.objects.create(name="Website", slug="webshop", is_default=True, currency="EUR")
        listing = ShopListing.objects.create(shop=shop, product=self.ant, price=D("10.00"))
        ShopPriceTier.objects.create(listing=listing, min_qty=100, unit_price=D("8.00"))

    def test_parse_tiers(self):
        self.assertEqual(services.parse_tiers("35; 100"), [(D("35"), None), (D("100"), None)])
        self.assertEqual(services.parse_tiers("35, 100"), [(D("35"), None), (D("100"), None)])
        self.assertEqual(services.parse_tiers("35: 12,50; 100: 10,9"), [(D("35"), D("12.50")), (D("100"), D("10.9"))])
        self.assertEqual(services.parse_tiers(""), [])

    def test_tiers_price_each_quantity_and_document(self):
        from docx import Document
        offer = services.new_offer(customer=self.customer, user=self.user, language="de_en")
        offer = services.save_quick({"customer": self.customer, "language": "de_en"},
                                    [{"sku": "ANT-100", "qtys": "35; 100", "prices": ""},
                                     {"sku": "", "description": "Sonderanfertigung", "qtys": "1", "prices": "99,90"}],
                                    self.user, offer=offer)
        line = offer.lines.get(sku="ANT-100")
        self.assertEqual([(t["qty"], t["unit_price"]) for t in line.tiers], [("35", "10.0000"), ("100", "8.0000")])
        self.assertEqual((line.qty, line.unit_price, line.line_total), (D("35"), D("10.0000"), D("350.00")))
        offer.refresh_from_db()
        self.assertEqual(offer.net_total, D("449.90"))   # перша к-сть: 350 + 99,90
        rev = services.generate_document(offer)
        doc = Document(rev.docx_file.path)
        text = "\n".join(p.text for p in doc.paragraphs)
        cells = "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
        self.assertIn("Angebot / Quotation", text)                        # обома мовами
        self.assertIn("Menge / Qty", cells)
        self.assertIn("35 Stk\n100 Stk", cells)                           # ступені — рядками в комірці
        self.assertIn("8,00", cells)
        self.assertIn("Staffelpreise", text)
        self.assertIn("Tiered prices", text)
        self.assertIn("Sevskiy GmbH", _all_text(rev.docx_file.path))     # бланк Commercial Invoice
        self.assertIn("Kunde / Customer:", cells)
        self.assertIn("Client GmbH", cells)
        self.assertNotIn("{{", text + cells)
        self.assertNotIn("Ship to", cells)

    def test_prefill_from_forwarded_mail_and_create(self):
        from offers.models import QuoteRequest
        m = self.mail("FW: Filters", ForwardedQuoteRequestTests.FW.format(frm="procurement10@moduletp.example"), frm=ME)
        self.assertIsNotNone(self.qr(m))
        page = self.client.get(f"/offers/quick/?thread={m.thread_id}")
        self.assertEqual(page.status_code, 200)
        data = page.context["data"]
        self.assertEqual((data["language"], data["contact_email"]), ("en", "procurement10@moduletp.example"))
        self.assertEqual(data["lines"][0]["sku"], "ANT-100")
        self.assertEqual(data["lines"][0]["qtys"], "35; 100")             # «35 units and 100 units» → ступені
        import json
        r = self.client.post("/offers/quick/", {
            "thread": m.thread_id, "language": "en", "currency": "EUR", "valid_days": "30",
            "contact_name": "Denys", "contact_email": "procurement10@moduletp.example", "billing_address": "SL Tire",
            "lines_json": json.dumps(data["lines"])})
        offer = Offer.objects.latest("pk")
        self.assertRedirects(r, f"/offers/{offer.pk}/quick/", fetch_redirect_response=False)
        self.assertEqual((offer.language, offer.source_message_id, offer.lines.get().tiers[1]["qty"]),
                         ("en", m.pk, "100"))
        self.assertEqual(QuoteRequest.objects.get(message=m).status, QuoteRequest.DONE)
        self.assertTrue(offer.revisions.exists())                          # документ сформовано
        page = self.client.get(f"/offers/{offer.pk}/quick/")
        self.assertContains(page, "Перегенерувати")

    def test_price_hints_and_customer_api(self):
        d = self.client.get("/offers/api/price/?sku=ant-100&qtys=35;100&currency=EUR").json()
        self.assertEqual([t["unit_price"] for t in d["tiers"]], ["10", "8"])
        self.assertFalse(self.client.get("/offers/api/price/?sku=NOPE&qtys=1").json()["found"])
        c = self.client.get(f"/offers/api/customer/{self.customer.pk}/").json()
        self.assertIn("Hauptstr. 1", c["address"])

    def test_admin_tiers_field(self):
        offer = services.new_offer(customer=self.customer, user=self.user)
        line = services.add_line(offer, product=self.ant, qty=35)
        services.set_tiers(line, services.parse_tiers("35; 150"))
        line.save()
        self.assertEqual(services.tiers_text(line), "35: 10; 150: 8")
