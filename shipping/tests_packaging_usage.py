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

    def test_archived_box_merges_into_active_target_only(self):
        p = Product.objects.create(sku="AN-9", name="A")
        ProductPackaging.objects.create(product=p, packaging=self.twin, qty_per_box=4)
        PackagingMaterial.objects.filter(pk=self.twin.pk).update(is_active=False)
        self.twin.refresh_from_db()
        out = pu.merge(self.big, [self.twin])
        self.assertEqual((out["moved_recs"], out["archived"]), (1, [self.twin.pk]))
        self.assertEqual(ProductPackaging.objects.get(product=p).packaging_id, self.big.pk)
        PackagingMaterial.objects.filter(pk=self.big.pk).update(is_active=False)
        self.big.refresh_from_db()
        with self.assertRaises(pu.MergeError):                                # в архівну — ні
            pu.merge(self.big, [self.small])
        self.assertTrue(PackagingMaterial.objects.get(pk=self.small.pk).is_active)

    def test_active_box_is_suggested_as_target(self):
        self.shipment(dims=(30.5, 20, 10.5))
        self.shipment(dims=(30.5, 20, 10.5))
        PackagingMaterial.objects.filter(pk=self.twin.pk).update(is_active=False)
        g = pu.similar(1, include_inactive=True)[0]
        self.assertEqual(g["target"].pk, self.big.pk)                        # вживаніша, але архівна — не ціль

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

    def test_page_merges_archived_box(self):
        PackagingMaterial.objects.filter(pk=self.twin.pk).update(is_active=False)
        self.client.force_login(self.boss)
        r = self.client.get(self.url + "?inactive=1")
        self.assertContains(r, f'name="merge" value="{self.twin.pk}"')
        self.assertNotContains(r, f'name="target" value="{self.twin.pk}"')
        self.client.post(self.url, {"action": "merge", "target": self.big.pk, "merge": [self.twin.pk]})
        self.assertIn(f"злито в #{self.big.pk}", PackagingMaterial.objects.get(pk=self.twin.pk).notes)

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


class AfterMergeTests(Base):
    """Після злиття: статистика й запас — за ціллю, історія злиттів, відкат."""

    def stock(self, m):
        return PackagingMaterial.objects.get(pk=m.pk).stock_qty

    def booked(self, dims):
        """Відправлення, оформлене так, як у житті: спершу чернетка, потім етикетка (сигнал → списання)."""
        s = self.shipment(dims=dims, status=Shipment.Status.DRAFT)
        with self.captureOnCommitCallbacks(execute=True):
            s.status = Shipment.Status.LABEL_READY
            s.save()
        return s

    def test_stats_go_to_target_and_new_parcels_debit_it(self):
        far = PackagingMaterial.objects.create(name="Far", length_cm=32, width_cm=20, height_cm=10, track_stock=True)
        PackagingMaterial.objects.filter(pk__in=[self.big.pk, far.pk]).update(stock_qty=10)
        old = self.booked((32, 20, 10))                                   # до злиття — списує Far
        self.assertEqual((self.stock(far), self.stock(self.big)), (9, 10))
        self.booked((30, 20, 10))
        pu.merge(self.big, [far])                                         # ±2 см — у ціль
        self.assertEqual((self.stock(far), self.stock(self.big)), (0, 18))  # 10 − 1 + 9
        st = pu.usage(365)["materials"]
        self.assertEqual((st[self.big.pk]["boxes"], st[self.big.pk]["merged"], st[far.pk]["boxes"]), (2, 1, 0))
        self.booked((32, 20, 10))                                         # після злиття — списує ціль
        self.assertEqual((self.stock(far), self.stock(self.big)), (0, 17))
        with self.captureOnCommitCallbacks(execute=True):                # давнє: трекінг — нічого нового
            old.status = Shipment.Status.IN_TRANSIT
            old.save()
        self.assertEqual(self.stock(self.big), 17)
        with self.captureOnCommitCallbacks(execute=True):                # давнє скасували — повертається в ціль
            old.status = Shipment.Status.CANCELLED
            old.save()
        self.assertEqual((self.stock(far), self.stock(self.big)), (0, 18))

    def test_history_snapshot_and_after(self):
        self.shipment()
        self.shipment(dims=(30.5, 20, 10.5))
        self.shipment(dims=(30.5, 20, 10.5))
        out = pu.merge(self.big, [self.twin], via="admin")
        self.shipment()
        h = pu.history()[0]
        self.assertEqual(h["merge"].pk, out["merge_id"])
        self.assertEqual([(b["id"], b["boxes"]) for b in h["before"]], [(self.big.pk, 1), (self.twin.pk, 2)])
        self.assertEqual((h["total"]["boxes"], h["now"], h["after"], h["can_undo"]), (3, 4, 1, True))
        self.assertEqual(pu.history(target=self.small), [])

    def test_undo_restores_everything(self):
        a = Product.objects.create(sku="AN-1", name="A")
        b = Product.objects.create(sku="AN-2", name="B")
        ProductPackaging.objects.create(product=a, packaging=self.twin, qty_per_box=20, is_default=True)
        ProductPackaging.objects.create(product=b, packaging=self.big, qty_per_box=5, is_default=False)
        ProductPackaging.objects.create(product=b, packaging=self.twin, qty_per_box=8, is_default=True, notes="x")
        op = OrderPackaging.objects.create(order=SalesOrder.objects.create(source="webshop", order_number="U1"),
                                           packaging=self.twin)
        PackagingMaterial.objects.filter(pk=self.big.pk).update(stock_qty=4)
        PackagingMaterial.objects.filter(pk=self.twin.pk).update(stock_qty=3, notes="старе")
        self.twin.refresh_from_db()
        out = pu.merge(self.big, [self.twin])
        m = pu.history()[0]["merge"]
        res = pu.undo(m)
        self.assertEqual((res["boxes"], res["recs"], res["dropped"], res["orders"], res["stock"]), (1, 1, 1, 1, 3))
        twin = PackagingMaterial.objects.get(pk=self.twin.pk)
        self.assertEqual((twin.is_active, twin.merged_into_id, twin.merged_at, twin.notes, twin.stock_qty),
                         (True, None, None, "старе", 3))
        self.assertEqual(self.stock(self.big), 4)
        self.assertEqual(ProductPackaging.objects.get(product=a).packaging_id, self.twin.pk)
        kept = ProductPackaging.objects.get(product=b, packaging=self.big)
        self.assertEqual((kept.qty_per_box, kept.is_default), (5, False))
        back = ProductPackaging.objects.get(product=b, packaging=self.twin)
        self.assertEqual((back.qty_per_box, back.is_default, back.notes), (8, True, "x"))
        op.refresh_from_db()
        self.assertEqual(op.packaging_id, self.twin.pk)
        self.assertIsNotNone(pu.history()[0]["undone"])
        with self.assertRaises(pu.MergeError):
            pu.undo(m)
        self.assertTrue(out["merge_id"])

    def test_undo_waits_for_later_merge_and_restore_unlinks(self):
        first = pu.merge(self.big, [self.twin])["merge_id"]
        other = PackagingMaterial.objects.create(name="Other", length_cm=29, width_cm=20, height_cm=10)
        pu.merge(self.big, [other])
        from shipping.models import PackagingMerge
        with self.assertRaises(pu.MergeError):
            pu.undo(PackagingMerge.objects.get(pk=first))
        self.assertFalse(pu.history()[1]["can_undo"])
        twin = PackagingMaterial.objects.get(pk=self.twin.pk)               # повернули з архіву вручну
        twin.is_active = True
        twin.save(update_fields=["is_active"])
        twin.refresh_from_db()
        self.assertEqual((twin.merged_into_id, twin.merged_at), (None, None))

    def test_page_shows_history_and_undoes(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        boss = User.objects.create_superuser("boss2", "b2@x.y", "p")
        merge_id = pu.merge(self.big, [self.twin], user=boss, via="admin")["merge_id"]
        self.client.force_login(boss)
        url = reverse("admin:shipping_packagingmaterial_usage")
        r = self.client.get(url)
        self.assertContains(r, f'id="merge-{merge_id}"')
        self.assertContains(r, "Відкотити це злиття")
        r = self.client.get(reverse("admin:shipping_packagingmaterial_change", args=[self.big.pk]))
        self.assertContains(r, f"#merge-{merge_id}")
        r = self.client.post(url, {"action": "undo", "merge": merge_id})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(PackagingMaterial.objects.get(pk=self.twin.pk).is_active)
