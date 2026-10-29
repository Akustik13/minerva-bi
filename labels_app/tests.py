import json
import shutil
import tempfile
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from bots.models import DigiKeyConfig, DigiKeyListing
from inventory.models import Product
from labels_app import mark, order_label, product_label
from labels_app import views as label_views
from labels_app.models import LabelText
from sales.models import SalesOrder, SalesSettings


def old_label(pn, lines, year=2024, qty=3, barcode=None, qr=None):
    """Старий .dymo у форматі наших етикеток (той самий макет, лише інші тексти)."""
    data = {"pn": pn, "qty": qty, "year": year, "lines": [("", l) for l in lines]}
    xml = product_label.render(data)
    if barcode:
        xml = xml.replace(f"<DataString>{pn}</DataString>\n            </MultiDataString>",
                          f"<DataString>{barcode}</DataString>\n            </MultiDataString>")
    if qr:
        xml = xml.replace(f"<DataString>{pn}</DataString>", f"<DataString>{qr}</DataString>", 1)
    return xml


class AntennaLabelTests(TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        patcher = mock.patch.object(label_views, "LABELS_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.year = timezone.localdate().year
        self.product = Product.objects.create(
            sku="AN220213-01C", name="Антена UWB", name_export="Multi-purpose UWB PCB antenna", category="antenna",
            tech_attributes={"Frequency Range": "370MHz ~ 6GHz", "Termination": "SMA Female", "hts": "8529"})
        cfg = DigiKeyConfig.get()
        cfg.marketplace_supplier_id = "3228"
        cfg.save()

    def listing(self, **kw):
        values = dict(product=self.product, dk_title="UWB PCB Antenna", dk_description="UWB antenna.",
                      dk_product_id="uuid-1", sync_status="published",
                      dk_attributes={"Frequency Range": "370MHz ~ 6GHz", "Termination": "SMA Female"})
        values.update(kw)
        return DigiKeyListing.objects.create(**values)

    def write(self, name, xml):
        (self.dir / name).write_text(xml, encoding="utf-8")

    # ── шаблон ────────────────────────────────────────────────────────────────

    def test_render_fills_every_marked_field(self):
        self.listing()
        order = SalesOrder.objects.create(source="digikey", order_number="102437554")
        data = product_label.build("AN220213-01C", 2, order=order)
        xml = product_label.render(data)
        root = ET.fromstring(xml.encode("utf-8"))
        texts = [t.text for t in root.iter("Text")]
        self.assertIn("P/N: AN220213-01C", texts)
        self.assertIn("QTY: 2 PCS", texts)
        self.assertIn("Digi-Key P/N: 3228-AN220213-01C-ND.", texts)
        self.assertIn("Multi-purpose UWB PCB antenna.", texts)
        self.assertIn("Frequency range: 0.370...6 GHz.", texts)
        self.assertIn("Connector type: SMA Female.", texts)
        self.assertIn(f"Copyright © 2009 - {self.year} Sevskiy GmbH. ", texts)
        self.assertEqual([d.text for d in root.iter("DataString") if d.text], ["AN220213-01C", "AN220213-01C"])
        self.assertEqual(root.find(".//QRCodeObject/TextDataHolder/Value").text, "AN220213-01C")
        self.assertNotIn("{{", xml)
        self.assertNotIn("<!--", xml)

    def test_render_escapes_xml(self):
        data = {"pn": "AN1", "qty": 1, "year": 2026, "lines": [("Frequency: ", "2400...2500 & 4750…7500 MHz.")]}
        root = ET.fromstring(product_label.render(data).encode("utf-8"))
        self.assertIn("Frequency: 2400...2500 & 4750…7500 MHz.", [t.text for t in root.iter("Text")])

    # ── старий файл ──────────────────────────────────────────────────────────

    def test_read_file_parses_our_label_layout(self):
        self.write("AN250202-01P_noDGkey.dymo", old_label("AN250202-01P", [
            "Bands: UMTS, ISM2400, Wi-Fi 5/6 GHz, ISM5800.",
            "Frequency:  1920...2170, 2400...2500,", "5150…7125 MHz.", "Connector type: SMA male"]))
        f = product_label.read_file("AN250202-01P")
        self.assertEqual(f["description"], "Bands: UMTS, ISM2400, Wi-Fi 5/6 GHz, ISM5800")
        self.assertEqual(f["frequency"], "1920...2170, 2400...2500, 5150…7125 MHz")
        self.assertEqual(f["connector"], "SMA male")
        self.assertEqual((f["year"], f["qty"], f["pn"]), (2024, 3, "AN250202-01P"))

    def test_read_file_digikey_line_variants(self):
        for line in ("Digi-Key P/N: 3228-AN1-ND.", "Digi Key P/N:3228-AN1-ND.", "DigiKey P/N: 3228-AN1-ND.",
                     "Digi-Key PIN: 3228-AN1-ND.", "P/N: 3228-AN1-ND."):
            self.write("AN100001.dymo", old_label("AN100001", [line, "Some antenna."]))
            f = product_label.read_file("AN100001")
            self.assertEqual(f["digikey_pn"], "3228-AN1-ND", line)
            self.assertEqual(f["description"], "Some antenna", line)

    def test_old_file_mistakes_are_reported_and_fixed(self):
        self.write("AN220213-01C.dymo", old_label("AN220203-02A", [
            "Digi-Key P/N: 3228-AN220203-02A-ND.", "Multi-purpose UWB PCB antenna."], year=2024, qr="AN110504-02C"))
        self.listing()
        data = product_label.build("AN220213-01C", 1)
        texts = " | ".join(i["text"] for i in data["issues"] if i["level"] == "info")
        self.assertIn("P/N: AN220203-02A", texts)
        self.assertIn("QR: AN110504-02C", texts)
        self.assertIn("Digi-Key P/N 3228-AN220203-02A-ND", texts)
        self.assertIn(f"рік 2024 — на новій етикетці {self.year}", texts)
        xml = product_label.render(data)
        self.assertNotIn("AN220203-02A", xml)
        self.assertNotIn("AN110504-02C", xml)

    def test_file_is_fallback_when_stock_and_digikey_are_empty(self):
        self.product.name_export = ""
        self.product.tech_attributes = {}
        self.product.save()
        self.write("AN220213-01C.dymo", old_label("AN220213-01C", [
            "Multi-purpose UWB PCB antenna.", "Frequency range: 0.370...6 GHz.", "Connector type: SMA female."]))
        fields = product_label.build("AN220213-01C")["fields"]
        self.assertEqual(fields["frequency"]["value"], "0.370...6 GHz")
        self.assertEqual(fields["frequency"]["source"], "file")
        self.assertEqual(fields["description"]["value"], "Multi-purpose UWB PCB antenna")

    # ── джерела ───────────────────────────────────────────────────────────────

    def test_stock_first_then_digikey(self):
        self.listing(dk_attributes={"Frequency Range": "400MHz ~ 6GHz"})
        data = product_label.build("AN220213-01C")
        fields = data["fields"]
        self.assertEqual((fields["description"]["value"], fields["description"]["source"]),
                         ("Multi-purpose UWB PCB antenna", "stock"))
        self.assertEqual(fields["frequency"]["value"], "0.370...6 GHz")
        self.assertTrue(any("🔷 DigiKey «0.400...6 GHz»" in i["text"] for i in data["issues"] if i["level"] == "warn"))

    def test_frequency_in_one_unit(self):
        for raw, label in (("600MHz ~ 7.2GHz", "0.600...7.2 GHz"), ("370MHz ~ 6GHz", "0.370...6 GHz"),
                           ("2.4GHz ~ 2.5GHz", "2.4...2.5 GHz"), ("824 ~ 960MHz", "824...960 MHz"),
                           ("698MHz ~ 960MHz, 1.71GHz ~ 2.7GHz", "698...960 MHz, 1.71...2.7 GHz"),
                           ("433MHz, 866MHz, 5.5GHz", "433MHz, 866MHz, 5.5GHz")):
            self.assertEqual(product_label._fmt_frequency(raw), label, raw)

    def test_mixed_units_in_old_file_are_flagged(self):
        self.product.tech_attributes = {}
        self.product.save()
        self.write("AN220213-01C.dymo", old_label("AN220213-01C", [
            "Multi-purpose UWB PCB antenna.", "Frequency range: 0.6 MHz ... 10 GHz."]))
        issues = product_label.build("AN220213-01C")["issues"]
        self.assertTrue(any("різні одиниці" in i["text"] for i in issues))

    def test_part_number_is_not_a_description(self):
        self.product.name_export = "3228-AN220213-01C-ND"
        self.product.name = "AN220213-01C"
        self.product.save()
        self.listing(dk_title="3228-AN220213-01C-ND", dk_description="Multi-purpose UWB PCB antenna. 370 MHz.")
        self.assertEqual(product_label.build("AN220213-01C")["fields"]["description"]["value"], "Multi-purpose UWB PCB antenna")

    def test_cyrillic_stock_name_is_skipped_for_digikey_title(self):
        self.product.name_export = ""
        self.product.save()
        self.listing()
        self.assertEqual(product_label.build("AN220213-01C")["fields"]["description"]["value"], "UWB PCB Antenna")

    def test_missing_description_blocks_print(self):
        self.product.name_export = ""
        self.product.save()
        data = product_label.build("AN220213-01C")
        self.assertFalse(data["can_print"])

    def test_too_many_lines_blocks_print(self):
        self.listing()
        long = "ISM, RFID, IoT (Sigfox, LoRa), LP-WAN, Bluetooth, Wi-Fi 2.4/5GHz, 5G NR, LTE"
        data = product_label.build("AN220213-01C", order=SalesOrder.objects.create(source="digikey", order_number="1"),
                             edits={"description": long})
        self.assertEqual(len(data["lines"]), 5)
        self.assertFalse(data["can_print"])
        self.assertTrue(product_label.build("AN220213-01C", show_dk=False, edits={"description": long})["can_print"])

    # ── Digi-Key P/N ──────────────────────────────────────────────────────────

    def test_digikey_line_follows_order_source(self):
        self.listing(dk_supplier_sku="AN220213-01C-160-MHF1")
        dk = product_label.build("AN220213-01C", order=SalesOrder.objects.create(source="digikey", order_number="1"))
        shop = product_label.build("AN220213-01C", order=SalesOrder.objects.create(source="webshop", order_number="2"))
        self.assertEqual(dk["digikey_pn"], "3228-AN220213-01C-160-MHF1-ND")
        self.assertTrue(dk["show_dk"])
        self.assertFalse(shop["show_dk"])
        self.assertTrue(product_label.build("AN220213-01C", show_dk=True)["show_dk"])   # галочка у вікні

    def test_draft_listing_has_no_digikey_pn(self):
        self.listing(dk_product_id="", sync_status="draft")
        data = product_label.build("AN220213-01C", show_dk=True)
        self.assertEqual(data["digikey_pn"], "")
        self.assertFalse(data["show_dk"])
        self.assertTrue(any("не опубліковано" in i["text"] for i in data["issues"]))

    def test_label_is_for_the_ordered_sku(self):
        """Скриншот 29.10: у замовленні AN220203-02A — Supplier SKU лістингу товару AN220214-01A;
        у налаштуваннях DigiKey — UUID постачальника Marketplace, а не 3228."""
        cfg = DigiKeyConfig.get()
        cfg.marketplace_supplier_id = "b6629887-3d7c-5cf9-adff-0d7fd9a1b2c3"
        cfg.save()
        other = Product.objects.create(
            sku="AN220214-01A", name="3228-AN220214-01A-ND", name_export="", category="antenna",
            tech_attributes={"Frequency Range": "600MHz ~ 7.2GHz", "Termination": "SMA Female"})
        DigiKeyListing.objects.create(product=other, dk_title="AN220203-02A", dk_description="", dk_product_id="u-2",
                                      dk_supplier_sku="AN220203-02A",
                                      dk_attributes={"Frequency Range": "600MHz ~ 7.2GHz"})
        self.write("AN220203-02A.dymo", old_label("AN220203-02A", [
            "Digi-Key P/N: 3228-AN220203-02A-ND.", "Multi-purpose UWB PCB antenna.",
            "Frequency range: 0.650...9 GHz.", "Connector type: SMA female."], year=2026))
        order = SalesOrder.objects.create(source="digikey", order_number="102437554")
        data = product_label.build("AN220203-02A", 5, order=order)
        self.assertEqual(data["pn"], "AN220203-02A")
        self.assertEqual(data["digikey_pn"], "3228-AN220203-02A-ND")
        self.assertEqual([p + v for p, v in data["lines"]], [
            "Digi-Key P/N: 3228-AN220203-02A-ND.", "Multi-purpose UWB PCB antenna.",
            "Frequency range: 0.650...9 GHz.", "Connector type: SMA female."])
        xml = product_label.render(data)
        self.assertNotIn("b6629887", xml)
        self.assertNotIn("AN220214-01A", xml)
        self.assertTrue(any("0.600...7.2 GHz" in i["text"] for i in data["issues"] if i["level"] == "warn"))

    def test_uuid_vendor_falls_back_to_other_labels(self):
        cfg = DigiKeyConfig.get()
        cfg.marketplace_supplier_id = "b6629887-3d7c-5cf9-adff-0d7fd9a1b2c3"
        cfg.save()
        self.listing()
        self.write("AN100001.dymo", old_label("AN100001", ["Digi-Key P/N: 3228-AN100001-ND.", "Some antenna."]))
        self.assertEqual(product_label.build("AN220213-01C")["digikey_pn"], "3228-AN220213-01C-ND")

    def test_digikey_pn_can_be_corrected_and_is_remembered(self):
        self.listing()
        data = product_label.build("AN220213-01C", edits={"digikey_pn": "3228-AN220203-02A-ND"})
        self.assertTrue(any("не артикула AN220213-01C" in i["text"] for i in data["issues"]))
        product_label.save("AN220213-01C", {"digikey_pn": "3228-AN220213-01C-X-ND"})
        self.assertEqual(LabelText.objects.get(sku="AN220213-01C").digikey_pn, "3228-AN220213-01C-X-ND")
        self.assertEqual(product_label.build("AN220213-01C")["digikey_pn"], "3228-AN220213-01C-X-ND")

    def test_checked_texts_belong_to_one_sku(self):
        self.listing(dk_supplier_sku="AN220213-01C-X")
        product_label.save("AN220213-01C-X", {"description": "Variant antenna"})
        self.assertEqual(product_label.build("AN220213-01C-X")["fields"]["description"]["value"], "Variant antenna")
        self.assertEqual(product_label.build("AN220213-01C")["fields"]["description"]["value"], "Multi-purpose UWB PCB antenna")
        self.assertEqual(LabelText.objects.get(sku="AN220213-01C-X").product, self.product)

    # ── перевірено людиною ────────────────────────────────────────────────────

    def test_save_then_ready_until_stock_changes(self):
        self.listing()
        self.assertFalse(product_label.build("AN220213-01C")["ready"])
        product_label.save("AN220213-01C", {"frequency": "0.37...6 GHz"}, user=User(username="slava"))
        text = LabelText.objects.get(sku="AN220213-01C")
        self.assertEqual((text.frequency, text.checked_by), ("0.37...6 GHz", "slava"))
        data = product_label.build("AN220213-01C")
        self.assertEqual(data["fields"]["frequency"]["source"], "saved")
        self.assertTrue(data["ready"])
        self.product.tech_attributes = {"Frequency Range": "300MHz ~ 6GHz", "Termination": "SMA Female"}
        self.product.save()
        data = product_label.build("AN220213-01C")
        self.assertFalse(data["ready"])
        self.assertTrue(any("під час перевірки було «0.370...6 GHz»" in i["text"] for i in data["issues"]))

    def test_deliberately_empty_connector_stays_empty(self):
        product_label.save("AN220213-01C", {"connector": ""})
        data = product_label.build("AN220213-01C")
        self.assertEqual(data["fields"]["connector"]["value"], "")
        self.assertFalse(any(p == "Connector type: " for p, _v in data["lines"]))
        self.assertTrue(data["ready"])


class LabelViewsTests(TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        patcher = mock.patch.object(label_views, "LABELS_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.user = User.objects.create_user("staff", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)
        self.product = Product.objects.create(
            sku="AN220213-01C", name_export="Multi-purpose UWB PCB antenna",
            tech_attributes={"Frequency Range": "370MHz ~ 6GHz", "Termination": "SMA Female"})
        self.order = SalesOrder.objects.create(source="digikey", order_number="102437554")

    def test_status_marks_antennas_and_cables_as_generatable(self):
        (self.dir / "FT220407-01A.dymo").write_text("<x/>", encoding="utf-8")
        r = self.client.get("/labels/status/?skus=AN220213-01C,CA-MHF1-MC1.13-175-MHF4,FT220407-01A,XX-1")
        d = r.json()
        self.assertEqual((d["AN220213-01C"]["found"], d["AN220213-01C"]["generate"], d["AN220213-01C"]["file"]),
                         (True, "antenna", False))
        self.assertEqual(d["CA-MHF1-MC1.13-175-MHF4"]["generate"], "cable")
        self.assertEqual((d["FT220407-01A"]["found"], d["FT220407-01A"]["file"]), (True, True))
        # етикетка є для кожного артикула: файлу немає — з шаблону «інший товар»
        self.assertEqual((d["XX-1"]["found"], d["XX-1"]["file"], d["XX-1"]["generate"]), (True, False, "generic"))
        self.assertEqual(d["FT220407-01A"]["generate"], "filter")

    def test_check_save_and_download(self):
        r = self.client.get("/labels/product/check/", {"sku": "AN220213-01C", "qty": 2, "order": self.order.pk})
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertFalse(d["ready"])
        self.assertIn("/labels/product/AN220213-01C/?qty=2", d["url"])
        r = self.client.post("/labels/product/save/", json.dumps({
            "sku": "AN220213-01C", "qty": 2, "order": self.order.pk, "dk": 0,
            "description": "Multi-purpose UWB PCB antenna", "frequency": "0.370...6 GHz", "connector": "SMA female"}),
            content_type="application/json")
        d = r.json()
        self.assertTrue(d["ok"], d)
        self.assertTrue(d["ready"])
        self.assertIn("dk=0", d["url"])
        r = self.client.get(d["url"])
        self.assertEqual(r.status_code, 200)
        self.assertIn('filename="AN220213-01C.dymo"', r["Content-Disposition"])
        texts = [t.text for t in ET.fromstring(r.content).iter("Text")]
        self.assertIn("QTY: 2 PCS", texts)
        self.assertIn("Frequency range: 0.370...6 GHz.", texts)

    def test_save_refuses_label_with_errors(self):
        r = self.client.post("/labels/product/save/", json.dumps({"sku": "AN220213-01C", "description": ""}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(LabelText.objects.exists())
        self.assertEqual(self.client.get("/labels/product/AN220213-01C/?qty=0").status_code, 400)

    def test_save_needs_csrf(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        r = client.post("/labels/product/save/", json.dumps({"sku": "AN220213-01C"}), content_type="application/json")
        self.assertEqual(r.status_code, 403)
        r = client.post("/labels/antenna/save/", json.dumps({"sku": "AN220213-01C"}), content_type="application/json")
        self.assertEqual(r.status_code, 403)                               # стара адреса — та сама перевірка

    def test_order_card_buttons_follow_setting(self):
        from sales.models import SalesOrderLine
        SalesOrderLine.objects.create(order=self.order, product=self.product, sku_raw="AN220213-01C", qty=2)
        r = self.client.get(f"/admin/sales/salesorder/{self.order.pk}/change/")
        self.assertContains(r, 'data-svl="antenna"')
        self.assertContains(r, "🧩 з шаблону антени")
        self.assertContains(r, "SOURCE = 'generate'")
        settings_obj = SalesSettings.get()
        settings_obj.dymo_label_source = "file"
        settings_obj.save()
        r = self.client.get(f"/admin/sales/salesorder/{self.order.pk}/change/")
        self.assertContains(r, "SOURCE = 'file'")

    def test_direct_print_is_the_same_everywhere(self):
        """«🖨️ Надрукувати через DYMO» — одна функція (служба DYMO Connect) у списку, формі, картці товару, /labels/."""
        from sales.models import SalesOrderLine
        SalesOrderLine.objects.create(order=self.order, product=self.product, sku_raw="AN220213-01C", qty=2)
        filter_ = Product.objects.create(sku="FT220407-01A", name="Filter")
        (self.dir / "FT220407-01A.dymo").write_text("<x/>", encoding="utf-8")
        settings_obj = SalesSettings.get()
        settings_obj.dymo_open_mode = "print"
        settings_obj.save()
        pages = {
            "order list": "/admin/sales/salesorder/",
            "order form": f"/admin/sales/salesorder/{self.order.pk}/change/",
            "product card": f"/admin/inventory/product/{filter_.pk}/change/",
            "labels page": "/labels/",
        }
        with self.settings(LABELS_DIR=self.dir):                          # картка товару шукає файл сама
            for name, url in pages.items():
                r = self.client.get(url)
                self.assertContains(r, "DYMO_MODE = 'print'", msg_prefix=name)
                self.assertContains(r, "https://127.0.0.1:41951/DYMO/DLS/Printing/", msg_prefix=name)
                self.assertNotContains(r, "labelwriter.com", msg_prefix=name)   # старий скрипт DYMO Label Software
            r = self.client.get(pages["product card"])
        self.assertContains(r, 'data-svl="filter" data-sku="FT220407-01A"')   # фільтр: файл з бази або шаблон
        self.assertContains(self.client.get(pages["labels page"]), 'data-svl-act="direct"')


class OrderLabelTests(TestCase):
    """Етикетка замовлення: номер, країна й усі артикули з кількістю."""

    def setUp(self):
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.user = User.objects.create_user("staff", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)
        self.order = SalesOrder.objects.create(source="digikey", order_number="102497269", addr_country="NZ")

    def add(self, sku, qty, sku_raw=None):
        from sales.models import SalesOrderLine
        product = Product.objects.get_or_create(sku=sku, defaults={"name": sku})[0]
        return SalesOrderLine.objects.create(order=self.order, product=product,
                                             sku_raw=sku if sku_raw is None else sku_raw, qty=qty)

    def test_label_lists_every_sku_with_quantity(self):
        for sku, qty in (("AN220214-01A", 5), ("AN220215-01A", 4), ("AN220213-01C", 2), ("AN220203-02A", 4)):
            self.add(sku, qty)
        data = order_label.build(self.order)
        self.assertEqual(data["country"], "NZ")
        self.assertEqual(data["font_size"], 12)
        self.assertEqual(data["lines"], ["1. AN220214-01A × 5", "2. AN220215-01A × 4", "3. AN220213-01C × 2", "4. AN220203-02A × 4"])
        xml = order_label.render(data)
        root = ET.fromstring(xml.encode("utf-8"))
        texts = [t.text for t in root.iter("Text")]
        self.assertIn("Order: 102497269", texts)
        self.assertIn("   NZ", texts)                # у рядку номера замовлення
        self.assertNotIn("Pos.", xml)
        first = "1. AN220214-01A × 5"
        self.assertIn(first, texts)
        self.assertEqual(root.find(".//BarcodeObject"), None)  # штрихкода немає
        for span in root.iter("TextSpan"):                    # артикули й країна — жирним
            if span.find("Text").text in ("   NZ", first):
                self.assertEqual(span.find("FontInfo/IsBold").text, "True")
        self.assertEqual(root.find(".//QRCodeObject"), None)
        self.assertEqual(root.find(".//ImageObject"), None)   # без логотипа
        line = root.find(".//TextObject[Name='ITextObject2']//LineTextSpan")
        self.assertEqual([t.text for t in line.iter("Text")], ["Order: 102497269", "   NZ"])
        self.assertNotIn("{{", xml)
        self.assertNotIn("Copyright", xml)

    def test_country_falls_back_to_shipment_and_region(self):
        self.order.addr_country = ""
        self.order.shipping_region = "Germany"
        self.order.save()
        self.assertEqual(order_label.country(self.order), "DE")
        from shipping.models import Carrier, Shipment
        Shipment.objects.create(order=self.order, recipient_country="at",
                                carrier=Carrier.objects.create(name="UPS", carrier_type="ups"))
        self.assertEqual(order_label.country(self.order), "AT")
        self.order.shipping_region = ""
        self.order.save()
        Shipment.objects.all().delete()
        self.assertEqual(order_label.country(self.order), "")

    def test_same_sku_is_summed_and_ordered_sku_is_used(self):
        self.add("AN220214-01A", 2, sku_raw="AN220203-02A")
        self.add("AN220213-01C", Decimal("1.5"))
        self.add("AN220203-02A", 3)
        self.assertEqual(order_label.build(self.order)["items"], [("AN220203-02A", "5"), ("AN220213-01C", "1.5")])

    def test_many_lines_go_to_columns_and_smaller_font(self):
        rows = [(f"AN2202{n:02d}-01A", 1) for n in range(12)]
        size, lines = order_label.layout(rows)
        self.assertLess(size, 10)
        self.assertLess(len(lines), 12)
        self.assertTrue(all(len(line.split("   ")) >= 2 for line in lines[:-1]))
        size, lines = order_label.layout([(f"AN2202{n:03d}-01A-LONG", 1) for n in range(200)])
        self.assertEqual(size, 6)
        self.assertRegex(lines[-1], r"\+\d+ more")

    def test_download_and_buttons(self):
        self.add("AN220214-01A", 5)
        r = self.client.get(f"/labels/order/{self.order.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('filename="Order-102497269.dymo"', r["Content-Disposition"])
        ET.fromstring(r.content)
        r = self.client.get(f"/admin/sales/salesorder/{self.order.pk}/change/")
        self.assertContains(r, 'data-svl="order"')
        r = self.client.get("/admin/sales/salesorder/")          # картки рендеряться завжди, перемикає JS
        self.assertContains(r, f'data-svl="order" data-order="{self.order.pk}" data-sku="102497269"')

    def test_order_without_lines(self):
        self.assertEqual(self.client.get(f"/labels/order/{self.order.pk}/").status_code, 400)
        self.assertEqual(self.client.get("/labels/order/999999/").status_code, 404)


class LabelSettingsTests(TestCase):
    """«Продажі → Налаштування → 🏷️ Етикетки DYMO»: що вікно етикетки антени бере за замовчуванням."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        patcher = mock.patch.object(label_views, "LABELS_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.product = Product.objects.create(
            sku="AN220214-01A", name_export="Multi-purpose UWB PCB antenna", category="antenna",
            tech_attributes={"Frequency Range": "600MHz ~ 7.2GHz", "Termination": "SMA-Female"})
        DigiKeyListing.objects.create(product=self.product, dk_title="RF ANT Omni 0.6-8GHz 5G/LTE SMA",
                                      dk_description="", dk_product_id="u-1")
        cfg = DigiKeyConfig.get()
        cfg.marketplace_supplier_id = "3228"
        cfg.save()
        (self.dir / "AN220214-01A.dymo").write_text(old_label("AN220214-01A", [
            "Multi-purpose UWB PCB antenna.", "Frequency range: 0.650...9 GHz.", "Connector type: SMA female."]),
            encoding="utf-8")
        self.settings = SalesSettings.get()

    def use(self, **values):
        for name, value in values.items():
            setattr(self.settings, name, value)
        self.settings.save()

    def test_old_label_first(self):
        self.assertEqual(product_label.build("AN220214-01A")["fields"]["frequency"]["value"], "0.600...7.2 GHz")
        self.use(antenna_label_sources="file")
        fields = product_label.build("AN220214-01A")["fields"]
        self.assertEqual((fields["frequency"]["value"], fields["frequency"]["source"]), ("0.650...9 GHz", "file"))
        self.assertEqual(fields["connector"]["value"], "SMA female")

    def test_digikey_first(self):
        self.use(antenna_label_sources="digikey")
        self.assertEqual(product_label.build("AN220214-01A")["fields"]["description"]["value"], "RF ANT Omni 0.6-8GHz 5G/LTE SMA")

    def test_digikey_line_default(self):
        shop = SalesOrder.objects.create(source="webshop", order_number="W1")
        self.assertFalse(product_label.build("AN220214-01A", order=shop)["show_dk"])
        self.use(antenna_label_digikey="always")
        self.assertTrue(product_label.build("AN220214-01A", order=shop)["show_dk"])
        self.use(antenna_label_digikey="never")
        digikey = SalesOrder.objects.create(source="digikey", order_number="D1")
        self.assertFalse(product_label.build("AN220214-01A", order=digikey)["show_dk"])
        self.assertTrue(product_label.build("AN220214-01A", order=digikey, show_dk=True)["show_dk"])  # галочка у вікні

    def test_window_mode(self):
        self.assertFalse(product_label.build("AN220214-01A")["ready"])          # перший раз — вікно
        self.use(antenna_label_window="errors")
        self.assertTrue(product_label.build("AN220214-01A")["ready"])           # попередження друк не зупиняють
        product_label.save("AN220214-01A", {})
        self.use(antenna_label_window="always")
        self.assertFalse(product_label.build("AN220214-01A")["ready"])

    def test_confirmed_mixed_units_do_not_reopen_the_window(self):
        """Скриншот 29.10: збережено «600MHz...7.2GHz» до того, як частота стала в одних одиницях."""
        LabelText.objects.create(sku="AN220214-01A", product=self.product, description="Multi-purpose UWB PCB antenna",
                                 frequency="600MHz...7.2GHz", connector="SMA-Female", digikey_pn="",
                                 seen={"description": "Multi-purpose UWB PCB antenna", "frequency": "600MHz...7.2GHz",
                                       "connector": "SMA-Female", "digikey_pn": ""}, checked_at=timezone.now())
        data = product_label.build("AN220214-01A")
        self.assertEqual(data["fields"]["frequency"]["value"], "600MHz...7.2GHz")
        mixed = [i for i in data["issues"] if "різні одиниці" in i["text"]]
        self.assertEqual([i["level"] for i in mixed], ["info"])
        self.assertTrue(data["ready"], data["issues"])

    def test_settings_page_has_label_block(self):
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(User.objects.create_user("staff", password="x", is_staff=True, is_superuser=True))
        r = self.client.get(f"/admin/sales/salessettings/{self.settings.pk}/change/")
        self.assertContains(r, "🏷️ Етикетки DYMO")
        self.assertContains(r, "id_antenna_label_window")
        self.assertContains(r, "id_dymo_print_preview")
        self.assertContains(r, "id_dymo_origin_mark")


class OriginMarkTests(TestCase):
    """Позначка походження в куті етикетки: A — з шаблону, B — файл з бази, E — поправлено вручну."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        patcher = mock.patch.object(label_views, "LABELS_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(User.objects.create_user("staff", password="x", is_staff=True, is_superuser=True))
        self.product = Product.objects.create(
            sku="AN220213-01C", name_export="Multi-purpose UWB PCB antenna",
            tech_attributes={"Frequency Range": "370MHz ~ 6GHz", "Termination": "SMA Female"})
        self.today = timezone.localdate().strftime("%d.%m.%y")

    def marks(self, xml):
        root = ET.fromstring(xml)
        return [o for o in root.iter("TextObject") if o.findtext("Name") == mark.NAME]

    def mark_text(self, xml):
        found = self.marks(xml)
        self.assertEqual(len(found), 1)
        return next(found[0].iter("Text")).text

    def test_stamp_is_in_the_free_strip_and_replaced_not_doubled(self):
        xml = product_label.render({"pn": "AN1", "qty": 1, "year": 2026, "lines": [("", "Antenna.")]})
        once = mark.stamp(xml, "auto", on=True)
        self.assertEqual(self.mark_text(once), "A " + self.today)
        obj = self.marks(once)[0]
        x = float(obj.find("ObjectLayout/DYMOPoint/X").text)
        self.assertGreater(x, 3.363)                         # праворуч від усіх об'єктів наших макетів
        self.assertAlmostEqual(x + float(obj.find("ObjectLayout/Size/Width").text), 0.2233333 + 3.203333, places=3)
        twice = mark.stamp(once, "file", on=True)
        self.assertEqual(self.mark_text(twice), "B " + self.today)
        self.assertEqual(self.marks(mark.stamp(twice, "file", on=False)), [])
        self.assertEqual(mark.stamp("<DieCutLabel/>", "file", on=True), "<DieCutLabel/>")   # старий формат — як є

    def test_every_label_gets_its_mark(self):
        (self.dir / "FT220407-01A.dymo").write_text(
            product_label.render({"pn": "FT220407-01A", "qty": 3, "year": 2025, "lines": []}), encoding="utf-8")
        r = self.client.get("/labels/serve/FT220407-01A/?qty=5")
        self.assertEqual(self.mark_text(r.content), "B " + self.today)
        self.assertIn("QTY: 5 PCS", r.content.decode())
        r = self.client.get("/labels/serve/FT220407-01A/?qty=5&origin=auto")    # кабель, щойно з шаблону
        self.assertEqual(self.mark_text(r.content), "A " + self.today)
        r = self.client.get("/labels/product/AN220213-01C/?qty=2")
        self.assertEqual(self.mark_text(r.content), "A " + self.today)
        order = SalesOrder.objects.create(source="digikey", order_number="102497269")
        from sales.models import SalesOrderLine
        SalesOrderLine.objects.create(order=order, product=self.product, sku_raw="AN220213-01C", qty=2)
        r = self.client.get(f"/labels/order/{order.pk}/")
        self.assertEqual(self.mark_text(r.content), "A " + self.today)
        ss = SalesSettings.get()
        ss.dymo_origin_mark = False
        ss.save()
        self.assertEqual(self.marks(self.client.get("/labels/serve/FT220407-01A/").content), [])
        self.assertEqual(self.marks(self.client.get("/labels/product/AN220213-01C/?qty=2").content), [])

    def test_antenna_text_typed_by_a_person_is_marked_e(self):
        product_label.save("AN220213-01C", {"description": "Multi-purpose UWB PCB antenna", "frequency": "0.370...6 GHz",
                                      "connector": "SMA Female"})
        self.assertEqual(product_label.build("AN220213-01C")["origin"], "auto")      # перевірено, але як на складі
        product_label.save("AN220213-01C", {"description": "UWB antenna, special batch"})
        self.assertEqual(product_label.build("AN220213-01C")["origin"], "edited")
        r = self.client.get("/labels/product/AN220213-01C/?qty=2")
        self.assertEqual(self.mark_text(r.content), "E " + self.today)

    def test_preview_setting_reaches_every_page(self):
        ss = SalesSettings.get()
        self.assertEqual((ss.dymo_print_preview, ss.dymo_origin_mark), ("preview", True))
        self.assertIn("PREVIEW = 'preview' !== 'direct'", self.client.get("/labels/").content.decode())
        ss.dymo_print_preview = "direct"
        ss.save()
        order = SalesOrder.objects.create(source="digikey", order_number="102437554")
        from sales.models import SalesOrderLine
        SalesOrderLine.objects.create(order=order, product=self.product, sku_raw="AN220213-01C", qty=2)
        for url in ("/labels/", "/admin/sales/salesorder/", f"/admin/sales/salesorder/{order.pk}/change/"):
            self.assertContains(self.client.get(url), "PREVIEW = 'direct' !== 'direct'", msg_prefix=url)


def loose_label(*lines):
    """.dymo іншого макета (без Copyright), як старі «ohne Logo» фільтри й адаптери."""
    objects = "".join(f"<TextObject><Name>T{i}</Name><FormattedText><LineTextSpan><TextSpan><Text>{line}</Text>"
                      f"</TextSpan></LineTextSpan></FormattedText></TextObject>" for i, line in enumerate(lines))
    return f"<DesktopLabel><DYMOLabel><DynamicLayoutManager><LabelObjects>{objects}</LabelObjects>" \
           f"</DynamicLayoutManager></DYMOLabel></DesktopLabel>"


class ProductKindTests(TestCase):
    """Один рушій етикеток з шаблону: фільтри (FT…) й інші товари — як антени; файлу немає — з шаблону."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        patcher = mock.patch.object(label_views, "LABELS_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(User.objects.create_user("staff", password="x", is_staff=True, is_superuser=True))
        self.filter = Product.objects.create(
            sku="FT220407-01A", name="Фільтр 2.7–3.65 ГГц", name_export="SMA band pass filter", category="rf_filter",
            tech_attributes={"Pass Band": "2.7GHz ~ 3.65GHz", "Filter Type": "Band Pass", "Termination": "SMA Female"})
        self.adapter = Product.objects.create(sku="AD-FME-M1", name_export="RF adapter RP FME female to U.FL, bulkhead",
                                              category="adapter")

    def test_kind_of(self):
        cases = [("AN220213-01C", ""), ("FT220407-01A", ""), ("X1", "RF Filters"), ("X2", "Antennas"),
                 ("CA-MHF1-MC1.13-175-MHF4", ""), ("CA-???", ""), ("AD-FME-M1", "adapter"), ("", "")]
        self.assertEqual([product_label.kind_of(sku, category) for sku, category in cases],
                         ["antenna", "filter", "filter", "antenna", "cable", "generic", "generic", ""])

    def test_filter_label_like_antenna(self):
        d = product_label.build("FT220407-01A", 3)
        self.assertEqual((d["kind"], d["kind_title"]), ("filter", "фільтра"))
        self.assertTrue(d["can_print"], d["issues"])
        self.assertEqual(d["lines"], [("", "SMA band pass filter."), ("Pass band: ", "2.7...3.65 GHz."),
                                      ("Connector type: ", "SMA Female.")])
        texts = [t.text for t in ET.fromstring(product_label.render(d)).iter("Text")]
        for text in ("P/N: FT220407-01A", "QTY: 3 PCS", "Pass band: 2.7...3.65 GHz.",
                     f"Copyright © 2009 - {timezone.localdate().year} Sevskiy GmbH. "):
            self.assertIn(text, texts)
        self.filter.tech_attributes = {"Stop Band": "680MHz ~ 915MHz", "Filter Type": "Band Stop"}
        self.filter.save()
        d = product_label.build("FT220407-01A")
        self.assertIn(("Stop band: ", "680...915 MHz."), d["lines"])
        levels = {i["text"].split(" — ")[0]: i["level"] for i in d["issues"]}
        self.assertEqual(levels.get("Немає роз'єму"), "info")              # у фільтра роз'єм — за бажанням
        self.filter.tech_attributes = {}
        self.filter.save()
        levels = {i["text"].split(" — ")[0]: i["level"] for i in product_label.build("FT220407-01A")["issues"]}
        self.assertEqual(levels.get("Немає частоти"), "warn")               # а смуга — обов'язково

    def test_old_filter_files_are_read(self):
        (self.dir / "FT220407-01A.dymo").write_text(old_label("FT220407-01A", ["Description: RF filter SMA."]),
                                                    encoding="utf-8")
        self.assertEqual(product_label.read_file("FT220407-01A")["description"], "RF filter SMA")
        (self.dir / "FT220413-01A -ohne Logo.dymo").write_text(loose_label(
            "Description: SMA Band Pass Filter.", "Pass band: 4600 to 6000 MHz.", "P/N: 220413", "QTY: 1 PCS"),
            encoding="utf-8")
        old = product_label.read_file("FT220413-01A")
        self.assertEqual((old["standard"], old["description"], old["frequency"], old["band"]),
                         (False, "SMA Band Pass Filter", "4600...6000 MHz", "pass"))
        d = product_label.build("FT220413-01A")                         # товару немає — дані зі старого файлу
        self.assertEqual(d["kind"], "filter")
        self.assertTrue(d["can_print"], d["issues"])
        self.assertIn(("Pass band: ", "4600...6000 MHz."), d["lines"])

    def test_generic_product(self):
        d = product_label.build("AD-FME-M1", 2)
        self.assertEqual(d["kind"], "generic")
        self.assertTrue(d["can_print"], d["issues"])
        self.assertEqual(d["lines"], [("", "RF adapter RP FME female to U.FL, bulkhead.")])
        self.assertFalse([i for i in d["issues"] if i["level"] in ("warn", "error")], d["issues"])
        long_text = "Universal RF adapter " + " ".join(["SMA female to U.FL"] * 5)        # 3 рядки — можна
        self.assertTrue(product_label.build("AD-FME-M1", edits={"description": long_text})["can_print"])
        too_long = " ".join(["Universal RF adapter SMA female to U.FL"] * 4)
        self.assertFalse(product_label.build("AD-FME-M1", edits={"description": too_long})["can_print"])
        # товару немає на складі, лише файл іншого макета (як AD-FME-…) — опис з його текстів
        (self.dir / "AD-OLD-1.dymo").write_text(loose_label(
            "RF adapter", "Qty: 1 PCS", "RP FME Female to U.FL (base) RF, bulkhead", "P/N: AD-OLD-1 B7/16"),
            encoding="utf-8")
        d = product_label.build("AD-OLD-1")
        self.assertEqual(d["fields"]["description"]["value"], "RF adapter RP FME Female to U.FL (base) RF, bulkhead")
        self.assertTrue(d["can_print"], d["issues"])

    def test_general_rule_template_or_file(self):
        from labels_app.views import uses_template
        self.assertEqual([uses_template(kind, has_file, source) for kind, has_file, source in (
            ("generic", False, "generate"), ("generic", True, "generate"), ("filter", True, "generate"),
            ("filter", True, "file"), ("filter", False, "file"), ("antenna", True, "generate"), ("", False, "generate"))],
            [True, False, True, False, True, True, False])

    def test_every_product_has_a_print_button(self):
        r = self.client.get("/admin/inventory/product/")
        self.assertContains(r, 'data-svl="generic" data-sku="AD-FME-M1"')
        self.assertContains(r, 'data-svl="filter" data-sku="FT220407-01A"')
        self.assertContains(r, "PREVIEW = 'preview' !== 'direct'")        # спільний друк: перегляд, копії, позначка
        order = SalesOrder.objects.create(source="digikey", order_number="102437554")
        from sales.models import SalesOrderLine
        SalesOrderLine.objects.create(order=order, product=self.adapter, sku_raw="AD-FME-M1", qty=2)
        r = self.client.get(f"/admin/sales/salesorder/{order.pk}/change/")
        self.assertContains(r, 'data-svl="generic" data-sku="AD-FME-M1"')
        self.assertContains(r, "🧩 з шаблону товару")
        r = self.client.get("/labels/product/AD-FME-M1/?qty=2")
        self.assertEqual(r.status_code, 200)
        texts = [t.text for t in ET.fromstring(r.content).iter("Text")]
        self.assertIn("RF adapter RP FME female to U.FL, bulkhead.", texts)
        self.assertIn("QTY: 2 PCS", texts)
        r = self.client.get("/labels/product/check/", {"sku": "FT220407-01A"})
        self.assertEqual((r.json()["kind_title"], r.json()["field_names"]["frequency"]),
                         ("фільтра", "Смуга (Pass / Stop band)"))

    def test_order_list_marks_labels_without_file(self):
        """Список замовлень: кнопка етикетки, якої немає в базі (буде з шаблону), — 🧩 і інший колір."""
        from django.contrib.admin.sites import site
        from sales.models import SalesOrderLine
        (self.dir / "FT220407-01A.dymo").write_text(old_label("FT220407-01A", ["RF filter SMA."]), encoding="utf-8")
        order = SalesOrder.objects.create(source="digikey", order_number="102437554")
        SalesOrderLine.objects.create(order=order, product=self.adapter, sku_raw="AD-FME-M1", qty=2)
        SalesOrderLine.objects.create(order=order, product=self.filter, sku_raw="FT220407-01A", qty=1)
        with self.settings(LABELS_DIR=self.dir):                          # колонка шукає файли сама
            html = site._registry[SalesOrder].label_buttons_list(order)
        no_file = html[html.index('data-sku="AD-FME-M1"'):].split("</button>")[0]
        with_file = html[html.index('data-sku="FT220407-01A"'):].split("</button>")[0]
        self.assertIn("🧩 з шаблону — файлу в базі немає", no_file)
        self.assertIn("AD-FME-M1 🧩", no_file)
        self.assertIn("📄 файл з бази", with_file)
        self.assertNotIn("🧩", with_file)
        r = self.client.get("/admin/sales/salesorder/")                  # картки: кнопки будує JS з /labels/status/
        self.assertContains(r, "if (_tmpl) a.classList.add('sv-lbl-tmpl');")
        status = self.client.get("/labels/status/?skus=AD-FME-M1").json()["AD-FME-M1"]
        self.assertEqual((status["found"], status["file"]), (True, False))
