"""Рекомендована упаковка й навчання з відправлень — `shipping/services/packaging.py`.

Позиції, що вміщуються в одну коробку, не розбиваються на окремі посилки, і вага повної
коробки не множиться на кожну позицію.

    python manage.py test shipping.tests_packaging --settings=tabele.settings_test
"""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from inventory.models import Product
from sales.models import SalesOrder, SalesOrderLine
from shipping.models import Carrier, PackagingMaterial, ProductPackaging, Shipment, ShipmentPackage
from shipping.services import packaging


def box(l, w, h, tare="0", max_kg=None):
    return PackagingMaterial.objects.create(length_cm=l, width_cm=w, height_cm=h,
                                            tare_weight_kg=Decimal(tare), max_weight_kg=max_kg)


class Base(TestCase):
    def setUp(self):
        self.a = Product.objects.create(sku="CA-SMA-1", name="Cable A")
        self.b = Product.objects.create(sku="CA-SMA-2", name="Cable B")
        self.x = box(20, 15, 5)
        self.n = 0

    def order(self, *items, number=None):
        self.n += 1
        o = SalesOrder.objects.create(source="webshop", order_number=number or f"9000{self.n}")
        for product, qty in items:
            SalesOrderLine.objects.create(order=o, product=product, sku_raw=product.sku, qty=Decimal(qty))
        return o

    def pp(self, product, material, qpb, weight=None, default=True):
        return ProductPackaging.objects.create(product=product, packaging=material, qty_per_box=qpb,
                                               estimated_weight_g=weight, is_default=default)

    def ship(self, order, status=Shipment.Status.LABEL_READY, dims=(25, 18, 6), kg="0.42", rows=None):
        if not hasattr(self, "carrier"):
            self.carrier = Carrier.objects.create(name="UPS", carrier_type="ups", sender_country="DE")
        s = Shipment.objects.create(order=order, carrier=self.carrier, status=status, weight_kg=Decimal(kg),
                                    length_cm=dims[0], width_cm=dims[1], height_cm=dims[2])
        for r in rows or []:
            ShipmentPackage.objects.create(shipment=s, **r)
        return s


class RecommendTests(Base):
    def test_two_cables_share_one_box(self):
        self.pp(self.a, self.x, 3, weight=400)
        self.pp(self.b, self.x, 3, weight=400)
        rec = packaging.recommend(self.order((self.a, 1), (self.b, 1)))
        self.assertEqual(rec.source, "product_packaging")
        self.assertEqual(rec.total_boxes, 1)
        self.assertEqual(rec.boxes[0].lines, 2)
        # 400 г — коробка на 3 кабелі; два кабелі ≈ 2/3 від неї, а не 2 × 400 г
        self.assertAlmostEqual(float(rec.weight_g), 266.67, places=1)
        self.assertTrue(rec.approx_weight)

    def test_net_weight_and_one_tare(self):
        x = box(20, 15, 5, tare="0.05")
        self.a.net_weight_g, self.b.net_weight_g = Decimal(80), Decimal(70)
        self.a.save(), self.b.save()
        self.pp(self.a, x, 3)
        self.pp(self.b, x, 3)
        rec = packaging.recommend(self.order((self.a, 1), (self.b, 1)))
        self.assertEqual(rec.total_boxes, 1)
        self.assertEqual(rec.weight_g, Decimal(200))          # 80 + 70 + 50 (одна коробка)
        self.assertFalse(rec.approx_weight)

    def test_decimal_quantity_rounds_up(self):
        self.pp(self.a, self.x, 5)
        rec = packaging.recommend(self.order((self.a, 6)))
        self.assertEqual(rec.total_boxes, 2)                  # було 1: Decimal // округлює до нуля

    def test_same_product_on_two_lines(self):
        self.pp(self.a, self.x, 2)
        rec = packaging.recommend(self.order((self.a, 1), (self.a, 1)))
        self.assertEqual(rec.total_boxes, 1)

    def test_capacity_is_largest_known_for_the_box(self):
        self.pp(self.a, self.x, 1)
        self.pp(self.a, self.x, 2, default=False)
        self.pp(self.b, self.x, 2)
        rec = packaging.recommend(self.order((self.a, 1), (self.b, 1)))
        self.assertEqual(rec.total_boxes, 1)

    def test_default_too_small_picks_box_that_holds_all(self):
        y = box(30, 20, 10)
        self.pp(self.a, self.x, 1)
        self.pp(self.a, y, 5, default=False)
        rec = packaging.recommend(self.order((self.a, 5)))
        self.assertEqual((rec.box, rec.total_boxes), (y, 1))
        self.assertTrue(rec.lines[0].auto)

    def test_different_boxes_merge_into_bigger_one(self):
        y = box(30, 20, 10)
        self.pp(self.a, self.x, 1)
        self.pp(self.b, y, 4)
        rec = packaging.recommend(self.order((self.a, 1), (self.b, 1)))
        self.assertEqual((rec.box, rec.total_boxes), (y, 1))

    def test_no_merge_when_item_does_not_fit(self):
        tube = box(60, 8, 8)
        y = box(30, 20, 10)
        self.pp(self.a, tube, 1)
        self.pp(self.b, y, 4)
        rec = packaging.recommend(self.order((self.a, 1), (self.b, 1)))
        self.assertEqual(rec.total_boxes, 2)

    def test_max_weight_adds_boxes(self):
        x = box(20, 15, 5, max_kg=Decimal("1"))
        self.a.net_weight_g = Decimal(500)
        self.a.save()
        self.pp(self.a, x, 10)
        rec = packaging.recommend(self.order((self.a, 3)))
        self.assertEqual(rec.total_boxes, 2)

    def test_product_without_packaging_adds_weight(self):
        self.b.net_weight_g = Decimal(100)
        self.b.save()
        self.pp(self.a, self.x, 3)
        rec = packaging.recommend(self.order((self.a, 1), (self.b, 1)))
        self.assertTrue(rec.no_packaging)
        self.assertEqual(rec.total_boxes, 1)
        self.assertEqual(rec.weight_g, Decimal(100))


class HistoryTests(Base):
    def test_same_contents_reuse_last_shipment(self):
        self.pp(self.a, self.x, 1)
        self.pp(self.b, self.x, 1)                            # окремо — 2 коробки
        past = self.ship(self.order((self.a, 1), (self.b, 1)))
        rec = packaging.recommend(self.order((self.b, 1), (self.a, 1)))
        self.assertEqual(rec.source, "history")
        self.assertEqual(rec.shipment, past)
        self.assertEqual(rec.total_boxes, 1)
        self.assertEqual(rec.weight_g, Decimal(420))
        self.assertEqual(rec.rows(), [{"weight_kg": 0.42, "length_cm": 25.0, "width_cm": 18.0,
                                       "height_cm": 6.0, "quantity": 1}])

    def test_package_rows_are_copied(self):
        past = self.ship(self.order((self.a, 4)), rows=[
            {"weight_kg": Decimal("1.1"), "length_cm": 30, "width_cm": 20, "height_cm": 10, "quantity": 2}])
        rec = packaging.recommend(self.order((self.a, 4)))
        self.assertEqual((rec.shipment, rec.total_boxes), (past, 2))
        self.assertEqual(rec.weight_g, Decimal("2200"))

    def test_other_contents_or_bad_status_not_used(self):
        self.ship(self.order((self.a, 1)))
        self.ship(self.order((self.a, 2), (self.b, 1)))
        self.ship(self.order((self.a, 1), (self.b, 1)), status=Shipment.Status.CANCELLED)
        self.ship(self.order((self.a, 1), (self.b, 1)), status=Shipment.Status.DRAFT)
        split = self.order((self.a, 1), (self.b, 1))
        self.ship(split)
        self.ship(split)                                      # два відправлення — невідомо, що в якому
        rec = packaging.recommend(self.order((self.a, 1), (self.b, 1)))
        self.assertNotEqual(rec.source, "history")


class LearnTests(Base):
    def test_box_with_two_products_learns_shared_capacity(self):
        s = self.ship(self.order((self.a, 1), (self.b, 1)))
        plan = packaging.learn_plan(s)
        self.assertEqual(sorted((c["sku"], c["qty_per_box"]) for c in plan),
                         [("CA-SMA-1", 2), ("CA-SMA-2", 2)])
        packaging.learn(s)
        self.assertEqual(packaging.learn_plan(s), [])         # уже знає
        rec = packaging.recommend(self.order((self.a, 2)))
        self.assertEqual((rec.source, rec.total_boxes), ("product_packaging", 1))
        self.assertEqual(rec.weight_g, Decimal(420))

    def test_learns_bigger_capacity_of_known_box(self):
        self.pp(self.a, box(25, 18, 6), 1)
        s = self.ship(self.order((self.a, 3)))
        plan = packaging.learn_plan(s)
        self.assertEqual([(c["sku"], c["qty_per_box"]) for c in plan], [("CA-SMA-1", 3)])
        created = packaging.learn(s)
        self.assertEqual(len(created), 1)
        self.assertEqual(ProductPackaging.objects.filter(product=self.a).count(), 2)
        self.assertEqual(PackagingMaterial.objects.filter(length_cm=25).count(), 1)

    def test_rotated_dimensions_reuse_material(self):
        s = self.ship(self.order((self.a, 1)), dims=(5, 20, 15))
        packaging.learn(s)
        self.assertEqual(ProductPackaging.objects.get(product=self.a).packaging, self.x)

    def test_multiple_box_types_without_distribution_not_learned(self):
        s = self.ship(self.order((self.a, 3)), rows=[
            {"weight_kg": 1, "length_cm": 30, "width_cm": 20, "height_cm": 10, "quantity": 1},
            {"weight_kg": 1, "length_cm": 20, "width_cm": 15, "height_cm": 5, "quantity": 1}])
        self.assertEqual(packaging.learn_plan(s), [])

    def test_distribution_per_box(self):
        o = self.order((self.a, 3), (self.b, 1))
        s = self.ship(o, rows=[
            {"weight_kg": 1, "length_cm": 30, "width_cm": 20, "height_cm": 10, "quantity": 1,
             "items_distribution": {"items": [{"index": 0, "quantity": 3}]}},
            {"weight_kg": Decimal("0.3"), "length_cm": 20, "width_cm": 15, "height_cm": 5, "quantity": 1,
             "items_distribution": {"items": [{"index": 1, "quantity": 1}]}}])
        s.customs_articles = {"customs_line_items": [{"sku": "CA-SMA-1"}, {"sku": "ca-sma-2"}]}
        s.save()
        plan = sorted((c["sku"], c["qty_per_box"], c["weight_g"]) for c in packaging.learn_plan(s))
        self.assertEqual(plan, [("CA-SMA-1", 3, 1000), ("CA-SMA-2", 1, 300)])

    def test_draft_not_learned(self):
        s = self.ship(self.order((self.a, 1)), status=Shipment.Status.DRAFT)
        self.assertEqual(packaging.learn_plan(s), [])


class ViewTests(Base):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.user = User.objects.create_user("boss", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)

    def test_save_packaging_requires_post_and_right(self):
        s = self.ship(self.order((self.a, 1), (self.b, 1)))
        url = f"/admin/shipping/shipment/{s.pk}/save-packaging/"
        self.assertEqual(self.client.get(url).status_code, 405)
        with mock.patch.object(User, "has_perm", return_value=False):
            self.assertEqual(self.client.post(url).status_code, 403)
        self.assertFalse(ProductPackaging.objects.exists())
        r = self.client.post(url)
        self.assertEqual(len(r.json()["created"]), 2)
        self.assertEqual(set(ProductPackaging.objects.values_list("qty_per_box", flat=True)), {2})

    def test_sync_preview_writes_nothing(self):
        from shipping.models import ShippingSettings
        ShippingSettings.objects.get_or_create(pk=1)
        self.ship(self.order((self.a, 1)))                    # старіше: 1 шт — менше, нового нічого
        self.ship(self.order((self.a, 1), (self.b, 1)))
        url = "/admin/shipping/shippingsettings/1/sync-packaging/"
        r = self.client.get(url)
        self.assertEqual(r.json()["total"], 2)
        self.assertFalse(ProductPackaging.objects.exists())
        self.assertFalse(PackagingMaterial.objects.filter(length_cm=25).exists())
        r = self.client.post(url)
        self.assertEqual(r.json()["created"], 2)
        self.assertEqual(ProductPackaging.objects.count(), 2)

    def test_create_shipment_form_uses_one_box(self):
        self.pp(self.a, self.x, 3, weight=400)
        self.pp(self.b, self.x, 3, weight=400)
        o = self.order((self.a, 1), (self.b, 1))
        r = self.client.get(f"/admin/shipping/shipment/create/{o.pk}/")
        self.assertEqual(r.status_code, 200)
        rows = json.loads(r.context["pkg_rows_json"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quantity"], 1)
        self.assertEqual(r.context["packaging_hint"]["total_boxes"], 1)

    def test_order_panel(self):
        from sales.admin import SalesOrderAdmin
        from django.contrib import admin
        self.pp(self.a, self.x, 3, weight=400)
        self.pp(self.b, self.x, 3, weight=400)
        o = self.order((self.a, 1), (self.b, 1))
        html = SalesOrderAdmin(SalesOrder, admin.site).packaging_panel(o)
        self.assertIn("(2 позиції разом)", html)
        self.assertIn("&quot;qty_boxes&quot;: 1", html)
