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
        self.assertIn(f"Angebot {offer.number}", text)
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
