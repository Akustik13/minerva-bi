"""REST API упаковки (/packaging/, api/packaging.py): коробки, запас, використання, схожі розміри, об'єднання.
python manage.py test api.tests_packaging --settings=tabele.settings_test"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client

from api import key_insight
from api.models import APIKey
from sales.models import SalesOrder
from shipping.models import (Carrier, OrderPackaging, PackagingMaterial, PackagingMovement, ProductPackaging,
                             Shipment, ShipmentPackage)

from .tests import APITestBase

URL = "/api/v1/packaging/"
RW = ["packaging:read", "packaging:write"]


class PackagingBase(APITestBase):
    def setUp(self):
        super().setUp()
        cache.clear()                     # ліміт запитів ключа — pk ключа повторюється між тестами
        self.key.scopes = list(RW)
        self.key.save()
        self.n = 0

    def box(self, l=30, w=20, h=15, **kw):
        stock = kw.pop("stock", None)
        m = PackagingMaterial.objects.create(length_cm=l, width_cm=w, height_cm=h, **kw)
        if stock is not None:
            PackagingMaterial.objects.filter(pk=m.pk).update(stock_qty=stock)
            m.refresh_from_db()
        return m

    def scopes(self, *scopes):
        APIKey.objects.filter(pk=self.key.pk).update(scopes=list(scopes))

    def ids(self, r):
        self.assertEqual(r.status_code, 200, r.content)
        return [x["id"] for x in r.json()["results"]]

    def stock(self, m):
        return PackagingMaterial.objects.get(pk=m.pk).stock_qty

    def shipment(self, dims=None, rows=(), status=Shipment.Status.LABEL_READY, **kw):
        self.n += 1
        if not hasattr(self, "carrier"):
            self.carrier = Carrier.objects.create(name="UPS", carrier_type="ups", sender_country="DE")
        order = SalesOrder.objects.create(source="webshop", order_number=f"8100{self.n}")
        l, w, h = dims or (None, None, None)
        s = Shipment.objects.create(order=order, carrier=self.carrier, status=status, weight_kg=Decimal("1"),
                                    length_cm=l, width_cm=w, height_cm=h, **kw)
        for r in rows:
            ShipmentPackage.objects.create(shipment=s, **r)
        return s


class AccessTests(PackagingBase):
    def test_scopes_read_write_and_none(self):
        box = self.box(track_stock=True)
        self.scopes("stock:read")
        r = self.client.get(URL)
        self.assertEqual(r.status_code, 403)
        self.assertIn("packaging:read", r.json()["detail"])

        self.scopes("packaging:read")
        self.assertEqual(self.client.get(URL).status_code, 200)
        self.assertEqual(self.client.get(f"{URL}{box.pk}/").status_code, 200)
        self.assertEqual(self.client.get(f"{URL}usage/").status_code, 200)
        self.assertEqual(self.client.get(f"{URL}similar/").status_code, 200)
        for method, path, body in (("post", URL, {"length_cm": 10, "width_cm": 10, "height_cm": 10}),
                                   ("patch", f"{URL}{box.pk}/", {"name": "X"}),
                                   ("post", f"{URL}{box.pk}/receive/", {"qty": 1}),
                                   ("post", f"{URL}{box.pk}/count/", {"actual": 1}),
                                   ("post", f"{URL}merge/", {"target": box.pk, "merge": [box.pk]})):
            r = getattr(self.client, method)(path, body, format="json")
            self.assertEqual(r.status_code, 403, path)
            self.assertIn("packaging:write", r.json()["detail"])
        self.assertEqual(self.stock(box), 0)

        self.scopes(*RW)
        self.assertEqual(self.client.delete(f"{URL}{box.pk}/").status_code, 405)      # лише архів
        self.assertTrue(PackagingMaterial.objects.filter(pk=box.pk).exists())
        self.assertEqual(self.client.put(f"{URL}{box.pk}/", {}, format="json").status_code, 405)

    def test_shop_key_refused(self):
        from shop.models import Shop
        shop = Shop.objects.create(name="Sevskiy", slug="webshop", url="https://web.example.com")
        box = self.box()
        for r in (self.client.get(URL), self.client.get(f"{URL}usage/"),
                  self.client.post(f"{URL}{box.pk}/receive/", {"qty": 1}, format="json")):
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.json()["code"], "shop_key_forbidden")
        rows = {(x["method"], x["path"]): x for x in key_insight.capabilities(self.key)}
        self.assertEqual(rows[("GET", URL)]["scope"], "packaging:read")
        APIKey.objects.filter(pk=self.key.pk).update(shop=shop, default_source="")   # прив'язаний полем «Магазин»
        self.assertEqual(self.client.get(URL).status_code, 403)
        self.key.refresh_from_db()
        rows = {(x["method"], x["path"]): x for x in key_insight.capabilities(self.key)}
        self.assertEqual(rows[("GET", URL)]["access"], "denied")
        self.assertEqual(rows[("DELETE", URL + "{pk}/")]["access"], "unsupported")

    def test_admin_session_reads_only(self):
        self.box()
        admin = Client()
        admin.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        self.assertEqual(admin.get(URL).status_code, 200)
        r = admin.post(URL, {"length_cm": 10, "width_cm": 10, "height_cm": 10}, content_type="application/json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(PackagingMaterial.objects.count(), 1)


class ListTests(PackagingBase):
    def setUp(self):
        super().setUp()
        self.a = self.box(30, 20, 15, code="BX-M-01", fold_heights="22, 16", track_stock=True, stock=10, min_stock=2)
        self.b = self.box(40, 30, 20, name="Old big", is_active=False)
        self.c = self.box(25, 18, 2, box_type="envelope", name="Envelope A4", track_stock=True, stock=1)

    def test_filters(self):
        self.assertEqual(self.ids(self.client.get(URL)), [self.a.pk, self.c.pk])          # за замовчуванням — активні
        self.assertEqual(self.ids(self.client.get(URL, {"active": "false"})), [self.b.pk])
        self.assertEqual(sorted(self.ids(self.client.get(URL, {"active": "all"}))), [self.a.pk, self.b.pk, self.c.pk])
        self.assertEqual(self.ids(self.client.get(URL, {"q": "bx-m"})), [self.a.pk])
        self.assertEqual(self.ids(self.client.get(URL, {"q": "big", "active": "all"})), [self.b.pk])
        self.assertEqual(self.ids(self.client.get(URL, {"box_type": "envelope"})), [self.c.pk])
        self.assertEqual(self.ids(self.client.get(URL, {"low": "true"})), [self.c.pk])   # 1 ≤ 3 (за замовчуванням)
        self.assertEqual(self.client.get(URL, {"active": "maybe"}).json()["code"], "bad_param")
        self.assertEqual(self.client.get(URL, {"box_type": "crate"}).status_code, 400)

    def test_fields(self):
        r = self.client.get(f"{URL}{self.a.pk}/")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertEqual((d["code"], d["box_type"], d["fold_heights"]), ("BX-M-01", "box", [22, 16]))
        self.assertEqual((d["stock_qty"], d["min_stock"], d["own_min_stock"], d["is_low"]), (10, 2, 2, False))
        self.assertEqual((d["length_cm"], d["volume_cm3"], d["size_class_effective"]), (30, 9000.0, "M"))
        self.assertNotIn("usage", d)
        c = self.client.get(f"{URL}{self.c.pk}/").json()
        self.assertEqual((c["min_stock"], c["own_min_stock"], c["is_low"]), (3, None, True))
        self.assertEqual(self.client.get(f"{URL}999999/").status_code, 404)

    def test_with_usage_computed_once(self):
        ProductPackaging.objects.create(product=self.p1, packaging=self.a)
        from shipping.services import packaging_usage
        with mock.patch.object(packaging_usage, "usage", wraps=packaging_usage.usage) as spy:
            r = self.client.get(URL, {"with_usage": "1", "active": "all"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(spy.call_count, 1)
        rows = {x["id"]: x for x in r.json()["results"]}
        self.assertEqual(rows[self.a.pk]["usage"]["recs"], 1)
        self.assertEqual(rows[self.b.pk]["usage"], {"boxes": 0, "shipments": 0, "recs": 0, "orders": 0,
                                                    "last": None, "days": 365})
        one = self.client.get(f"{URL}{self.a.pk}/", {"with_usage": "1"}).json()
        self.assertEqual(one["usage"]["recs"], 1)


class WriteTests(PackagingBase):
    def test_create(self):
        r = self.client.post(URL, {"code": " bx-s 01 ", "length_cm": "30", "width_cm": 20, "height_cm": 30,
                                   "fold_heights": [22, "16", 30], "stock_qty": 50, "track_stock": True,
                                   "own_min_stock": 4, "tare_weight_kg": "0.150"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        d = r.json()
        m = PackagingMaterial.objects.get(pk=d["id"])
        self.assertEqual((m.code, m.fold_heights, m.stock_qty, m.min_stock), ("BX-S01", "22, 16", 0, 4))
        self.assertTrue(m.name)                                        # з розмірів, як у картці
        self.assertIsNotNone(m.stock_since)
        self.assertEqual((d["stock_qty"], d["fold_heights"], d["min_stock"], d["is_low"]), (0, [22, 16], 4, True))

    def test_create_refusals(self):
        dims = {"length_cm": 30, "width_cm": 20, "height_cm": 15}
        r = self.client.post(URL, {**dims, "fold_heights": "20"}, format="json")     # вище за «Висоту»
        self.assertEqual(r.status_code, 400)
        self.assertIn("fold_heights", r.json())
        r = self.client.post(URL, {**dims, "fold_heights": "abc"}, format="json")
        self.assertIn("fold_heights", r.json())
        self.box(code="BX-M-01")
        r = self.client.post(URL, {**dims, "code": "bx-m-01"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("BX-M-01", r.json()["code"][0])
        self.assertIn("code", self.client.post(URL, {**dims, "code": "Ящик №1"}, format="json").json())
        self.assertIn("height_cm", self.client.post(URL, {**dims, "height_cm": 0}, format="json").json())
        self.assertIn("length_cm", self.client.post(URL, {"width_cm": 1, "height_cm": 1}, format="json").json())
        self.assertEqual(PackagingMaterial.objects.count(), 1)

    def test_patch(self):
        box = self.box(code="BX-M-01", track_stock=True, stock=7, fold_heights="10")
        r = self.client.patch(f"{URL}{box.pk}/", {"stock_qty": 99, "name": "Main box", "code": "BX-M-01",
                                                  "fold_heights": "12; 8", "own_min_stock": None}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        box.refresh_from_db()
        self.assertEqual((box.stock_qty, box.name, box.fold_heights, box.min_stock), (7, "Main box", "12, 8", None))
        self.assertEqual(r.json()["fold_heights"], [12, 8])
        r = self.client.patch(f"{URL}{box.pk}/", {"height_cm": 11}, format="json")    # складається до 12 — вище
        self.assertEqual(r.status_code, 400)
        self.assertIn("fold_heights", r.json())
        with mock.patch("shipping.services.packaging_stock.check_low") as check_low, \
                self.captureOnCommitCallbacks(execute=True):
            r = self.client.patch(f"{URL}{box.pk}/", {"is_active": False}, format="json")   # в архів
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(PackagingMaterial.objects.get(pk=box.pk).is_active)
        check_low.assert_called_once_with(box.pk)
        self.assertEqual(self.ids(self.client.get(URL, {"active": "false"})), [box.pk])


class StockTests(PackagingBase):
    def test_receive_and_count(self):
        plain = self.box()
        r = self.client.post(f"{URL}{plain.pk}/receive/", {"qty": 5}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "no_stock_tracking")
        self.assertEqual(self.client.post(f"{URL}{plain.pk}/count/", {"actual": 3}, format="json").json()["code"],
                         "no_stock_tracking")
        self.assertFalse(PackagingMovement.objects.exists())

        owner = User.objects.create_user("own", "own@example.com", "p")
        APIKey.objects.filter(pk=self.key.pk).update(owner=owner)
        box = self.box(track_stock=True)
        self.assertEqual(self.client.post(f"{URL}{box.pk}/receive/", {"qty": 0}, format="json").status_code, 400)
        r = self.client.post(f"{URL}{box.pk}/receive/", {"qty": 5, "note": "RE-42"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["movement"]["kind"], r.json()["movement"]["qty"]), ("purchase", 5))
        self.assertEqual(r.json()["packaging"]["stock_qty"], 5)
        mv = PackagingMovement.objects.get(material=box)
        self.assertEqual((mv.balance, mv.user), (5, owner))
        self.assertEqual(mv.note, "RE-42 · API: webshop")

        r = self.client.post(f"{URL}{box.pk}/count/", {"actual": 3}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["changed"], r.json()["movement"]["kind"], r.json()["movement"]["qty"]),
                         (True, "adjust", -2))
        self.assertEqual(self.stock(box), 3)
        r = self.client.post(f"{URL}{box.pk}/count/", {"actual": 3}, format="json")
        self.assertEqual((r.json()["changed"], r.json()["movement"]), (False, None))
        self.assertEqual(self.client.post(f"{URL}{box.pk}/count/", {"actual": -1}, format="json").status_code, 400)
        self.assertEqual(PackagingMovement.objects.filter(material=box).count(), 2)


class UsageTests(PackagingBase):
    def test_usage(self):
        box = self.box(20, 15, 5, code="BX-S-01")
        spare = self.box(50, 40, 30)
        ProductPackaging.objects.create(product=self.p1, packaging=spare)
        self.shipment(rows=[{"weight_kg": 1, "length_cm": 5, "width_cm": 20, "height_cm": 15, "quantity": 2}])
        self.shipment(dims=(33, 22, 11))                                        # такої коробки немає
        self.shipment(dims=(20, 15, 5), status=Shipment.Status.DRAFT)           # не оформлено
        self.shipment(dims=(20, 15, 5), direction="in")                          # посилка до нас
        r = self.client.get(f"{URL}usage/")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertEqual((d["days"], d["shipments"]), (365, 2))                 # чернетка й вхідна — ні
        self.assertEqual([m["id"] for m in d["materials"]], [box.pk, spare.pk])   # за кількістю, далі рекомендації
        first = d["materials"][0]
        self.assertEqual((first["code"], first["boxes"], first["shipments"], first["dims"]), ("BX-S-01", 2, 1,
                                                                                              "20 × 15 × 5"))
        self.assertIsNotNone(first["last"])
        self.assertEqual((d["materials"][1]["boxes"], d["materials"][1]["recs"]), (0, 1))
        self.assertEqual([(sorted(int(x) for x in u["dims"].split(" × ")), u["boxes"]) for u in d["unmatched"]],
                         [([11, 22, 33], 1)])
        self.assertEqual(self.client.get(f"{URL}usage/", {"days": "x"}).status_code, 400)
        self.assertEqual(self.client.get(f"{URL}usage/", {"days": "30"}).json()["days"], 30)


class SimilarTests(PackagingBase):
    def test_tolerance_groups(self):
        a = self.box(30, 20, 15)
        b = self.box(30.5, 20, 15, code="BX-M-02")
        c = self.box(32, 20, 15)
        self.box(30, 20, 15, box_type="envelope")                                # інший тип — окремо
        old = self.box(30, 20, 15.5, is_active=False)

        def groups(**params):
            r = self.client.get(f"{URL}similar/", params)
            self.assertEqual(r.status_code, 200, r.content)
            return r.json()["groups"]

        (g,) = groups()
        self.assertEqual(g["target_id"], b.pk)                                   # з кодом
        self.assertEqual([m["id"] for m in g["members"]], [b.pk, a.pk])
        self.assertEqual(g["spread"], [0, 0, 0.5])                               # виміри від меншого
        (g,) = groups(tolerance=3)
        self.assertEqual(sorted(m["id"] for m in g["members"]), [a.pk, b.pk, c.pk])
        (g,) = groups(tolerance=1, inactive=1)
        self.assertIn(old.pk, [m["id"] for m in g["members"]])
        self.assertEqual(groups(tolerance=0), [])
        self.assertEqual(self.client.get(f"{URL}similar/", {"tolerance": "abc"}).status_code, 400)
        self.assertEqual(self.client.get(f"{URL}similar/", {"tolerance": "-1"}).status_code, 400)


class MergeTests(PackagingBase):
    def test_merge_moves_links_and_archives(self):
        owner = User.objects.create_user("own", "own@example.com", "p")
        APIKey.objects.filter(pk=self.key.pk).update(owner=owner)
        target = self.box(30, 20, 15, track_stock=True, stock=2)
        other = self.box(30.5, 20, 15, track_stock=True, stock=3)
        ProductPackaging.objects.create(product=self.p1, packaging=other, qty_per_box=4)
        ProductPackaging.objects.create(product=self.p2, packaging=other, qty_per_box=9)
        ProductPackaging.objects.create(product=self.p2, packaging=target, qty_per_box=5)
        order = SalesOrder.objects.create(source="webshop", order_number="8200")
        OrderPackaging.objects.create(order=order, packaging=other, qty_boxes=2)

        r = self.client.post(f"{URL}merge/", {"target": target.pk, "merge": [other.pk]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertEqual((d["moved_recs"], d["dropped_recs"], d["moved_orders"], d["stock"], d["archived"]),
                         (1, 1, 1, 3, [other.pk]))
        self.assertEqual(d["target"]["stock_qty"], 5)
        other.refresh_from_db()
        self.assertFalse(other.is_active)
        self.assertEqual(other.stock_qty, 0)
        self.assertFalse(ProductPackaging.objects.filter(packaging=other).exists())
        self.assertEqual(ProductPackaging.objects.get(packaging=target, product=self.p2).qty_per_box, 9)
        self.assertEqual(OrderPackaging.objects.get(order=order).packaging_id, target.pk)
        self.assertTrue(PackagingMovement.objects.filter(material=target, qty=3, user=owner).exists())

    def test_merge_refusals(self):
        box = self.box()
        envelope = self.box(box_type="envelope")
        r = self.client.post(f"{URL}merge/", {"target": box.pk, "merge": [envelope.pk]}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "merge_refused"))
        r = self.client.post(f"{URL}merge/", {"target": box.pk, "merge": [box.pk]}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "merge_refused"))
        r = self.client.post(f"{URL}merge/", {"target": box.pk, "merge": [999999]}, format="json")
        self.assertEqual((r.status_code, r.json()["code"], r.json()["missing"]), (404, "not_found", [999999]))
        self.assertEqual(self.client.post(f"{URL}merge/", {"target": box.pk, "merge": []}, format="json").status_code,
                         400)
        self.assertTrue(PackagingMaterial.objects.get(pk=envelope.pk).is_active)
