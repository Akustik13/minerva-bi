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

    def test_help_page(self):
        services.add_products(self.shop, [self.p])
        r = self.client.get("/dashboard/shop/help/")
        self.assertContains(r, "Зв'язки магазину з базою даних")
        self.assertContains(r, "webshop")

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


class DigiKeyPriceTests(TestCase):
    def setUp(self):
        from bots.models import DigiKeyListing
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        self.p = Product.objects.create(sku="3228-AN-1-ND", name="Antenne", sale_price=Decimal("20.00"))
        self.dk = DigiKeyListing.objects.create(product=self.p, dk_prices=[
            {"qty": 1, "price": 10.0}, {"qty": 10, "price": 9.0}, {"qty": 100, "price": 8.0}])
        self.l = ShopListing.objects.create(shop=self.shop, product=self.p, price=Decimal("25"))
        ShopSettings.get()

    def _tiers(self):
        return [(t.min_qty, t.unit_price) for t in self.l.tiers.order_by("min_qty")]

    def test_use_digikey_prices_with_factor(self):
        n, missing = services.use_digikey_prices([self.l], Decimal("90"))
        self.assertEqual((n, missing), (1, []))
        self.l.refresh_from_db()
        self.assertEqual(self.l.price_source, ShopListing.PRICE_DIGIKEY)
        self.assertEqual(self.l.price, Decimal("9.0000"))
        self.assertEqual(self._tiers(), [(10, Decimal("8.1000")), (100, Decimal("7.2000"))])

    def test_digikey_update_propagates_and_manual_detaches(self):
        services.use_digikey_prices([self.l])
        self.dk.dk_prices = [{"qty": 1, "price": 11.0}, {"qty": 50, "price": 9.5}]
        self.dk.save(update_fields=["dk_prices"])
        self.l.refresh_from_db()
        self.assertEqual(self.l.price, Decimal("11.0000"))
        self.assertEqual(self._tiers(), [(50, Decimal("9.5000"))])
        services.set_price([self.l], Decimal("12"))
        self.l.refresh_from_db()
        self.assertEqual(self.l.price_source, ShopListing.PRICE_MANUAL)
        self.dk.dk_prices = [{"qty": 1, "price": 5.0}]
        self.dk.save(update_fields=["dk_prices"])
        self.l.refresh_from_db()
        self.assertEqual(self.l.price, Decimal("12.0000"))  # ручна ціна не перезаписується

    def test_no_digikey_prices(self):
        other = Product.objects.create(sku="X-1", name="X")
        l2 = ShopListing.objects.create(shop=self.shop, product=other)
        self.assertEqual(services.use_digikey_prices([l2]), (0, ["X-1"]))


class ShippingTests(TestCase):
    def setUp(self):
        from .models import ShippingZone
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True,
                                        free_shipping_enabled=True, free_shipping_threshold=Decimal("100"))
        ShippingZone.objects.create(shop=self.shop, name="DE", countries=["DE"], price=Decimal("6.90"))
        ShippingZone.objects.create(shop=self.shop, name="EU", countries=["AT", "FR"], price=Decimal("14.90"),
                                    free_shipping=False)
        self.p = Product.objects.create(sku="AN-1", name="Antenne", sale_price=Decimal("20.00"))
        create_movement(product=self.p, tx_type="Incoming", qty=50)
        ShopListing.objects.create(shop=self.shop, product=self.p)
        key = APIKey.objects.create(name="site", scopes=["products:read", "orders:write", "orders:read"],
                                    default_source="webshop")
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")

    def test_parse_countries(self):
        from .shipping import format_countries, parse_countries
        from .models import EU_COUNTRIES
        self.assertEqual(parse_countries("de, at;ch *"), ["DE", "AT", "CH", "*"])
        self.assertEqual(len(parse_countries("EU, CH")), len(EU_COUNTRIES) + 1)
        self.assertEqual(format_countries(parse_countries("EU CH")), "EU, CH")
        with self.assertRaises(ValueError):
            parse_countries("DE, Germany")

    def test_cost_and_free_threshold(self):
        from .shipping import shipping_cost
        self.assertEqual(shipping_cost(self.shop, "DE", 50), Decimal("6.90"))
        self.assertEqual(shipping_cost(self.shop, "DE", 100), Decimal("0"))
        self.assertEqual(shipping_cost(self.shop, "AT", 500), Decimal("14.90"))  # регіон без безкоштовної
        self.assertIsNone(shipping_cost(self.shop, "US", 50))

    def test_api_config(self):
        d = self.c.get("/api/v1/shop/shipping/").json()
        self.assertTrue(d["configured"])
        self.assertEqual(d["allowed_countries"], ["AT", "DE", "FR"])
        self.assertEqual(d["free_shipping"], {"enabled": True, "threshold": 100.0})
        self.assertEqual([z["free_from"] for z in d["zones"]], [100.0, None])

    def test_order_country_and_cost(self):
        base = {"client": "X", "shop": True, "lines": [{"sku": "AN-1", "qty": 2}]}
        r = self.c.post("/api/v1/orders/", {**base, "order_number": "WS-S1", "addr_country": "US"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "shipping_not_available")
        r = self.c.post("/api/v1/orders/", {**base, "order_number": "WS-S2", "addr_country": "de",
                                            "shipping_cost": "0"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        from sales.models import SalesOrder
        self.assertEqual(SalesOrder.objects.get(order_number="WS-S2").shipping_cost, Decimal("6.90"))

    def test_digikey_zone_import(self):
        from .shipping import import_digikey_zones
        created, updated = import_digikey_zones(self.shop, [
            {"code": "EU1", "countries": ["AT", "BE", "NL"], "price": 12.5},
            {"code": "US", "countries": ["US", "CA"], "price": 35}])
        self.assertEqual((created, updated), (2, 0))
        z = self.shop.shipping_zones.get(dk_code="EU1")
        self.assertEqual(z.countries, ["BE", "NL"])  # AT уже в ручному регіоні
        self.assertEqual(import_digikey_zones(self.shop, [{"code": "US", "countries": ["US"], "price": 30}]), (0, 1))

    def test_admin_pages(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("a", "a@x.y", "p"))
        self.assertEqual(self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/").status_code, 200)
        self.assertContains(self.client.get("/admin/shop/shop/"), "2 регіонів")
        r = self.client.post("/admin/inventory/product/", {"action": "action_set_category",
                                                           "_selected_action": [self.p.pk]})
        self.assertContains(r, "Змінити категорію товарів")
        from inventory.models import ProductCategory
        ProductCategory.objects.create(slug="antenna", name="Антени")
        self.client.post("/admin/inventory/product/", {"action": "action_set_category", "apply": "1",
                                                       "_selected_action": [self.p.pk], "category": "antenna"})
        self.p.refresh_from_db()
        self.assertEqual(self.p.category, "antenna")


class DefaultZonesTests(TestCase):
    def test_create_default_zones(self):
        from .models import ShippingZone
        from .shipping import create_default_zones, shipping_cost
        shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        ShippingZone.objects.create(shop=shop, name="Schweiz", countries=["CH"], price=Decimal("20"))
        self.assertEqual(create_default_zones(shop), 4)
        self.assertEqual(create_default_zones(shop), 0)  # повторно нічого не дублює
        self.assertEqual(shipping_cost(shop, "DE", 10), Decimal("6.90"))
        self.assertEqual(shipping_cost(shop, "FR", 10), Decimal("17.00"))
        self.assertEqual(shipping_cost(shop, "CH", 10), Decimal("20"))  # ручний регіон не перекрито
        self.assertEqual(shipping_cost(shop, "US", 10), Decimal("25.00"))
        self.assertIsNone(shipping_cost(shop, "RU", 10))
        shop.refresh_from_db()
        self.assertEqual((shop.free_shipping_enabled, shop.free_shipping_threshold), (False, Decimal("250.00")))

    def test_listing_shop_tabs_and_filter(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("a", "a@x.y", "p"))
        shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        ShopListing.objects.create(shop=shop, product=Product.objects.create(sku="A-1", name="A"))
        r = self.client.get("/admin/shop/shoplisting/")
        self.assertContains(r, "Усі магазини")
        self.assertContains(r, f"?shop__id__exact={shop.pk}")
        self.assertContains(self.client.get(f"/admin/shop/shoplisting/?shop__id__exact={shop.pk}"), "A-1")
