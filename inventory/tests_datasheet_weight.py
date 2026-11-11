"""⚖️ Вага нетто з даташита (inventory/services/datasheet_weight.py, inventory/admin_weight.py) і на сторінці
нового відправлення (shipping/services/weight_check.py).

    python manage.py test inventory.tests_datasheet_weight --settings=tabele.settings_test
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from inventory.models import Product
from inventory.services import datasheet_weight as dw


class FindTests(TestCase):
    def test_patterns(self):
        cases = [
            ("Mechanical\nWeight: approx. 5.3 g\nColour black", Decimal("5.3")),
            ("Gewicht 12g", Decimal("12")),
            ("Mass (g) 3,2", Decimal("3.2")),
            ("Net weight 0.012 kg", Decimal("12")),
            ("Weight [mg]: 850", Decimal("0.85")),
            ("Вага 1,5 г", Decimal("1.5")),
            ("Weight 0.5 oz", Decimal("14.1748")),
        ]
        for text, grams in cases:
            found = dw.find([text])
            self.assertTrue(found, text)
            self.assertEqual(found[0]["grams"].normalize(), grams.normalize(), text)
            self.assertEqual(found[0]["page"], 1)

    def test_not_weight(self):
        self.assertEqual(dw.find(["Cable weight 25 g/m", "Frequency 2.4 GHz", "Weight class A"]), [])

    def test_same_value_counted_once(self):
        found = dw.find(["Weight: 5 g", "Weight 5g; Mass 7 g"])
        self.assertEqual([(c["grams"].normalize(), c["hits"]) for c in found],
                         [(Decimal("5"), 2), (Decimal("7"), 1)])


class CheckSaveTests(TestCase):
    def setUp(self):
        self.p = Product.objects.create(sku="AN220213-01C", name="Antenna")

    def test_found_does_not_write(self):
        with mock.patch.object(dw, "datasheet_pages", return_value=(["Weight: 5.3 g"], "file")):
            r = dw.check(self.p)
        self.assertEqual((r["status"], r["candidates"][0]["grams"]), ("found", Decimal("5.3000")))
        self.p.refresh_from_db()
        self.assertIsNone(self.p.net_weight_g)

    def test_none_is_remembered(self):
        with mock.patch.object(dw, "datasheet_pages", return_value=(["Frequency 2.4 GHz"], "file")):
            self.assertEqual(dw.check(self.p)["status"], "none")
        self.p.refresh_from_db()
        self.assertIn("ваги немає", self.p.net_weight_note)
        self.assertIsNotNone(self.p.net_weight_checked_at)

    def test_no_datasheet(self):
        with mock.patch.object(dw, "datasheet_pages", side_effect=dw.NoDatasheet("даташита немає")):
            self.assertEqual(dw.check(self.p)["status"], "no_datasheet")
        self.p.refresh_from_db()
        self.assertIn("Даташита немає", self.p.net_weight_note)

    def test_save_sources(self):
        dw.save(self.p, "5,3", "datasheet", "«Weight: 5.3 g» (с. 2)")
        self.p.refresh_from_db()
        self.assertEqual((self.p.net_weight_g, self.p.net_weight_source), (Decimal("5.3000"), "datasheet"))
        self.assertTrue(self.p.net_weight_note.startswith("📄"))
        with self.assertRaises(ValueError):
            dw.save(self.p, "0", "manual")
        dw.save(self.p, "6", "manual")
        self.p.refresh_from_db()
        self.assertEqual(self.p.net_weight_source, "manual")


class ViewsTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.boss = User.objects.create_superuser("boss", "b@x.y", "x")
        self.p = Product.objects.create(sku="AN1", name="Antenna")
        self.check = f"/admin/inventory/product/{self.p.pk}/weight-check/"
        self.save_url = f"/admin/inventory/product/{self.p.pk}/weight-save/"

    def test_check_and_save(self):
        self.client.force_login(self.boss)
        with mock.patch.object(dw, "datasheet_pages", return_value=(["Weight: 5.3 g"], "file")):
            j = self.client.post(self.check).json()
        self.assertEqual((j["status"], j["candidates"][0]["grams"]), ("found", "5.3"))
        j = self.client.post(self.save_url, {"grams": "5.3", "source": "datasheet", "note": "«Weight: 5.3 g»"}).json()
        self.assertEqual((j["ok"], j["kg"], j["sku"]), (True, "0.0053", "AN1"))
        self.assertEqual(self.client.get(self.check).status_code, 405)

    def test_no_permission(self):
        viewer = User.objects.create_user("viewer", password="x", is_staff=True)
        self.client.force_login(viewer)
        with mock.patch("django.contrib.auth.models.User.has_perm", return_value=False):
            r = self.client.post(self.save_url, {"grams": "5"})
        self.assertIn(r.status_code, (302, 403))
        self.p.refresh_from_db()
        self.assertIsNone(self.p.net_weight_g)

    def test_product_card_shows_panel_and_marks_manual(self):
        self.client.force_login(self.boss)
        page = self.client.get(f"/admin/inventory/product/{self.p.pk}/change/")
        self.assertContains(page, "Знайти вагу в даташиті")


class ShipmentPageTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        from sales.models import SalesOrder, SalesOrderLine
        from shipping.models import Carrier, OrderPackaging, PackagingMaterial
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.boss = User.objects.create_superuser("boss", "b@x.y", "x")
        Carrier.objects.create(name="UPS", carrier_type="ups", is_active=True)
        self.order = SalesOrder.objects.create(source="digikey", order_number="102827879", affects_stock=False)
        self.with_w = Product.objects.create(sku="AN1", name="A", net_weight_g=Decimal("20"),
                                             net_weight_source="datasheet")
        self.without = Product.objects.create(sku="AN2", name="B")
        SalesOrderLine.objects.create(order=self.order, product=self.with_w, sku_raw="AN1", qty=2)
        SalesOrderLine.objects.create(order=self.order, product=self.without, sku_raw="AN2", qty=1)
        box = PackagingMaterial.objects.create(name="Mietel gros v2", length_cm=30, width_cm=20, height_cm=10,
                                               tare_weight_kg=Decimal("0.120"))
        OrderPackaging.objects.create(order=self.order, packaging=box, qty_boxes=1, actual_weight_g=170)

    def test_breakdown(self):
        from shipping.services.weight_check import breakdown
        d = breakdown(self.order)
        self.assertEqual((d["tare_g"], d["product_g"], d["estimate_g"], d["missing"]),
                         (Decimal("120"), Decimal("40"), Decimal("160"), 1))

    def test_page(self):
        self.client.force_login(self.boss)
        page = self.client.get(f"/admin/shipping/shipment/create/{self.order.pk}/")
        self.assertContains(page, "Вага посилки:")
        self.assertContains(page, "тара 120 г")
        self.assertContains(page, f"/admin/inventory/product/{self.without.pk}/weight-check/")
