"""🧾 Рахунки (Rechnung): номер з нумерації магазину, адреса з сайту, ПДВ, тексти, документ на бланку CI, сторінки
/rechnungen/ з правами, лист з вкладенням `invoice:<pk>`.

python manage.py test sales.tests_invoices --settings=tabele.settings_test
"""
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from email_assistant.models import EmailAccount
from inventory.models import Product
from sales import invoice_service as svc
from sales.models import SalesOrder, SalesOrderLine
from sales.models_invoice import InvoiceSettings, SalesInvoice
from shop.models import Shop, ShopOrder

BILLING = {"company": "Fogra e.V.", "name": "Anna Muster", "street": "Streitfeldstr. 19", "zip": "81673",
           "city": "München", "country": "DE", "vat_id": "DE129514004", "email": "rechnung@fogra.example"}
MEDIA = Path(tempfile.mkdtemp(prefix="minerva-invoices-"))   # Path, як у settings (documents_list ділить шляхи)


def person(username, modules=("sales",), role="readonly"):
    user = User.objects.create_user(username, f"{username}@x.y", "p", is_staff=True)
    user.profile.role = role
    user.profile.allowed_modules = list(modules)
    user.profile.save()
    return User.objects.get(pk=user.pk)


def docx_text(data: bytes) -> str:
    from docx import Document
    doc = Document(BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for c in t._cells:
            parts.append(c.text)
    return "\n".join(parts)


@override_settings(MEDIA_ROOT=MEDIA)
class _Base(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.boss = User.objects.create_superuser("slava", "s@x.y", "p")
        self.shop = Shop.objects.create(name="sevskiy.de", slug="sevskiy_de", is_default=True)
        self.order = SalesOrder.objects.create(
            source="sevskiy_de", order_number="PO-31728-001", client="Fogra e.V.", contact_name="Anna Muster",
            email="einkauf@fogra.example", addr_street="Lager 1", addr_zip="63741", addr_city="Aschaffenburg",
            addr_country="DE", payment_method="prepayment", payment_status="unpaid", currency="EUR",
            shipping_cost=Decimal("12.90"))
        ShopOrder.objects.create(order=self.order, shop=self.shop, billing=BILLING, vat_rate=Decimal("19.00"))
        product = Product.objects.create(sku="AN-1", name="Antenne", name_export="Antenna 2.4 GHz")
        SalesOrderLine.objects.create(order=self.order, product=product, sku_raw="AN-1", qty=Decimal("10"),
                                      unit_price=Decimal("12.5"), total_price=Decimal("125"), currency="EUR")
        self.order.refresh_from_db()


class RulesTests(_Base):
    def test_vat_rules(self):
        s = InvoiceSettings.get()
        self.assertEqual(svc.vat_for("DE", "", None, s), ("domestic", Decimal("19")))
        self.assertEqual(svc.vat_for("FR", "FR123", None, s)[0], "reverse_charge")
        self.assertEqual(svc.vat_for("FR", "", None, s)[0], "domestic")
        self.assertEqual(svc.vat_for("US", "", None, s)[0], "export")
        self.assertEqual(svc.vat_for("FR", "", Decimal("20"), s), ("domestic", Decimal("20")))   # ставка сайту
        self.assertEqual(svc.vat_for("FR", "FR123", Decimal("0"), s)[0], "reverse_charge")

    def test_language(self):
        s = InvoiceSettings.get()
        self.assertEqual((svc.language_for("AT", s), svc.language_for("FR", s)), ("de", "en"))
        s.default_language = "de_en"
        self.assertEqual(svc.language_for("FR", s), "de_en")

    def test_number_from_shop_and_prefix(self):
        self.assertEqual(svc.number_for(self.order), "CI-31728-001")      # нумерація магазину
        self.assertEqual(svc.number_for(self.order), "CI-31728-001")      # закріплено за замовленням
        self.assertEqual(ShopOrder.objects.get(order=self.order).doc_numbers["CI"], "CI-31728-001")
        plain = SalesOrder.objects.create(source="manual", order_number="M-7", client="X")
        self.assertEqual(svc.number_for(plain), "RE-M-7")
        SalesInvoice.objects.create(number="RE-M-7", inv_date=timezone.localdate())
        self.assertEqual(svc.unique_number("RE-M-7"), "RE-M-7-2")

    def test_initial_data_from_site(self):
        data = svc.initial_data(self.order)
        self.assertEqual(data["bill_to"]["company"], "Fogra e.V.")
        self.assertEqual(data["bill_to"]["zip_city"], "81673 München")
        self.assertEqual(data["bill_to"]["country"], "Deutschland")
        self.assertEqual(data["bill_to"]["vat_id"], "DE129514004")
        self.assertEqual(data["ship_to"]["street"], "Lager 1")            # доставка — на іншу адресу
        self.assertEqual((data["language"], data["vat_mode"], data["vat_rate"]), ("de", "domestic", Decimal("19.00")))
        self.assertEqual(data["lines"], [{"description": "Antenna 2.4 GHz", "part_no": "AN-1", "qty": 10, "unit": "",
                                          "unit_price": "12.5"}])
        self.assertEqual(data["due_date"], timezone.localdate() + timedelta(days=7))   # Vorkasse
        self.assertIn("Vorkasse", data["payment_text"])
        self.assertEqual(data["shipping_cost"], Decimal("12.90"))

    def test_paid_order_has_no_due_date(self):
        self.order.payment_status = "paid"
        self.order.payment_method = "stripe"
        data = svc.initial_data(self.order)
        self.assertIsNone(data["due_date"])
        self.assertIn("{method}", data["payment_text"])

    def test_context_fills_placeholders_and_totals(self):
        inv = svc.create_from_order(self.order, self.boss)
        ctx = svc.build_context(inv)
        self.assertEqual((ctx["net_total"], ctx["vat_total"], ctx["gross_total"]), ("137,90", "26,20", "164,10"))
        self.assertIn("164,10 EUR", str(ctx["payment_text"]))
        self.assertIn("CI-31728-001", str(ctx["payment_text"]))
        self.assertEqual(ctx["bill"]["vat_line"], "USt-IdNr.: DE129514004")
        inv.vat_mode = "reverse_charge"
        ctx = svc.build_context(inv)
        self.assertEqual((ctx["vat_rate"], ctx["vat_total"], ctx["gross_total"]), ("0", "0,00", "137,90"))

    def test_fill_keeps_unknown_braces(self):
        self.assertEqual(svc.fill("{number} {x} {}", {"number": "A-1"}), "A-1 {x} {}")


class DocumentTests(_Base):
    def test_blank_template_renders_invoice(self):
        inv = svc.create_from_order(self.order, self.boss)
        self.assertEqual(svc.template_kind(), "blank")
        text = docx_text(svc.render_docx(inv))
        for needle in ("Rechnung", "CI-31728-001", "Fogra e.V.", "Rechnungsempfänger", "USt-IdNr.: DE129514004",
                       "Antenna 2.4 GHz", "Art.-Nr.: AN-1", "Lieferadresse", "Gesamtbetrag brutto: 164,10 EUR",
                       "Verpackung und Versand"):
            self.assertIn(needle, text)
        self.assertNotIn("{{", text)
        self.assertNotIn("HS Code", text)

    def test_simple_template_renders(self):
        inv = svc.create_from_order(self.order, self.boss)
        text = docx_text(svc._render(svc.simple_template(), svc.build_context(inv)))
        self.assertIn("Rechnung CI-31728-001", text)
        self.assertIn("Gesamtbetrag brutto: 164,10 EUR", text)

    @mock.patch("documents.service._convert_to_pdf", return_value=b"%PDF-1.4 test")
    def test_generate_copies_to_order_folder(self, _pdf):
        inv = svc.generate(svc.create_from_order(self.order, self.boss))
        self.assertTrue(inv.docx_file and inv.pdf_file)
        folder = Path(MEDIA) / "orders" / "sevskiy_de" / "PO-31728-001"
        self.assertTrue((folder / "Rechnung_CI-31728-001.pdf").exists())
        from shop.accounts import order_documents
        docs = order_documents(ShopOrder.objects.get(order=self.order))   # кабінет покупця бачить рахунок
        self.assertEqual([d["kind"] for d in docs], ["CI"])


@mock.patch("documents.service._convert_to_pdf", return_value=None)
class PageTests(_Base):
    def test_rights(self, _pdf):
        stranger = person("ola", modules=("crm",))
        self.client.force_login(stranger)
        self.assertEqual(self.client.get("/rechnungen/").status_code, 403)
        viewer = person("vika")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get("/rechnungen/").status_code, 200)
        self.assertEqual(self.client.get(f"/rechnungen/new/?order={self.order.pk}").status_code, 403)
        inv = svc.create_from_order(self.order, self.boss)
        page = self.client.get(f"/rechnungen/{inv.pk}/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'type="submit" class="ri-btn primary"')   # без права — без кнопки
        self.assertEqual(self.client.post(f"/rechnungen/{inv.pk}/", {"number": "X"}).status_code, 403)
        self.assertEqual(self.client.post(f"/rechnungen/{inv.pk}/delete/").status_code, 403)
        self.assertEqual(self.client.post("/rechnungen/template/reset/").status_code, 403)

    def _post(self, url, **extra):
        data = {"inv_date": "2026-11-18", "language": "de", "currency": "EUR", "bill_company": "Fogra e.V.",
                "bill_email": "rechnung@fogra.example", "lines": '[{"description": "Antenne", "part_no": "AN-1", '
                '"qty": "2", "unit_price": "10,5"}]', "shipping_cost": "5", "vat_mode": "domestic", "vat_rate": "19",
                "payment_text": "Bis {due_date}", "due_date": "2026-12-02", **extra}
        return self.client.post(url, data)

    def test_create_edit_delete(self, _pdf):
        self.client.force_login(self.boss)
        self.assertContains(self.client.get(f"/rechnungen/new/?order={self.order.pk}"), "Fogra e.V.")
        resp = self._post(f"/rechnungen/new/?order={self.order.pk}", order_id=self.order.pk)
        inv = SalesInvoice.objects.get()
        self.assertRedirects(resp, f"/rechnungen/{inv.pk}/", fetch_redirect_response=False)
        self.assertEqual((inv.number, inv.sales_order, inv.gross_total), ("CI-31728-001", self.order, Decimal("30.94")))
        self.assertEqual(inv.customer_number, "31728")            # видано разом із номером магазину
        self.assertTrue(inv.docx_file)
        again = self.client.get(f"/rechnungen/new/?order={self.order.pk}")        # уже є — відкривається наявний
        self.assertRedirects(again, f"/rechnungen/{inv.pk}/", fetch_redirect_response=False)
        self._post(f"/rechnungen/{inv.pk}/", shipping_cost="0", vat_mode="reverse_charge")
        inv.refresh_from_db()
        self.assertEqual((inv.vat_mode, inv.gross_total), ("reverse_charge", Decimal("21.00")))
        bad = self._post(f"/rechnungen/{inv.pk}/", lines='[{"description": "A", "qty": "-1", "unit_price": "1"}]')
        self.assertContains(bad, "від&#x27;ємними")
        file_resp = self.client.get(f"/rechnungen/{inv.pk}/file/docx/")
        self.assertEqual(file_resp.status_code, 200)
        file_resp.close()
        self.client.post(f"/rechnungen/{inv.pk}/delete/")
        self.assertFalse(SalesInvoice.objects.exists())

    def test_taken_number_refused(self, _pdf):
        SalesInvoice.objects.create(number="R-1", inv_date=timezone.localdate())
        self.client.force_login(self.boss)
        resp = self._post("/rechnungen/new/", number="R-1")
        self.assertContains(resp, "уже має інший рахунок")
        self.assertEqual(SalesInvoice.objects.count(), 1)

    def test_template_upload_checked_and_reset(self, _pdf):
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get("/rechnungen/template/sample/").status_code, 200)
        from docx import Document
        doc = Document()
        doc.add_paragraph("{{ number }} {{ broken")
        buf = BytesIO()
        doc.save(buf)
        self.client.post("/rechnungen/template/upload/",
                         {"template": SimpleUploadedFile("t.docx", buf.getvalue())})
        self.assertEqual(svc.template_kind(), "blank")                    # шаблон з помилкою не прийнято
        good = Document()
        good.add_paragraph("Mein Rechnung {{ number }}")
        buf = BytesIO()
        good.save(buf)
        self.client.post("/rechnungen/template/upload/",
                         {"template": SimpleUploadedFile("t.docx", buf.getvalue())})
        self.assertEqual(svc.template_kind(), "custom")
        inv = svc.create_from_order(self.order, self.boss)
        self.assertIn("Mein Rechnung CI-31728-001", docx_text(svc.render_docx(inv)))
        self.client.post("/rechnungen/template/reset/")
        self.assertEqual(svc.template_kind(), "blank")

    def test_settings_admin_and_menu_redirect(self, _pdf):
        self.client.force_login(self.boss)
        self.assertRedirects(self.client.get("/admin/sales/salesinvoice/"), "/rechnungen/",
                             fetch_redirect_response=False)
        page = self.client.get("/admin/sales/invoicesettings/", follow=True)
        self.assertContains(page, "Тексти рахунку")


@mock.patch("documents.service._convert_to_pdf", return_value=None)
class MailTests(_Base):
    def test_attachment_ref_needs_right_and_marks_sent(self, _pdf):
        from email_assistant import attachments
        inv = svc.generate(svc.create_from_order(self.order, self.boss))
        stranger = person("ola", modules=("crm", "email_assistant"))
        acc = EmailAccount.objects.create(user=stranger, email_address="ola@x.example", imap_username="o",
                                          imap_password="x", smtp_username="o", smtp_password="x", is_primary=True)
        self.assertIsNone(attachments.load_ref(acc, f"invoice:{inv.pk}"))
        acc.user = self.boss
        item = attachments.load_ref(acc, f"invoice:{inv.pk}")
        self.assertEqual(item["name"], "Rechnung_CI-31728-001.docx")
        self.assertEqual(svc.mark_sent_refs([f"invoice:{inv.pk}"], "a@b.c", user=stranger), 0)
        self.assertEqual(svc.mark_sent_refs([f"invoice:{inv.pk}"], "a@b.c", user=self.boss), 1)
        inv.refresh_from_db()
        self.assertEqual(inv.sent_to, "a@b.c")
        self.assertIsNotNone(inv.sent_at)

    def test_compose_prefill(self, _pdf):
        inv = svc.generate(svc.create_from_order(self.order, self.boss))
        data = svc.compose_initial(inv.pk, self.boss)
        self.assertEqual(data["to"], "rechnung@fogra.example")
        self.assertEqual(data["subject"], "Rechnung CI-31728-001")
        self.assertIn("164,10 EUR", data["body_html"])
        self.assertEqual(data["att_refs"], [{"ref": f"invoice:{inv.pk}", "name": "Rechnung_CI-31728-001.docx"}])

    def test_media_folder_is_sales(self, _pdf):
        from tabele.media import _allowed
        stranger = person("ola", modules=("crm",))
        self.assertFalse(_allowed(stranger, "sales_invoices/Rechnung_X.pdf"))
        self.assertTrue(_allowed(person("vika"), "sales_invoices/Rechnung_X.pdf"))


@mock.patch("documents.service._convert_to_pdf", return_value=b"%PDF-1.4 test")
class QuickFromOrderTests(_Base):
    def test_one_click_creates_and_returns_to_order(self, _pdf):
        self.client.force_login(self.boss)
        url = f"/rechnungen/from-order/{self.order.pk}/"
        self.assertEqual(self.client.get(url).status_code, 405)                 # лише POST
        resp = self.client.post(url)
        self.assertRedirects(resp, f"/admin/sales/salesorder/{self.order.pk}/change/", fetch_redirect_response=False)
        inv = SalesInvoice.objects.get()
        self.assertEqual((inv.number, inv.customer_number, inv.created_by), ("CI-31728-001", "31728", self.boss))
        self.assertTrue(inv.pdf_file and inv.docx_file)
        again = self.client.post(url)                                           # другий клік — той самий рахунок
        self.assertRedirects(again, f"/rechnungen/{inv.pk}/", fetch_redirect_response=False)
        self.assertEqual(SalesInvoice.objects.count(), 1)

    def test_without_right_nothing_created(self, _pdf):
        self.client.force_login(person("vika"))
        resp = self.client.post(f"/rechnungen/from-order/{self.order.pk}/")
        self.assertRedirects(resp, f"/admin/sales/salesorder/{self.order.pk}/change/", fetch_redirect_response=False)
        self.assertFalse(SalesInvoice.objects.exists())

    def test_order_card_button_follows_setting(self, _pdf):
        from sales.admin_invoices import order_button
        html = order_button(self.order, None, "")
        self.assertIn('data-ri-mode="quick"', html)
        self.assertIn(f'data-ri-quick="/rechnungen/from-order/{self.order.pk}/"', html)
        InvoiceSettings.objects.filter(pk=1).update(order_button="form")
        self.assertIn('data-ri-mode="form"', order_button(self.order, None, ""))
        inv = svc.generate(svc.create_from_order(self.order, self.boss))
        html = order_button(self.order, inv, "")
        self.assertIn(f'href="/rechnungen/{inv.pk}/"', html)
        self.assertIn(f'data-ri-pdf="/rechnungen/{inv.pk}/file/pdf/"', html)
        self.assertNotIn("data-ri-quick=", html.split("<script>")[0])     # є рахунок — лише відкрити
        self.client.force_login(self.boss)
        page = self.client.get(f"/admin/sales/salesorder/{self.order.pk}/change/")
        self.assertContains(page, "🧾 Rechnung")
