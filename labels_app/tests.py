import json
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from bots.models import DigiKeyConfig, DigiKeyListing
from inventory.models import Product
from labels_app import antenna
from labels_app import views as label_views
from labels_app.models import LabelText
from sales.models import SalesOrder, SalesSettings


def old_label(pn, lines, year=2024, qty=3, barcode=None, qr=None):
    """Старий .dymo у форматі наших етикеток (той самий макет, лише інші тексти)."""
    data = {"pn": pn, "qty": qty, "year": year, "lines": [("", l) for l in lines]}
    xml = antenna.render(data)
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
        data = antenna.build("AN220213-01C", 2, order=order)
        xml = antenna.render(data)
        root = ET.fromstring(xml.encode("utf-8"))
        texts = [t.text for t in root.iter("Text")]
        self.assertIn("P/N: AN220213-01C", texts)
        self.assertIn("QTY: 2 PCS", texts)
        self.assertIn("Digi-Key P/N: 3228-AN220213-01C-ND.", texts)
        self.assertIn("Multi-purpose UWB PCB antenna.", texts)
        self.assertIn("Frequency range: 370MHz...6GHz.", texts)
        self.assertIn("Connector type: SMA Female.", texts)
        self.assertIn(f"Copyright © 2009 - {self.year} Sevskiy GmbH. ", texts)
        self.assertEqual([d.text for d in root.iter("DataString") if d.text], ["AN220213-01C", "AN220213-01C"])
        self.assertEqual(root.find(".//QRCodeObject/TextDataHolder/Value").text, "AN220213-01C")
        self.assertNotIn("{{", xml)
        self.assertNotIn("<!--", xml)

    def test_render_escapes_xml(self):
        data = {"pn": "AN1", "qty": 1, "year": 2026, "lines": [("Frequency: ", "2400...2500 & 4750…7500 MHz.")]}
        root = ET.fromstring(antenna.render(data).encode("utf-8"))
        self.assertIn("Frequency: 2400...2500 & 4750…7500 MHz.", [t.text for t in root.iter("Text")])

    # ── старий файл ──────────────────────────────────────────────────────────

    def test_read_file_parses_our_label_layout(self):
        self.write("AN250202-01P_noDGkey.dymo", old_label("AN250202-01P", [
            "Bands: UMTS, ISM2400, Wi-Fi 5/6 GHz, ISM5800.",
            "Frequency:  1920...2170, 2400...2500,", "5150…7125 MHz.", "Connector type: SMA male"]))
        f = antenna.read_file("AN250202-01P")
        self.assertEqual(f["description"], "Bands: UMTS, ISM2400, Wi-Fi 5/6 GHz, ISM5800")
        self.assertEqual(f["frequency"], "1920...2170, 2400...2500, 5150…7125 MHz")
        self.assertEqual(f["connector"], "SMA male")
        self.assertEqual((f["year"], f["qty"], f["pn"]), (2024, 3, "AN250202-01P"))

    def test_read_file_digikey_line_variants(self):
        for line in ("Digi-Key P/N: 3228-AN1-ND.", "Digi Key P/N:3228-AN1-ND.", "DigiKey P/N: 3228-AN1-ND.",
                     "Digi-Key PIN: 3228-AN1-ND.", "P/N: 3228-AN1-ND."):
            self.write("AN100001.dymo", old_label("AN100001", [line, "Some antenna."]))
            f = antenna.read_file("AN100001")
            self.assertEqual(f["digikey_pn"], "3228-AN1-ND", line)
            self.assertEqual(f["description"], "Some antenna", line)

    def test_old_file_mistakes_are_reported_and_fixed(self):
        self.write("AN220213-01C.dymo", old_label("AN220203-02A", [
            "Digi-Key P/N: 3228-AN220203-02A-ND.", "Multi-purpose UWB PCB antenna."], year=2024, qr="AN110504-02C"))
        self.listing()
        data = antenna.build("AN220213-01C", 1)
        texts = " | ".join(i["text"] for i in data["issues"] if i["level"] == "info")
        self.assertIn("P/N: AN220203-02A", texts)
        self.assertIn("QR: AN110504-02C", texts)
        self.assertIn("Digi-Key P/N 3228-AN220203-02A-ND", texts)
        self.assertIn(f"рік 2024 — на новій етикетці {self.year}", texts)
        xml = antenna.render(data)
        self.assertNotIn("AN220203-02A", xml)
        self.assertNotIn("AN110504-02C", xml)

    def test_file_is_fallback_when_stock_and_digikey_are_empty(self):
        self.product.name_export = ""
        self.product.tech_attributes = {}
        self.product.save()
        self.write("AN220213-01C.dymo", old_label("AN220213-01C", [
            "Multi-purpose UWB PCB antenna.", "Frequency range: 0.370...6 GHz.", "Connector type: SMA female."]))
        fields = antenna.build("AN220213-01C")["fields"]
        self.assertEqual(fields["frequency"]["value"], "0.370...6 GHz")
        self.assertEqual(fields["frequency"]["source"], "file")
        self.assertEqual(fields["description"]["value"], "Multi-purpose UWB PCB antenna")

    # ── джерела ───────────────────────────────────────────────────────────────

    def test_stock_first_then_digikey(self):
        self.listing(dk_attributes={"Frequency Range": "400MHz ~ 6GHz"})
        data = antenna.build("AN220213-01C")
        fields = data["fields"]
        self.assertEqual((fields["description"]["value"], fields["description"]["source"]),
                         ("Multi-purpose UWB PCB antenna", "stock"))
        self.assertEqual(fields["frequency"]["value"], "370MHz...6GHz")
        self.assertTrue(any("🔷 DigiKey «400MHz...6GHz»" in i["text"] for i in data["issues"] if i["level"] == "warn"))

    def test_cyrillic_stock_name_is_skipped_for_digikey_title(self):
        self.product.name_export = ""
        self.product.save()
        self.listing()
        self.assertEqual(antenna.build("AN220213-01C")["fields"]["description"]["value"], "UWB PCB Antenna")

    def test_missing_description_blocks_print(self):
        self.product.name_export = ""
        self.product.save()
        data = antenna.build("AN220213-01C")
        self.assertFalse(data["can_print"])

    def test_too_many_lines_blocks_print(self):
        self.listing()
        long = "ISM, RFID, IoT (Sigfox, LoRa), LP-WAN, Bluetooth, Wi-Fi 2.4/5GHz, 5G NR, LTE"
        data = antenna.build("AN220213-01C", order=SalesOrder.objects.create(source="digikey", order_number="1"),
                             edits={"description": long})
        self.assertEqual(len(data["lines"]), 5)
        self.assertFalse(data["can_print"])
        self.assertTrue(antenna.build("AN220213-01C", show_dk=False, edits={"description": long})["can_print"])

    # ── Digi-Key P/N ──────────────────────────────────────────────────────────

    def test_digikey_line_follows_order_source(self):
        self.listing(dk_supplier_sku="AN220213-01C-160-MHF1")
        dk = antenna.build("AN220213-01C", order=SalesOrder.objects.create(source="digikey", order_number="1"))
        shop = antenna.build("AN220213-01C", order=SalesOrder.objects.create(source="webshop", order_number="2"))
        self.assertEqual(dk["digikey_pn"], "3228-AN220213-01C-160-MHF1-ND")
        self.assertTrue(dk["show_dk"])
        self.assertFalse(shop["show_dk"])
        self.assertTrue(antenna.build("AN220213-01C", show_dk=True)["show_dk"])   # галочка у вікні

    def test_draft_listing_has_no_digikey_pn(self):
        self.listing(dk_product_id="", sync_status="draft")
        data = antenna.build("AN220213-01C", show_dk=True)
        self.assertEqual(data["digikey_pn"], "")
        self.assertFalse(data["show_dk"])
        self.assertTrue(any("не опубліковано" in i["text"] for i in data["issues"]))

    def test_order_line_digikey_sku_finds_product(self):
        self.listing(dk_supplier_sku="AN220213-01C-X")
        self.assertEqual(antenna.build("AN220213-01C-X")["pn"], "AN220213-01C")

    # ── перевірено людиною ────────────────────────────────────────────────────

    def test_save_then_ready_until_stock_changes(self):
        self.listing()
        self.assertFalse(antenna.build("AN220213-01C")["ready"])
        antenna.save("AN220213-01C", {"frequency": "0.370...6 GHz"}, user=User(username="slava"))
        text = LabelText.objects.get(product=self.product)
        self.assertEqual((text.frequency, text.checked_by), ("0.370...6 GHz", "slava"))
        data = antenna.build("AN220213-01C")
        self.assertEqual(data["fields"]["frequency"]["source"], "saved")
        self.assertTrue(data["ready"])
        self.product.tech_attributes = {"Frequency Range": "300MHz ~ 6GHz", "Termination": "SMA Female"}
        self.product.save()
        data = antenna.build("AN220213-01C")
        self.assertFalse(data["ready"])
        self.assertTrue(any("під час перевірки було «370MHz...6GHz»" in i["text"] for i in data["issues"]))

    def test_deliberately_empty_connector_stays_empty(self):
        antenna.save("AN220213-01C", {"connector": ""})
        data = antenna.build("AN220213-01C")
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
        self.assertEqual((d["FT220407-01A"]["found"], d["FT220407-01A"]["generate"]), (True, ""))
        self.assertFalse(d["XX-1"]["found"])

    def test_check_save_and_download(self):
        r = self.client.get("/labels/antenna/check/", {"sku": "AN220213-01C", "qty": 2, "order": self.order.pk})
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertFalse(d["ready"])
        self.assertIn("/labels/antenna/AN220213-01C/?qty=2", d["url"])
        r = self.client.post("/labels/antenna/save/", json.dumps({
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
        r = self.client.post("/labels/antenna/save/", json.dumps({"sku": "AN220213-01C", "description": ""}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(LabelText.objects.exists())
        self.assertEqual(self.client.get("/labels/antenna/AN220213-01C/?qty=0").status_code, 400)

    def test_save_needs_csrf(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        r = client.post("/labels/antenna/save/", json.dumps({"sku": "AN220213-01C"}), content_type="application/json")
        self.assertEqual(r.status_code, 403)

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
