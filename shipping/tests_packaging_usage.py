"""📊 Коробки: використання, схожі розміри, злиття — `shipping/services/packaging_usage.py` і сторінка
«📊 Використання й схожі» (`PackagingMaterialAdmin.usage_view`).

    python manage.py test shipping.tests_packaging_usage --settings=tabele.settings_test
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from inventory.models import Product
from sales.models import SalesOrder
from shipping.models import (Carrier, OrderPackaging, PackagingMaterial, PackagingMovement, ProductPackaging,
                             Shipment, ShipmentPackage)
from shipping.services import packaging_usage as pu


class Base(TestCase):
    def setUp(self):
        self.addCleanup(cache.clear)
        cache.clear()
        from core.shared_cache import cache as shared
        shared.delete_many([f"pack-usage:{d}" for d in (30, 90, 180, 365, 730)])
        self.carrier = Carrier.objects.create(name="UPS", carrier_type="ups", sender_country="DE")
        self.big = PackagingMaterial.objects.create(name="Big", code="BX-1", length_cm=30, width_cm=20,
                                                    height_cm=10, track_stock=True)
        self.twin = PackagingMaterial.objects.create(name="Big twin", length_cm=Decimal("30.5"), width_cm=20,
                                                     height_cm=Decimal("10.5"), track_stock=True)
        self.small = PackagingMaterial.objects.create(name="Small", length_cm=15, width_cm=10, height_cm=5)
        self.n = 0

    def shipment(self, dims=(30, 20, 10), status=Shipment.Status.LABEL_READY, rows=None, direction="out"):
        self.n += 1
        order = SalesOrder.objects.create(source="webshop", order_number=f"9100{self.n}")
        s = Shipment.objects.create(order=order, carrier=self.carrier, status=status, weight_kg=Decimal("1"),
                                    length_cm=dims[0], width_cm=dims[1], height_cm=dims[2], direction=direction)
        for r in rows or []:
            ShipmentPackage.objects.create(shipment=s, **r)
        return s


class UsageTests(Base):
    def test_counts_boxes_shipments_and_unmatched_sizes(self):
        self.shipment()                                                       # 1 × Big
        self.shipment(rows=[{"length_cm": 30, "width_cm": 20, "height_cm": 10, "quantity": 3},
                            {"length_cm": 15, "width_cm": 10, "height_cm": 5}])      # 3 × Big + 1 × Small
        self.shipment(dims=(50, 40, 40))                                     # такої коробки немає
        self.shipment(status=Shipment.Status.DRAFT)                         # чернетка — не рахується
        self.shipment(direction="in")                                        # посилка до нас — ні
        data = pu.usage(365)
        big = data["materials"][self.big.pk]
        self.assertEqual((big["boxes"], big["shipments"]), (4, 2))
        self.assertEqual(data["materials"][self.small.pk]["boxes"], 1)
        self.assertEqual(data["materials"][self.twin.pk]["boxes"], 0)
        self.assertEqual(data["shipments"], 3)
        self.assertEqual(data["unmatched"][0]["dims"], "50 × 40 × 40")
        self.assertEqual(data["unmatched"][0]["boxes"], 1)

    def test_recommendations_and_packed_orders_are_counted(self):
        p = Product.objects.create(sku="AN-1", name="Antenna")
        ProductPackaging.objects.create(product=p, packaging=self.big, qty_per_box=10)
        OrderPackaging.objects.create(order=SalesOrder.objects.create(source="webshop", order_number="X1"),
                                      packaging=self.big, qty_boxes=2)
        st = pu.usage(30)["materials"][self.big.pk]
        self.assertEqual((st["recs"], st["orders"]), (1, 2))
        self.assertIn("рекомендацій товарів 1", pu.stats_line(st))


class SimilarTests(Base):
    def test_groups_within_tolerance_and_same_type_only(self):
        self.shipment()
        groups = pu.similar(1)
        self.assertEqual(len(groups), 1)
        self.assertEqual([m.pk for m in groups[0]["members"]], [self.big.pk, self.twin.pk])   # вживана — ціль
        self.assertEqual(groups[0]["spread"], ["0.5", "0", "0.5"])
        self.assertEqual(pu.similar(Decimal("0.4")), [])
        PackagingMaterial.objects.filter(pk=self.twin.pk).update(box_type=PackagingMaterial.BoxType.ENVELOPE)
        self.assertEqual(pu.similar(1), [])                                   # конверт з коробкою — ні

    def test_archived_only_on_request(self):
        PackagingMaterial.objects.filter(pk=self.twin.pk).update(is_active=False)
        self.assertEqual(pu.similar(1), [])
        self.assertEqual(len(pu.similar(1, include_inactive=True)), 1)


class MergeTests(Base):
    def test_merge_moves_recommendations_orders_stock_and_archives(self):
        user = User.objects.create_superuser("boss", "b@x.y", "p")
        a = Product.objects.create(sku="AN-1", name="A")
        b = Product.objects.create(sku="AN-2", name="B")
        ProductPackaging.objects.create(product=a, packaging=self.twin, qty_per_box=20, is_default=True)
        ProductPackaging.objects.create(product=b, packaging=self.big, qty_per_box=5, is_default=False)
        ProductPackaging.objects.create(product=b, packaging=self.twin, qty_per_box=8, is_default=True)
        op = OrderPackaging.objects.create(order=SalesOrder.objects.create(source="webshop", order_number="X2"),
                                           packaging=self.twin)
        PackagingMaterial.objects.filter(pk=self.big.pk).update(stock_qty=4)
        PackagingMaterial.objects.filter(pk=self.twin.pk).update(stock_qty=3)
        out = pu.merge(self.big, [self.twin], user=user)
        self.assertEqual((out["moved_recs"], out["dropped_recs"], out["moved_orders"], out["stock"]), (1, 1, 1, 3))
        self.assertEqual(ProductPackaging.objects.get(product=a).packaging_id, self.big.pk)
        kept = ProductPackaging.objects.get(product=b)                       # лишилась ціль, з кращим
        self.assertEqual((kept.packaging_id, kept.qty_per_box, kept.is_default), (self.big.pk, 8, True))
        op.refresh_from_db()
        self.assertEqual(op.packaging_id, self.big.pk)
        twin = PackagingMaterial.objects.get(pk=self.twin.pk)
        big = PackagingMaterial.objects.get(pk=self.big.pk)
        self.assertEqual((twin.is_active, twin.stock_qty, big.stock_qty), (False, 0, 7))
        self.assertIn(f"злито в #{self.big.pk}", twin.notes)
        self.assertEqual(PackagingMovement.objects.filter(kind="adjust").count(), 2)

    def test_refuses_other_type_and_nothing_to_merge(self):
        PackagingMaterial.objects.filter(pk=self.twin.pk).update(box_type=PackagingMaterial.BoxType.ENVELOPE)
        self.twin.refresh_from_db()
        with self.assertRaises(pu.MergeError):
            pu.merge(self.big, [self.twin])
        with self.assertRaises(pu.MergeError):
            pu.merge(self.big, [self.big])
        self.assertTrue(PackagingMaterial.objects.get(pk=self.twin.pk).is_active)


class UsagePageTests(Base):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.url = reverse("admin:shipping_packagingmaterial_usage")
        self.boss = User.objects.create_superuser("boss", "b@x.y", "p")

    def viewer(self):
        u = User.objects.create_user("ro", "ro@x.y", "p", is_staff=True)
        u.profile.role = "readonly"
        u.profile.allowed_modules = ["shipping"]
        u.profile.save()
        return User.objects.get(pk=u.pk)

    def test_page_shows_usage_groups_and_merges(self):
        self.shipment()
        self.client.force_login(self.boss)
        r = self.client.get(self.url + "?days=90&tol=1")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Big twin")
        self.assertContains(r, "Злити вибрані в ціль")
        r = self.client.post(self.url, {"action": "merge", "target": self.big.pk, "merge": [self.twin.pk]})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(PackagingMaterial.objects.get(pk=self.twin.pk).is_active)

    def test_list_has_usage_column_and_link(self):
        self.client.force_login(self.boss)
        r = self.client.get(reverse("admin:shipping_packagingmaterial_changelist"))
        self.assertContains(r, self.url)

    def test_viewer_sees_page_but_cannot_merge(self):
        user = self.viewer()
        if not user.has_perm("shipping.view_packagingmaterial"):
            self.skipTest("роль readonly без розділу доставки")
        self.client.force_login(user)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Злити вибрані в ціль")
        r = self.client.post(self.url, {"action": "merge", "target": self.big.pk, "merge": [self.twin.pk]})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(PackagingMaterial.objects.get(pk=self.twin.pk).is_active)
