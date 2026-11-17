"""Мінімальний лот і дозволені кількості в магазинах (shop/lots.py): правило, ціни для покупця, API, адмінка.

python manage.py test shop.tests_lots --settings=tabele.settings_test
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from api.models import APIKey
from inventory.models import InventorySettings, Location, Product
from inventory.services.stock import create_movement
from sales.models import SalesOrder
from shop import lots, services
from shop.models import Shop, ShopListing, ShopPriceTier


def D(v):
    return Decimal(str(v))


ROWS = [{"min_qty": 1, "unit_price": D("1.00")}, {"min_qty": 100, "unit_price": D("0.50")},
        {"min_qty": 1000, "unit_price": D("0.30")}, {"min_qty": 2500, "unit_price": D("0.25")},
        {"min_qty": 5000, "unit_price": D("0.20")}]


class RuleTests(TestCase):
    def test_step_from_minimum(self):
        r = lots.make(1500, 1000, [], lots.SOURCE_LISTING)
        self.assertEqual(r.examples(3), [1500, 2500, 3500])
        self.assertIsNone(r.problem(2500))
        p = r.problem(2000)
        self.assertEqual((p["code"], p["nearest"], p["min_qty"], p["qty_step"]), ("qty_not_in_step", [1500, 2500],
                                                                                   1500, 1000))
        self.assertEqual(r.problem(1000)["code"], "below_min_qty")
        self.assertEqual(r.problem(1000)["nearest"], [1500])

    def test_choices_win_over_minimum(self):
        r = lots.make(1, 1, [10000, 1000, 5000, 5000], lots.SOURCE_PRODUCT)
        self.assertEqual((r.choices, r.first), ((1000, 5000, 10000), 1000))
        self.assertIsNone(r.problem(5000))
        p = r.problem(7000)
        self.assertEqual((p["code"], p["nearest"], p["qty_choices"], p["qty_step"]),
                         ("qty_not_in_choices", [5000, 10000], [1000, 5000, 10000], None))
        self.assertEqual(r.problem(20000)["nearest"], [10000])
        self.assertEqual(r.text(), "лише 1 000 / 5 000 / 10 000 шт.")

    def test_no_rule_allows_any(self):
        r = lots.make(1, 1, [], lots.SOURCE_NONE)
        self.assertTrue(r.any)
        self.assertIsNone(r.problem(Decimal("0.5")))
        self.assertEqual(r.text(), "")
        self.assertEqual(lots.cut(ROWS, r), ROWS)

    def test_parse_choices(self):
        self.assertEqual(lots.parse_choices("1000; 5 000, 10.000\n1000"), [1000, 5000, 10000])
        self.assertEqual(lots.parse_choices(""), [])
        with self.assertRaises(ValueError):
            lots.parse_choices("1000; багато")
        with self.assertRaises(ValueError):
            lots.parse_choices("0")


class CutTests(TestCase):
    def test_lot_hides_one_piece_and_lower_tiers(self):
        rows = lots.cut(ROWS, lots.make(1000, 1000, [], lots.SOURCE_LISTING))
        # 2500 між дозволеними кількостями → діє від 3000
        self.assertEqual([(r["min_qty"], r["unit_price"]) for r in rows],
                         [(1000, D("0.30")), (3000, D("0.25")), (5000, D("0.20"))])

    def test_minimum_without_step_takes_price_at_lot(self):
        rows = lots.cut(ROWS, lots.make(300, 1, [], lots.SOURCE_LISTING))
        self.assertEqual([(r["min_qty"], r["unit_price"]) for r in rows][:2], [(300, D("0.50")), (1000, D("0.30"))])

    def test_choices_one_row_each(self):
        rows = lots.cut(ROWS, lots.make(1, 1, [1000, 5000, 10000], lots.SOURCE_LISTING))
        self.assertEqual([(r["min_qty"], r["unit_price"]) for r in rows],
                         [(1000, D("0.30")), (5000, D("0.20")), (10000, D("0.20"))])
        self.assertEqual([r["min_qty"] for r in lots.hidden_rows(ROWS, lots.make(1, 1, [1000], "x"))], [1, 100])


class _Shop(TestCase):
    def setUp(self):
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        self.p = Product.objects.create(sku="RES-1", name="Widerstand", sale_price=D("1.00"),
                                        base_prices=[{"min_qty": 1, "unit_price": "1.00"},
                                                     {"min_qty": 1000, "unit_price": "0.24"}])
        create_movement(product=self.p, tx_type="Incoming", qty=100000)
        self.l = ShopListing.objects.create(shop=self.shop, product=self.p)
        for q, price in ((100, "0.50"), (1000, "0.24"), (5000, "0.20")):
            ShopPriceTier.objects.create(listing=self.l, min_qty=q, unit_price=D(price))
        key = APIKey.objects.create(name="site", shop=self.shop,
                                    scopes=["products:read", "orders:write", "orders:read"])
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")

    def reload(self):
        self.l = ShopListing.objects.select_related("product", "shop").get(pk=self.l.pk)
        return self.l

    def order(self, qty, number="WS-1"):
        return self.c.post("/api/v1/orders/", {"order_number": number, "client": "Firma", "email": "a@b.de",
                                               "shop": True, "lines": [{"sku": "RES-1", "qty": qty}]}, format="json")


class ListingRuleTests(_Shop):
    def test_product_default_and_listing_override(self):
        self.assertTrue(lots.rule_for(self.reload()).any)
        Product.objects.filter(pk=self.p.pk).update(shop_min_qty=1000, shop_qty_step=1000)
        r = lots.rule_for(self.reload())
        self.assertEqual((r.first, r.step, r.source), (1000, 1000, lots.SOURCE_PRODUCT))
        services.set_lot([self.l], min_qty=1)                         # «будь-яка кількість у цьому магазині»
        self.assertTrue(lots.rule_for(self.reload()).any)
        services.set_lot([self.l], choices=[1000, 5000])
        r = lots.rule_for(self.reload())
        self.assertEqual((r.choices, r.source), ((1000, 5000), lots.SOURCE_LISTING))
        services.set_lot([self.l])                                    # знову як у товарі
        self.assertEqual(lots.rule_for(self.reload()).source, lots.SOURCE_PRODUCT)

    def test_price_breaks_and_offer_start_at_lot(self):
        services.set_lot([self.l], min_qty=1000, step=1000)
        self.assertEqual([(r["min_qty"], r["unit_price"]) for r in services.price_breaks(self.reload())],
                         [(1000, D("0.24")), (5000, D("0.20"))])
        self.assertIsNone(services.unit_price_for(self.l, 500))
        self.l.discount_percent = D("10")
        self.l.save()
        info = services.offer_info(self.reload())
        self.assertEqual(info["regular_price"], D("0.24"))
        self.assertEqual(services.price_breaks(self.l)[0], {"min_qty": 1000, "unit_price": D("0.22")})

    def test_copy_to_other_shop_keeps_lot(self):
        services.set_lot([self.l], choices=[1000, 5000])
        other = Shop.objects.create(name="B2B", slug="b2b")
        services.copy_listings([self.reload()], other)
        self.assertEqual(ShopListing.objects.get(shop=other, product=self.p).qty_choices, [1000, 5000])


class ApiTests(_Shop):
    def test_catalog_publishes_lot_without_one_piece_price(self):
        services.set_lot([self.l], min_qty=1000, step=1000)
        d = self.c.get("/api/v1/shop/products/RES-1/").json()
        self.assertEqual((d["min_qty"], d["qty_step"], d["qty_choices"]), (1000, 1000, []))
        self.assertEqual(d["price"], 0.24)
        self.assertEqual(d["price_breaks"], [{"min_qty": 1000, "unit_price": 0.24}, {"min_qty": 5000, "unit_price": 0.2}])
        self.assertEqual(d["base_prices"]["price_breaks"], [{"min_qty": 1000, "unit_price": 0.24}])

    def test_catalog_without_lot_unchanged(self):
        d = self.c.get("/api/v1/shop/products/RES-1/").json()
        self.assertEqual((d["min_qty"], d["qty_step"], d["qty_choices"]), (1, 1, []))
        self.assertEqual(d["price_breaks"][0], {"min_qty": 1, "unit_price": 1.0})

    def test_choices_in_catalog(self):
        Product.objects.filter(pk=self.p.pk).update(shop_qty_choices=[1000, 5000, 10000])
        d = self.c.get("/api/v1/shop/products/RES-1/").json()
        self.assertEqual((d["min_qty"], d["qty_step"], d["qty_choices"]), (1000, 1, [1000, 5000, 10000]))
        self.assertEqual([r["min_qty"] for r in d["price_breaks"]], [1000, 5000, 10000])

    def test_products_endpoint_hides_one_piece_price_for_site_key(self):
        Product.objects.filter(pk=self.p.pk).update(shop_min_qty=1000)
        item = self.c.get("/api/v1/products/").json()["results"][0]
        self.assertIsNone(item["sale_price"])
        self.assertEqual([r["min_qty"] for r in item["base_prices"]["price_breaks"]], [1000])

    def test_order_quantities_checked(self):
        services.set_lot([self.l], min_qty=1000, step=1000)
        r = self.order(500, "WS-1")
        self.assertEqual(r.status_code, 400, r.content)
        line = r.json()["lines"][0]
        self.assertEqual((line["code"], line["min_qty"], line["qty_step"], line["nearest"]),
                         ("below_min_qty", 1000, 1000, [1000]))
        r = self.order(1500, "WS-2")
        self.assertEqual(r.json()["lines"][0]["code"], "qty_not_in_step")
        self.assertEqual(r.json()["lines"][0]["nearest"], [1000, 2000])
        self.assertFalse(SalesOrder.objects.filter(source="webshop").exists())
        r = self.order(2000, "WS-3")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["lines"][0]["unit_price"], 0.24)

    def test_order_choices(self):
        Product.objects.filter(pk=self.p.pk).update(shop_qty_choices=[1000, 5000, 10000])
        r = self.order(7000, "WS-4")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.json()["lines"][0]["code"], "qty_not_in_choices")
        self.assertEqual(r.json()["lines"][0]["nearest"], [5000, 10000])
        self.assertEqual(self.order(5000, "WS-5").status_code, 201)

    def test_quote_ignores_lot(self):
        Product.objects.filter(pk=self.p.pk).update(shop_min_qty=1000)
        r = self.c.post("/api/v1/orders/", {"order_number": "AN-1", "client": "X", "quote": True,
                                            "lines": [{"sku": "RES-1", "qty": 10}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)


class AdminTests(_Shop):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("a", "a@x.y", "p"))

    def test_pages_show_lot(self):
        Product.objects.filter(pk=self.p.pk).update(shop_min_qty=1000, shop_qty_step=1000)
        for url in ["/admin/shop/shoplisting/", "/admin/shop/shoplisting/?lot=yes", "/admin/shop/shoplisting/?lot=no",
                    "/admin/shop/shoplisting/?lot=own", "/admin/shop/shoplisting/?lot=product"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get("/admin/shop/shoplisting/?lot=yes"), "RES-1")
        self.assertNotContains(self.client.get("/admin/shop/shoplisting/?lot=no"), "RES-1")
        page = self.client.get(f"/admin/shop/shoplisting/{self.l.pk}/change/").content.decode()
        self.assertIn("Покупець може замовити", page)
        self.assertIn("Покупець не бачить", page)
        page = self.client.get(f"/admin/inventory/product/{self.p.pk}/change/").content.decode()
        self.assertIn('name="shop_qty_choices"', page)

    def test_listing_form_saves_choices_text(self):
        from shop.admin_lots import ListingLotForm
        form = ListingLotForm(instance=self.l, data={
            "shop": self.shop.pk, "product": self.p.pk, "is_visible": "on", "price": "", "price_source": "manual",
            "price_factor": "100", "discount_percent": "0", "lifecycle_status": "",
            "min_qty": "", "qty_step": "1000", "qty_choices": ""})
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save()
        self.assertEqual((obj.min_qty, obj.qty_step), (1000, 1000))        # крок без мінімуму → мінімум = крок
        form = ListingLotForm(instance=obj, data={**form.data, "qty_choices": "1000; 5 000"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().qty_choices, [1000, 5000])
        self.assertIn("1000; 5000", ListingLotForm(instance=obj).as_p())

    def test_mass_action(self):
        r = self.client.post("/admin/shop/shoplisting/", {
            "action": "action_lot", "_selected_action": [self.l.pk], "apply": "1",
            "mode": "set", "min_qty": "", "qty_step": "", "qty_choices": "1000; 5000"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.reload().qty_choices, [1000, 5000])
        self.client.post("/admin/shop/shoplisting/", {"action": "action_lot", "_selected_action": [self.l.pk],
                                                      "apply": "1", "mode": "product"})
        self.assertFalse(lots.has_own(self.reload()))

    def test_product_form_lot(self):
        from inventory.admin import ProductAdminForm
        form = ProductAdminForm(instance=self.p)
        self.p.shop_qty_choices = [1000, 5000]
        self.assertIn("1000; 5000", str(ProductAdminForm(instance=self.p)["shop_qty_choices"]))
        self.assertIn("shop_qty_choices", form.fields)
