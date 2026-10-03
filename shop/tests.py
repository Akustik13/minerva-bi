"""Тести магазинів:  python manage.py test shop api --settings=tabele.settings_test"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from api.models import APIKey
from inventory.models import InventorySettings, Location, Product
from inventory.services.stock import create_movement

from . import services
from .models import Shop, ShopListing, ShopPriceTier, ShopSettings


class PricingTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        self.p = Product.objects.create(sku="AN-1", name="Antenne", sale_price=Decimal("20.00"),
                                        purchase_price=Decimal("8.00"))
        self.l = ShopListing.objects.create(shop=self.shop, product=self.p)

    def test_round_modes(self):
        self.assertEqual(services.round_price(Decimal("12.344"), "0.01"), Decimal("12.3400"))
        self.assertEqual(services.round_price(Decimal("12.33"), "0.05"), Decimal("12.3500"))
        self.assertEqual(services.round_price(Decimal("12.05"), "x.99"), Decimal("11.9900"))
        self.assertEqual(services.round_price(Decimal("12.70"), "x.99"), Decimal("12.9900"))
        self.assertEqual(services.round_price(Decimal("0.30"), "x.90"), Decimal("0.9000"))

    def test_generate_tiers_and_unit_price(self):
        services.generate_tiers([self.l], [{"min_qty": 10, "discount": 10}, {"min_qty": 100, "discount": 25}], "0.01")
        self.assertEqual([(r["min_qty"], r["unit_price"]) for r in services.price_breaks(self.l)],
                         [(1, Decimal("20.00")), (10, Decimal("18.0000")), (100, Decimal("15.0000"))])
        self.assertEqual(services.unit_price_for(self.l, 9), Decimal("20.00"))
        self.assertEqual(services.unit_price_for(self.l, 250), Decimal("15.0000"))

    def test_adjust_scales_tiers(self):
        services.generate_tiers([self.l], [{"min_qty": 10, "discount": 10}], "0.01")
        services.adjust_prices([self.l], 10, "0.01")
        self.l.refresh_from_db()
        self.assertEqual(self.l.price, Decimal("22.0000"))
        self.assertEqual(self.l.tiers.get().unit_price, Decimal("19.8000"))

    def test_price_from_purchase_and_reset(self):
        self.assertEqual(services.price_from_purchase([self.l], 150, "0.01"), (1, []))
        self.l.refresh_from_db()
        self.assertEqual(self.l.price, Decimal("20.0000"))
        services.reset_to_sale_price([self.l])
        self.l.refresh_from_db()
        self.assertIsNone(self.l.price)

    def test_copy_to_other_shop_with_factor(self):
        services.set_price([self.l], 10, "0.01")
        services.generate_tiers([self.l], [{"min_qty": 10, "discount": 10}], "0.01")
        market = Shop.objects.create(name="Market", slug="market")
        self.assertEqual(services.copy_listings(ShopListing.objects.filter(pk=self.l.pk), market, Decimal("1.2")), (1, 0))
        dst = ShopListing.objects.get(shop=market, product=self.p)
        self.assertEqual((dst.price, dst.tiers.get().unit_price), (Decimal("12.0000"), Decimal("10.8000")))

    def test_add_products(self):
        market = Shop.objects.create(name="Market", slug="market")
        self.assertEqual(services.add_products(market, [self.p], markup=Decimal("50")), (1, 0))
        self.assertEqual(ShopListing.objects.get(shop=market).price, Decimal("12.0000"))
        self.assertEqual(services.add_products(market, [self.p]), (0, 1))

    def test_shop_for_key(self):
        market = Shop.objects.create(name="Market", slug="market")
        self.assertEqual(services.shop_for_key(APIKey(name="x", shop=market)), market)
        self.assertEqual(services.shop_for_key(APIKey(name="x", default_source="market")), market)
        self.assertEqual(services.shop_for_key(APIKey(name="x", default_source="other")), self.shop)

    def test_single_default_shop(self):
        Shop.objects.create(name="New", slug="new", is_default=True)
        self.assertEqual(list(Shop.objects.filter(is_default=True).values_list("slug", flat=True)), ["new"])

    def test_settings_default_schedule(self):
        self.assertTrue(ShopSettings.get().price_breaks)


class ShopApiTierTests(TestCase):
    def setUp(self):
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        self.p = Product.objects.create(sku="AN-1", name="Antenne", sale_price=Decimal("20.00"))
        create_movement(product=self.p, tx_type="Incoming", qty=500)
        listing = ShopListing.objects.create(shop=shop, product=self.p)
        ShopPriceTier.objects.create(listing=listing, min_qty=10, unit_price=Decimal("18.00"))
        ShopPriceTier.objects.create(listing=listing, min_qty=100, unit_price=Decimal("15.00"))
        key = APIKey.objects.create(name="site", scopes=["products:read", "orders:write", "orders:read"],
                                    default_source="webshop")
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")

    def test_catalog_has_price_breaks(self):
        d = self.c.get("/api/v1/shop/products/AN-1/").json()
        self.assertEqual(d["price_breaks"], [{"min_qty": 1, "unit_price": 20.0}, {"min_qty": 10, "unit_price": 18.0},
                                             {"min_qty": 100, "unit_price": 15.0}])

    def test_order_uses_quantity_tier(self):
        r = self.c.post("/api/v1/orders/", {"order_number": "WS-T1", "client": "X", "shop": True,
                                            "lines": [{"sku": "AN-1", "qty": 120}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["lines"][0]["unit_price"], 15.0)
        self.assertEqual(r.json()["total_price"], 1800.0)


class ShopAdminTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("a", "a@x.y", "p"))
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        self.p = Product.objects.create(sku="AN-1", name="Antenne", sale_price=Decimal("20.00"))

    def _listing(self):
        return ShopListing.objects.get(shop=self.shop, product=self.p)

    def test_pages_load(self):
        services.add_products(self.shop, [self.p])
        for url in ["/admin/shop/shop/", "/admin/shop/shoplisting/", "/admin/shop/shopproduct/",
                    "/admin/shop/shoplisting/?price_state=tiers&stock=in",
                    f"/admin/shop/shoplisting/{self._listing().pk}/change/",
                    f"/admin/inventory/product/{self.p.pk}/change/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        r = self.client.get("/admin/shop/shopsettings/")
        self.assertEqual(self.client.get(r["Location"]).status_code, 200)

    def test_add_to_shop_action(self):
        r = self.client.post("/admin/shop/shopproduct/", {"action": "action_add_to_shop", "_selected_action": [self.p.pk]})
        self.assertContains(r, "Додати товари в магазин")
        self.client.post("/admin/shop/shopproduct/", {"action": "action_add_to_shop", "_selected_action": [self.p.pk],
                                                      "apply": "1", "shop": self.shop.pk, "visible": "on",
                                                      "price_source": "sale", "with_tiers": "on"})
        self.assertTrue(self._listing().tiers.exists())

    def test_listing_actions(self):
        services.add_products(self.shop, [self.p])
        lid = self._listing().pk
        url = "/admin/shop/shoplisting/"
        self.client.post(url, {"action": "action_set_price", "_selected_action": [lid], "apply": "1", "price": "12.5"})
        self.assertEqual(self._listing().price, Decimal("12.5000"))
        self.client.post(url, {"action": "action_adjust", "_selected_action": [lid], "apply": "1", "percent": "-10"})
        self.assertEqual(self._listing().price, Decimal("11.2500"))
        self.client.post(url, {"action": "action_hide", "_selected_action": [lid]})
        self.assertFalse(self._listing().is_visible)
        market = Shop.objects.create(name="Market", slug="market")
        self.client.post(url, {"action": "action_copy", "_selected_action": [lid], "apply": "1",
                               "target": market.pk, "factor": "2", "with_tiers": "on"})
        self.assertEqual(ShopListing.objects.get(shop=market).price, Decimal("22.5000"))
