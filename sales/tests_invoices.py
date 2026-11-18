"""🧾 Рахунки (Rechnung): номер з нумерації магазину, адреса з сайту, ПДВ, тексти, документ на бланку CI, сторінки
/rechnungen/ з правами, лист з вкладенням `invoice:<pk>`.

python manage.py test sales.tests_invoices --settings=tabele.settings_test
"""
import shutil
import tempfile
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


COMPANY = dict(name="Sevskiy GmbH", legal_name="Sevskiy GmbH", addr_street="Schatzbogen 43", addr_zip="81829",
               addr_city="München", addr_country="DE", vat_id="DE 293186022", tax_id="143/180/01384",
               iban="DE35700100800607738803", bank_name="Postbank Ndl Deutsche Bank", swift="PBNKDEFFXXX",
               email="sergey@sevskiy.de", phone="+49 89 38-90-72-29", website="www.sevskiy.de",
               registration_court="München, HRB 208657", ceo_name="Dr.-Ing. Sergey Sevskiy, Geschäftsführer")


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
        self.assertIsNone(data["due_date"])                 # Vorkasse — «vorab», без дати (як у зразку)
        self.assertEqual(data["pay_kind"], "prepayment")
        self.assertIn("vorab", data["payment_text"])
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
        self.assertIn("CI-31728-001", str(ctx["payment_text"]))
        self.assertIn("<w:b/>", str(ctx["intro"]))                         # номер замовлення жирним
        self.assertIn("PO-31728-001", str(ctx["intro"]))
        self.assertEqual(ctx["bill"]["vat_line"], "USt-IdNr.: DE129514004")
        self.assertEqual(ctx["bill"]["attn"], "z. Hd. Anna Muster")
        self.assertEqual(str(ctx["salutation"]), "Sehr geehrte Damen und Herren,")
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
        for needle in ("Rechnung", "Rechnungsnummer:\tCI-31728-001", "Fogra e.V.", "z. Hd. Anna Muster",
                       "USt-IdNr.: DE129514004", "Antenna 2.4 GHz AN-1", "Lieferadresse: Lager 1",
                       "Gesamtsumme brutto", "164,10", "Versandkosten", "Sehr geehrte Damen und Herren,",
                       "gemäß Ihrer Bestellung PO-31728-001", "Mit freundlichen Grüßen", "ohne Unterschrift gültig"):
            self.assertIn(needle, text)
        self.assertNotIn("{{", text)
        self.assertNotIn("HS Code", text)

    def test_simple_template_renders(self):
        inv = svc.create_from_order(self.order, self.boss)
        text = docx_text(svc._render(svc.simple_template(), svc.build_context(inv)))
        self.assertIn("Rechnungsnummer:\tCI-31728-001", text)
        self.assertIn("Gesamtsumme brutto", text)

    @mock.patch("documents.service._convert_to_pdf", return_value=b"%PDF-1.4 test")
    def test_generate_copies_to_order_folder(self, _pdf):
        inv = svc.generate(svc.create_from_order(self.order, self.boss))
        self.assertTrue(inv.docx_file and inv.pdf_file)
        path = _pdf.call_args.args[0]                    # LibreOffice отримує ШЛЯХ до збереженого DOCX, не байти
        self.assertTrue(isinstance(path, str) and path.endswith(".docx"), path)
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


@mock.patch("documents.service._convert_to_pdf", return_value=b"%PDF-1.4 test")
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
        self.assertEqual((item["name"], item["content_type"]), ("Rechnung_CI-31728-001.pdf", "application/pdf"))
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
        self.assertEqual(data["att_refs"], [{"ref": f"invoice:{inv.pk}", "name": "Rechnung_CI-31728-001.pdf"}])

    def test_only_pdf_goes_by_mail(self, _pdf):
        """Поштою — лише PDF: без PDF (немає LibreOffice) вкладення немає, DOCX не надсилається."""
        _pdf.return_value = None
        inv = svc.generate(svc.create_from_order(self.order, self.boss))
        self.assertTrue(inv.docx_file and not inv.pdf_file)
        self.assertIsNone(svc.attachment(inv.pk))
        self.assertEqual(svc.compose_initial(inv.pk, self.boss)["att_refs"], [])
        _pdf.return_value = b"%PDF-1.4 test"
        data = svc.compose_initial(inv.pk, self.boss)                       # PDF формується сам перед листом
        self.assertEqual(data["att_refs"][0]["name"], "Rechnung_CI-31728-001.pdf")

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
        self.assertIn(f'data-ri-regen="/rechnungen/{inv.pk}/regenerate/"', html)
        self.assertNotIn("data-ri-quick=", html.split("<script>")[0])     # є рахунок — лише відкрити
        self.client.force_login(self.boss)
        page = self.client.get(f"/admin/sales/salesorder/{self.order.pk}/change/")
        self.assertContains(page, "🧾 Rechnung")


class LetterLayoutTests(_Base):
    """Рахунок-лист як у зразку: дані фірми, звертання, англійська для інших країн, банк, колонтитул."""

    def setUp(self):
        super().setUp()
        from accounting.models import CompanySettings
        c = CompanySettings.get()
        for k, v in COMPANY.items():
            setattr(c, k, v)
        c.save()

    def test_seller_footer_and_bank_from_company(self):
        inv = svc.create_from_order(self.order, self.boss)
        ctx = svc.build_context(inv)
        self.assertEqual(ctx["seller"]["return_line"], "Sevskiy GmbH, Schatzbogen 43, 81829 München, Deutschland")
        self.assertEqual(ctx["seller"]["tax"], [{"label": "St.-Nr.:", "value": "143/180/01384"},
                                                {"label": "USt-IdNr.:", "value": "DE 293186022"}])
        self.assertEqual(str(ctx["F"]["c1"]).count("Geschäftsführer: Dr.-Ing. Sergey Sevskiy"), 1)
        self.assertIn("IBAN: DE35700100800607738803", str(ctx["F"]["c3"]))
        self.assertEqual(ctx["bank"][1], {"label": "IBAN:", "value": "DE35 7001 0080 0607 7388 03"})
        self.assertEqual(ctx["signer"], inv.signer_name or "Dr.-Ing. Sergey Sevskiy")
        self.assertEqual(svc.company_missing(), [])
        inv.pay_kind = "paid"
        self.assertEqual(svc.build_context(inv)["bank"], [])                 # уже оплачено — без реквізитів
        text = docx_text(svc.render_docx(svc.create_from_order(self.order, self.boss)))
        for needle in ("Sevskiy GmbH, Schatzbogen 43, 81829 München, Deutschland", "Ansprechpartner:",
                       "Kontoinhaber:\tSevskiy GmbH", "IBAN:\tDE35 7001 0080 0607 7388 03", "St.-Nr.:\t143/180/01384"):
            self.assertIn(needle, text)

    def test_salutation_and_attention(self):
        bill = {"company": "Fogra", "name": "Johannes Bartl", "salutation": "mr"}
        self.assertEqual(svc.address_view(bill, "de")["attn"], "z. Hd. Herrn Johannes Bartl")
        self.assertEqual(svc.salutation(bill, "de"), "Sehr geehrter Herr Bartl,")
        self.assertEqual(svc.salutation({**bill, "salutation": "ms"}, "en"), "Dear Ms Bartl,")
        self.assertEqual(svc.salutation(bill, "de_en"), "Sehr geehrter Herr Bartl,\nDear Mr Bartl,")
        self.assertEqual(svc.address_view({"name": "Anna"}, "de")["line1"], "Anna")      # без компанії — ім'я
        self.assertEqual(svc.address_view({"name": "Anna"}, "de")["attn"], "")

    def test_english_outside_german_speaking_countries(self):
        ShopOrder.objects.filter(order=self.order).update(billing={**BILLING, "country": "FR", "vat_id": "FR123"}, vat_rate=0)
        self.order.refresh_from_db()
        data = svc.initial_data(self.order)
        self.assertEqual((data["language"], data["vat_mode"]), ("en", "reverse_charge"))
        self.assertEqual(data["bill_to"]["country"], "France")
        self.assertIn("According to your order {order}", data["intro_text"])
        inv = svc.create_from_order(self.order, self.boss)
        text = docx_text(svc.render_docx(inv))
        for needle in ("Invoice", "Invoice No.:", "Dear Sir or Madam,", "Total gross", "Account holder:",
                       "Sevskiy GmbH, Schatzbogen 43, 81829 München, Germany", "VAT ID: FR123"):
            self.assertIn(needle, text)
        ShopOrder.objects.filter(order=self.order).update(billing={**BILLING, "country": "AT"})
        self.order.refresh_from_db()
        self.assertEqual(svc.initial_data(self.order)["language"], "de")       # Австрія — німецькою

    def test_old_default_texts_updated_by_migration(self):
        import importlib

        from django.apps import apps
        mig = importlib.import_module("sales.migrations.0053_invoice_letter_layout")
        s = InvoiceSettings.get()
        s.intro_de, s.outro_en = mig.OLD["intro"][0], "Own closing"
        s.save()
        inv = SalesInvoice.objects.create(number="R-9", inv_date=timezone.localdate(),
                                          intro_text=mig.OLD["intro"][0] + "\n" + mig.OLD["intro"][1],
                                          payment_text=mig.OLD["pay_prepayment"][0])
        mig.forwards(apps, None)
        s.refresh_from_db()
        inv.refresh_from_db()
        self.assertEqual((s.intro_de, s.outro_en), (mig.NEW["intro"][0], "Own closing"))   # змінене людиною — як є
        self.assertEqual(inv.intro_text, mig.NEW["intro"][0] + "\n" + mig.NEW["intro"][1])
        self.assertEqual((inv.payment_text, inv.pay_kind), (mig.NEW["pay_prepayment"][0], "prepayment"))


@mock.patch("documents.service._convert_to_pdf", return_value=b"%PDF-1.4 test")
class AutoSendTests(_Base):
    """🧾 Рахунок покупцю автоматично: замовлення з сайту → лист з PDF з пошти магазину, один раз."""

    def setUp(self):
        super().setUp()
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
        ns.email_host, ns.email_from = "smtp.example.com", "shop@example.com"
        ns.save()
        self.shop.mail_bcc = "buchhaltung@example.com"
        self.shop.save()
        svc.BACKGROUND = False
        self.addCleanup(setattr, svc, "BACKGROUND", True)
        InvoiceSettings.get()
        InvoiceSettings.objects.filter(pk=1).update(auto_send=True)
        from django.core import mail
        self.outbox = mail.outbox
        self.outbox.clear()

    def new_order(self, number, **fields):
        data = {"source": "sevskiy_de", "order_number": number, "client": "Fogra e.V.", "contact_name": "Anna Muster",
                "email": "einkauf@fogra.example", "addr_country": "DE", "currency": "EUR",
                "payment_method": "prepayment", "payment_status": "unpaid", **fields}
        with self.captureOnCommitCallbacks(execute=True):
            order = SalesOrder.objects.create(**data)
            ShopOrder.objects.create(order=order, shop=self.shop, billing=BILLING, vat_rate=Decimal("19"))
            SalesOrderLine.objects.create(order=order, product=Product.objects.get(sku="AN-1"), sku_raw="AN-1",
                                          qty=Decimal("1"), unit_price=Decimal("10"), total_price=Decimal("10"),
                                          currency="EUR")
        return order

    def test_vorkasse_order_gets_invoice_once(self, _pdf):
        order = self.new_order("PO-31728-002")
        self.assertEqual(len(self.outbox), 1)
        msg = self.outbox[0]
        inv = SalesInvoice.objects.get(sales_order=order)
        self.assertEqual(msg.to, ["rechnung@fogra.example"])                  # email з адреси рахунку на сайті
        self.assertEqual(msg.bcc, ["buchhaltung@example.com"])               # прихована копія магазину
        self.assertEqual(msg.subject, f"Rechnung {inv.number}")
        self.assertIn("Sehr geehrte Damen und Herren,", msg.body)
        self.assertIn(f"{inv.number} zu Ihrer Bestellung PO-31728-002", msg.body)
        self.assertEqual(msg.attachments[0][0], f"Rechnung_{inv.number}.pdf")
        self.assertIsNotNone(inv.sent_at)
        self.assertEqual(InvoiceSettings.get().auto_log[0]["result"], "sent")
        self.assertEqual(svc.auto_send(order.pk), "already")                  # другий раз — нічого
        self.assertEqual(len(self.outbox), 1)

    def test_stripe_waits_for_payment_then_paid_text(self, _pdf):
        order = self.new_order("PO-31728-003", payment_method="stripe", payment_status="pending")
        self.assertEqual(self.outbox, [])
        self.assertFalse(SalesInvoice.objects.filter(sales_order=order).exists())
        order.payment_status = "paid"
        with self.captureOnCommitCallbacks(execute=True):
            order.save(update_fields=["payment_status"])
        self.assertEqual(len(self.outbox), 1)
        inv = SalesInvoice.objects.get(sales_order=order)
        self.assertEqual(inv.pay_kind, "paid")
        self.assertIn("bereits bezahlt", inv.payment_text)
        with self.captureOnCommitCallbacks(execute=True):                   # ще раз «оплачено» — без нового листа
            order.save()
        self.assertEqual(len(self.outbox), 1)

    def test_off_foreign_and_without_pdf(self, _pdf):
        InvoiceSettings.objects.filter(pk=1).update(auto_send=False)
        self.new_order("PO-31728-004")
        self.assertEqual(self.outbox, [])
        InvoiceSettings.objects.filter(pk=1).update(auto_send=True)
        plain = SalesOrder.objects.create(source="manual", order_number="M-9", client="X", email="x@y.z")
        self.assertEqual(svc.auto_send(plain.pk), "not_shop")
        _pdf.return_value = None                                                 # без LibreOffice — покупцю DOCX не шлемо
        order = self.new_order("PO-31728-005")
        self.assertEqual(self.outbox, [])
        self.assertEqual(InvoiceSettings.get().auto_log[0]["result"], "no_pdf")
        self.assertIsNone(SalesInvoice.objects.get(sales_order=order).sent_at)

    def test_english_mail_for_foreign_buyer(self, _pdf):
        ShopOrder.objects.filter(order=self.order).update(billing={**BILLING, "country": "FR", "vat_id": "FR1"},
                                                           vat_rate=0)
        self.order.refresh_from_db()
        self.assertEqual(svc.auto_send(self.order.pk), "sent")
        msg = self.outbox[0]
        self.assertTrue(msg.subject.startswith("Invoice "))
        self.assertIn("Dear Sir or Madam,", msg.body)
        self.assertIn("Kind regards", msg.body)

    def test_test_mail_and_toggle(self, _pdf):
        self.client.force_login(self.boss)
        resp = self.client.post("/rechnungen/test-mail/", {"to": "me@example.com", "lang": "en"})
        self.assertRedirects(resp, "/rechnungen/#auto", fetch_redirect_response=False)
        msg = self.outbox[0]
        self.assertEqual((msg.to, msg.subject[:7]), (["me@example.com"], "[TEST] "))
        self.assertTrue(msg.attachments[0][0].endswith(".pdf"))
        self.assertIn("Dear Mr Mustermann,", msg.body)
        self.assertFalse(SalesInvoice.objects.exists())                          # нічого не збережено
        self.client.post("/rechnungen/auto/", {"on": "0"})
        self.assertFalse(InvoiceSettings.get().auto_send)
        self.client.force_login(person("vika"))
        self.assertEqual(self.client.post("/rechnungen/test-mail/", {"to": "a@b.c"}).status_code, 403)
        self.assertEqual(self.client.post("/rechnungen/auto/", {"on": "1"}).status_code, 403)
        self.assertFalse(InvoiceSettings.get().auto_send)


@mock.patch("documents.service._convert_to_pdf", return_value=b"%PDF-1.4 test")
class RegenerateTests(_Base):
    def test_regenerate_and_pdf_on_demand(self, _pdf):
        _pdf.return_value = None
        inv = svc.generate(svc.create_from_order(self.order, self.boss))
        self.assertFalse(inv.pdf_file)
        _pdf.return_value = b"%PDF-1.4 test"
        self.client.force_login(self.boss)
        resp = self.client.get(f"/rechnungen/{inv.pk}/file/pdf/")              # PDF ще немає — формується одразу
        self.assertEqual((resp.status_code, resp["Content-Type"]), (200, "application/pdf"))
        resp.close()
        inv.refresh_from_db()
        old = inv.pdf_file.name
        resp = self.client.post(f"/rechnungen/{inv.pk}/regenerate/", {"open": "pdf"})
        self.assertRedirects(resp, f"/rechnungen/{inv.pk}/file/pdf/", fetch_redirect_response=False)
        resp = self.client.post(f"/rechnungen/{inv.pk}/regenerate/")
        self.assertRedirects(resp, "/rechnungen/", fetch_redirect_response=False)
        inv.refresh_from_db()
        self.assertTrue(inv.pdf_file)
        self.assertEqual(self.client.get(f"/rechnungen/{inv.pk}/regenerate/").status_code, 405)
        self.client.force_login(person("vika"))
        self.assertEqual(self.client.post(f"/rechnungen/{inv.pk}/regenerate/").status_code, 403)
        self.assertTrue(old)


class CashOnDeliveryTests(_Base):
    """Накладений платіж (Nachnahme): спосіб оплати магазину, рахунок без банківських реквізитів і строку."""

    def test_invoice_for_cod_order(self):
        from accounting.models import CompanySettings
        c = CompanySettings.get()
        c.iban = "DE35700100800607738803"
        c.save()
        self.order.payment_method = "cod"
        self.order.save()
        data = svc.initial_data(self.order)
        self.assertEqual((data["pay_kind"], data["due_date"]), ("cod", None))
        self.assertIn("Nachnahme", data["payment_text"])
        inv = svc.create_from_order(self.order, self.boss)
        ctx = svc.build_context(inv)
        self.assertEqual(ctx["bank"], [])                                       # платить кур'єру — без реквізитів
        self.assertIn("164,10 EUR", str(ctx["payment_text"]))
        self.assertEqual(svc.method_name("cod", "en"), "cash on delivery")
        self.assertIn("Накладений платіж (Nachnahme)", self.order.get_payment_method_display())

    def test_shop_card_toggle_writes_invoice_settings(self):
        from django.contrib import admin
        from django.test import RequestFactory
        from shop.admin_payments import ShopForm
        InvoiceSettings.get()
        self.assertFalse(ShopForm(instance=self.shop).initial["invoice_auto_send"])
        ma = admin.site._registry[Shop]
        request = RequestFactory().get("/")
        request.user = self.boss
        self.assertFalse(ma.get_form(request, self.shop).base_fields["invoice_auto_send"].disabled)
        form = ShopForm(instance=self.shop)
        form.cleaned_data = {"invoice_auto_send": True}
        ma.save_model(request, self.shop, form, True)
        self.assertTrue(InvoiceSettings.get().auto_send)
        request.user = person("vika")                                          # без права на налаштування рахунків
        self.assertTrue(ma.get_form(request, self.shop).base_fields["invoice_auto_send"].disabled)


class CashOnDeliveryApiTests(TestCase):
    """Сайт надсилає замовлення з payment_method «cod» — Minerva приймає, коли Nachnahme увімкнено в магазині."""

    def setUp(self):
        from shop.tests_accounts import AccountTestCase
        AccountTestCase.setUp(self)

    def order(self, number):
        return self.c.post("/api/v1/orders/", {
            "order_number": number, "client": "Firma GmbH", "contact_name": "Anna", "email": "anna@firma.de",
            "addr_country": "DE", "shop": True, "lines": [{"sku": "AN-1", "qty": 2}], "vat_rate": "19",
            "payment_method": "cod", "payment_status": "unpaid"}, format="json")

    def test_cod_only_when_enabled_in_shop(self):
        self.shop.payment_methods = ["prepayment", "invoice"]
        self.shop.save()
        refused = self.order("WS-COD-1")
        self.assertEqual((refused.status_code, refused.json()["code"]), (400, "payment_method_not_allowed"))
        self.shop.payment_methods = ["prepayment", "cod"]
        self.shop.save()
        ok = self.order("WS-COD-2")
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertEqual(SalesOrder.objects.get(order_number=ok.json()["order_number"]).payment_method, "cod")
