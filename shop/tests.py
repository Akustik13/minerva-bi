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


class BasePriceTests(TestCase):
    def setUp(self):
        from bots.models import DigiKeyListing
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        self.p = Product.objects.create(sku="3228-AN-1-ND", name="Antenne", sale_price=Decimal("20.00"))
        self.dk = DigiKeyListing.objects.create(product=self.p, dk_offer_id="f11e", dk_prices=[
            {"qty": 1, "price": 10.0}, {"qty": 10, "price": 9.0}, {"qty": 100, "price": 8.0}])
        self.l = ShopListing.objects.create(shop=self.shop, product=self.p, price=Decimal("25"))
        ShopSettings.get()

    def _tiers(self):
        return [(t.min_qty, t.unit_price) for t in self.l.tiers.order_by("min_qty")]

    def _set(self, rows, currency="EUR", user=None):
        from inventory.services.base_prices import set_base_prices
        self.p.refresh_from_db()
        return set_base_prices(self.p, rows, currency=currency, user=user)

    def test_import_from_digikey_with_history(self):
        from inventory.services.base_prices import import_from_digikey
        self.assertTrue(import_from_digikey(self.p))
        self.p.refresh_from_db()
        self.assertEqual(self.p.base_prices[0], {"min_qty": 1, "unit_price": "10"})
        self.assertIn("DigiKey · офер f11e", self.p.base_prices_source)
        h = self.p.price_history.get()
        self.assertEqual((h.source, h.old_prices, len(h.new_prices)), ("digikey", [], 3))
        self.assertFalse(import_from_digikey(self.p))  # без змін — без запису в історію
        self.assertEqual(self.p.price_history.count(), 1)

    def test_listing_follows_base_prices_with_factor(self):
        self._set([{"min_qty": 1, "unit_price": "10"}, {"min_qty": 10, "unit_price": "9"},
                   {"min_qty": 100, "unit_price": "8"}])
        n, missing, no_rate = services.use_base_prices([self.l], Decimal("90"))
        self.assertEqual((n, missing, no_rate), (1, [], []))
        self.l.refresh_from_db()
        self.assertEqual((self.l.price_source, self.l.price), (ShopListing.PRICE_BASE, Decimal("9.0000")))
        self.assertEqual(self._tiers(), [(10, Decimal("8.1000")), (100, Decimal("7.2000"))])
        # зміна базових цін → позиція оновлюється сама
        user = User.objects.create_user("anna")
        self._set([{"min_qty": 1, "unit_price": "11"}, {"min_qty": 50, "unit_price": "9.5"}], user=user)
        self.l.refresh_from_db()
        self.assertEqual(self.l.price, Decimal("9.9000"))
        self.assertEqual(self._tiers(), [(50, Decimal("8.5500"))])
        self.p.refresh_from_db()
        self.assertEqual(self.p.base_prices_source, "Змінено вручну: anna")
        self.assertEqual(self.p.price_history.first().user_label, "anna")
        # ручна ціна від'єднує
        services.set_price([self.l], Decimal("12"))
        self._set([{"min_qty": 1, "unit_price": "5"}])
        self.l.refresh_from_db()
        self.assertEqual((self.l.price_source, self.l.price), (ShopListing.PRICE_MANUAL, Decimal("12.0000")))

    def test_currency_conversion_needs_rate(self):
        self._set([{"min_qty": 1, "unit_price": "10"}, {"min_qty": 10, "unit_price": "9"}], currency="USD")
        self.assertEqual(services.use_base_prices([self.l])[2], ["3228-AN-1-ND (USD)"])
        st = ShopSettings.get()
        st.fx_rates = {"USD": "0.8"}
        st.save()
        n, _, _ = services.use_base_prices([self.l])
        self.l.refresh_from_db()
        self.assertEqual((n, self.l.price), (1, Decimal("8.0000")))
        st.fx_rates = {"USD": "0.9"}
        st.save()  # новий курс → перерахунок
        self.l.refresh_from_db()
        self.assertEqual(self.l.price, Decimal("9.0000"))

    def test_admin_edit_records_user(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        admin_user = User.objects.create_superuser("boss", "b@x.y", "p")
        self.client.force_login(admin_user)
        url = f"/admin/inventory/product/{self.p.pk}/change/"
        r = self.client.get(url)
        self.assertContains(r, "Базові ціни (ступені)")
        form = r.context["adminform"].form
        data = {k: v for k, v in form.initial.items() if isinstance(v, (str, int, Decimal)) and not isinstance(v, bool)}
        data.update({"sku": self.p.sku, "category": "other", "kind": self.p.kind, "unit_type": self.p.unit_type,
                     "bom_type": self.p.bom_type, "lifecycle_status": "active", "reorder_point": "0",
                     "is_active": "on", "base_prices_text": "1: 4,89\n10: 4.52", "base_currency": "usd",
                     "base_note": "нова ціна від постачальника", "tech_attributes": "{}"})
        for fs in r.context["inline_admin_formsets"]:
            mf = fs.formset.management_form
            for f in mf:
                data[f.html_name] = f.value() if f.value() is not None else 0
        resp = self.client.post(url, data)
        self.assertEqual(resp.status_code, 302, getattr(resp, "context", None) and resp.context["adminform"].form.errors)
        self.p.refresh_from_db()
        self.assertEqual((self.p.base_price_currency, self.p.base_prices[1]["unit_price"]), ("USD", "4.52"))
        h = self.p.price_history.first()
        self.assertEqual((h.user_label, h.source, h.note), ("boss", "manual", "нова ціна від постачальника"))


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


class ShopCardTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("a", "a@x.y", "p"))
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)

    def test_key_by_source_is_listed(self):
        APIKey.objects.create(name="site-key", scopes=["products:read"], default_source="webshop")
        r = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        self.assertContains(r, "site-key")
        self.assertContains(r, "за джерелом «webshop»")
        self.assertContains(r, "Створити базові регіони")

    def test_unlinked_key_shown_separately(self):
        self.shop.slug = "sevskiyde"
        self.shop.save()
        APIKey.objects.create(name="TestKey", scopes=["products:read"])
        APIKey.objects.create(name="site-key", scopes=["products:read"], default_source="webshop")
        r = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        self.assertContains(r, "прив'язаних немає")
        self.assertContains(r, "Без прив'язки")

    def test_shop_order_source_is_shop_code(self):
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.shop.slug = "sevskiyde"
        self.shop.save()
        p = Product.objects.create(sku="AN-1", name="A", sale_price=Decimal("5"))
        ShopListing.objects.create(shop=self.shop, product=p)
        key = APIKey.objects.create(name="site", scopes=["orders:write", "orders:read"], default_source="webshop")
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        r = c.post("/api/v1/orders/", {"order_number": "WS-1", "client": "X", "shop": True, "source": "webshop",
                                       "lines": [{"sku": "AN-1", "qty": 1}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["source"], "sevskiyde")
        from sales.models import SalesSource
        self.assertTrue(SalesSource.objects.filter(slug="sevskiyde").exists())

    def test_default_zones_button(self):
        url = f"/admin/shop/shop/{self.shop.pk}/default-zones/"
        self.assertEqual(self.client.get(url).status_code, 403)
        r = self.client.post(url)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.shop.shipping_zones.count(), 4)


class PromoTests(TestCase):
    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        self.today = timezone.localdate()
        self.td = timedelta
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        self.p = Product.objects.create(sku="AN-1", name="Antenne", sale_price=Decimal("20.00"))
        self.l = ShopListing.objects.create(shop=self.shop, product=self.p)
        ShopPriceTier.objects.create(listing=self.l, min_qty=10, unit_price=Decimal("18.00"))
        key = APIKey.objects.create(name="site", scopes=["products:read", "orders:write", "orders:read"],
                                    default_source="webshop")
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")

    def test_offer_applies_to_api_and_orders(self):
        services.set_offer([self.l], Decimal("15"), self.today + self.td(days=3))
        d = self.c.get("/api/v1/shop/products/AN-1/").json()
        self.assertEqual(d["price"], 17.0)
        self.assertEqual(d["price_breaks"], [{"min_qty": 1, "unit_price": 17.0}, {"min_qty": 10, "unit_price": 15.3}])
        self.assertEqual(d["offer"]["percent"], 15.0)
        self.assertEqual(d["offer"]["regular_price"], 20.0)
        r = self.c.post("/api/v1/orders/", {"order_number": "WS-P1", "client": "X", "shop": True,
                                            "lines": [{"sku": "AN-1", "qty": 10}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["lines"][0]["unit_price"], 15.3)

    def test_expired_offer_and_new_badge(self):
        services.set_offer([self.l], Decimal("15"), self.today - self.td(days=1))
        services.set_new([self.l], self.today + self.td(days=30))
        d = self.c.get("/api/v1/shop/products/AN-1/").json()
        self.assertIsNone(d["offer"])
        self.assertEqual(d["price"], 20.0)
        self.assertTrue(d["is_new"])
        services.set_new([self.l], None)
        self.assertFalse(self.c.get("/api/v1/shop/products/AN-1/").json()["is_new"])

    def test_admin_promo_actions(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("a", "a@x.y", "p"))
        url = "/admin/shop/shoplisting/"
        self.client.post(url, {"action": "action_offer", "apply": "1", "_selected_action": [self.l.pk],
                               "percent": "20", "until": ""})
        self.client.post(url, {"action": "action_new", "apply": "1", "_selected_action": [self.l.pk], "days": "60"})
        self.l.refresh_from_db()
        self.assertEqual(self.l.discount_percent, Decimal("20"))
        self.assertTrue(services.is_new(self.l))
        r = self.client.get(url + "?promo=offer")
        self.assertContains(r, "−20 %")
        self.assertContains(r, "NEU")
        self.assertContains(self.client.get(f"{url}{self.l.pk}/change/"), "Акція −20 %")


class LifecycleTests(TestCase):
    def setUp(self):
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        self.new = Product.objects.create(sku="AN-2", name="Antenne v2", sale_price=Decimal("12"))
        self.old = Product.objects.create(sku="AN-1", name="Antenne v1", sale_price=Decimal("10"),
                                          lifecycle_status=Product.Lifecycle.DISCONTINUED, successor=self.new)
        create_movement(product=self.old, tx_type="Incoming", qty=3)
        for p in (self.old, self.new):
            ShopListing.objects.create(shop=self.shop, product=p)
        key = APIKey.objects.create(name="site", scopes=["products:read", "orders:write", "orders:read"],
                                    default_source="webshop")
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")

    def test_api_and_eol_only_from_stock(self):
        d = self.c.get("/api/v1/shop/products/AN-1/").json()
        self.assertEqual(d["lifecycle_status"], "discontinued")
        self.assertEqual(d["successor"], {"sku": "AN-2", "name": "Antenne v2"})
        base = {"client": "X", "shop": True}
        r = self.c.post("/api/v1/orders/", {**base, "order_number": "WS-L1", "lines": [{"sku": "AN-1", "qty": 5}]},
                        format="json")
        self.assertEqual(r.status_code, 409)
        r = self.c.post("/api/v1/orders/", {**base, "order_number": "WS-L2", "lines": [{"sku": "AN-1", "qty": 3}]},
                        format="json")
        self.assertEqual(r.status_code, 201, r.content)
        # активний товар без залишку — як і раніше «під замовлення»
        r = self.c.post("/api/v1/orders/", {**base, "order_number": "WS-L3", "lines": [{"sku": "AN-2", "qty": 5}]},
                        format="json")
        self.assertEqual(r.status_code, 201, r.content)

    def test_admin_lifecycle_action(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("a", "a@x.y", "p"))
        self.client.post("/admin/inventory/product/", {"action": "action_set_lifecycle", "apply": "1",
                                                       "_selected_action": [self.new.pk], "status": "nrnd",
                                                       "set_successor": "on", "successor": ""})
        self.new.refresh_from_db()
        self.assertEqual(self.new.lifecycle_status, "nrnd")
        r = self.client.get("/admin/shop/shoplisting/")
        self.assertContains(r, "NRND")
        self.assertContains(r, "EOL")
        self.assertEqual(self.client.get(f"/admin/inventory/product/{self.old.pk}/change/").status_code, 200)


class ShopLifecycleOverrideTests(TestCase):
    def setUp(self):
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.web = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        self.market = Shop.objects.create(name="Market", slug="market")
        self.p = Product.objects.create(sku="AN-1", name="v1", sale_price=Decimal("10"))
        self.p2 = Product.objects.create(sku="AN-2", name="v2", sale_price=Decimal("12"))
        self.lw = ShopListing.objects.create(shop=self.web, product=self.p)
        self.lm = ShopListing.objects.create(shop=self.market, product=self.p)
        ShopListing.objects.create(shop=self.market, product=self.p2)

    def _client(self, shop):
        key = APIKey.objects.create(name=shop.slug, scopes=["products:read", "orders:write"], shop=shop)
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        return c

    def test_override_only_in_one_shop(self):
        self.lm.lifecycle_status = "discontinued"
        self.lm.successor = self.p2
        self.lm.save()
        self.p.refresh_from_db()
        self.assertEqual(self.p.lifecycle_status, "active")  # склад не змінено
        m = self._client(self.market).get("/api/v1/shop/products/AN-1/").json()
        self.assertEqual((m["lifecycle_status"], m["successor"]["sku"]), ("discontinued", "AN-2"))
        w = self._client(self.web).get("/api/v1/shop/products/AN-1/").json()
        self.assertEqual((w["lifecycle_status"], w["successor"]), ("active", None))
        # EOL у магазині Market без залишку — замовити не можна; на сайті — можна (під замовлення)
        r = self._client(self.market).post("/api/v1/orders/", {"order_number": "M-1", "client": "X", "shop": True,
                                                               "lines": [{"sku": "AN-1", "qty": 1}]}, format="json")
        self.assertEqual(r.status_code, 409)
        r = self._client(self.web).post("/api/v1/orders/", {"order_number": "W-1", "client": "X", "shop": True,
                                                            "lines": [{"sku": "AN-1", "qty": 1}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)

    def test_warehouse_status_applies_everywhere_unless_overridden(self):
        self.p.lifecycle_status = "nrnd"
        self.p.save()
        self.lw.lifecycle_status = "active"  # у цьому магазині — як активний
        self.lw.save()
        self.assertEqual(self._client(self.market).get("/api/v1/shop/products/AN-1/").json()["lifecycle_status"], "nrnd")
        self.assertEqual(self._client(self.web).get("/api/v1/shop/products/AN-1/").json()["lifecycle_status"], "active")

    def test_admin_action(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("a", "a@x.y", "p"))
        url = "/admin/shop/shoplisting/"
        self.client.post(url, {"action": "action_lifecycle", "apply": "1", "_selected_action": [self.lm.pk],
                               "status": "nrnd", "set_successor": "on", "successor": self.p2.pk})
        self.lm.refresh_from_db()
        self.assertEqual((self.lm.lifecycle_status, self.lm.successor_id), ("nrnd", self.p2.pk))
        self.assertContains(self.client.get(url + "?life=own"), "NRND")
        self.assertEqual(self.client.get(f"{url}{self.lm.pk}/change/").status_code, 200)


class ProductApiDigiKeyAttrsTests(TestCase):
    def setUp(self):
        from bots.models import DigiKeyListing
        self.p = Product.objects.create(sku="AN250202-04C-175-MHF1", name="Antenne", tech_attributes={
            "Antenna Type": "PCB Trace", "Gain": "2dBi, 4dBi", "Frequency Range": "2.4GHz ~ 2.485GHz",
            "hts": "8529.10.9100", "eccnNumber": "EAR99", "gtin": "", "msl": "  ", "reach": None})
        DigiKeyListing.objects.create(product=self.p, dk_offer_id="f11e", dk_prices=[
            {"qty": 1, "price": 4.89}, {"qty": 10, "price": 4.52}, {"qty": 50, "price": 4.18}])
        from bots.models import DigiKeyConfig
        cfg = DigiKeyConfig.get()
        cfg.locale_currency = "USD"
        cfg.save()
        from inventory.services.base_prices import import_from_digikey
        import_from_digikey(self.p)
        Product.objects.create(sku="X-1", name="Без DigiKey")
        key = APIKey.objects.create(name="erp", scopes=["products:read"])
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")

    def test_product_detail(self):
        d = self.c.get("/api/v1/products/?sku=AN250202-04C-175-MHF1").json()["results"][0]
        self.assertEqual(d["tech_attributes"], {"Antenna Type": "PCB Trace", "Gain": "2dBi, 4dBi",
                                                "Frequency Range": "2.4GHz ~ 2.485GHz"})
        self.assertEqual(d["compliance"], {"hts": "8529.10.9100", "eccnNumber": "EAR99"})
        self.assertEqual(d["base_prices"]["price_breaks"], [{"min_qty": 1, "unit_price": 4.89},
                                                            {"min_qty": 10, "unit_price": 4.52},
                                                            {"min_qty": 50, "unit_price": 4.18}])
        self.assertEqual(d["base_prices"]["currency"], "USD")
        self.assertIn("DigiKey · офер f11e", d["base_prices"]["source"])
        x = self.c.get("/api/v1/products/?sku=X-1").json()["results"][0]
        self.assertIsNone(x["base_prices"])
        self.assertEqual((x["tech_attributes"], x["compliance"]), ({}, {}))

    def test_list_with_stock_and_shop(self):
        r = self.c.get("/api/v1/products/?with_stock=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("base_prices", r.json()["results"][0])
        shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        ShopListing.objects.create(shop=shop, product=self.p)
        d = self.c.get("/api/v1/shop/products/AN250202-04C-175-MHF1/").json()
        self.assertEqual(d["tech_attributes"]["Antenna Type"], "PCB Trace")
        self.assertEqual(d["base_prices"]["currency"], "USD")  # базові ціни як є, у своїй валюті
        self.assertIsNone(d["price"])  # курсу USD немає — ціна магазину не підставляється
        self.assertNotIn("compliance", d)


class ProductListShopsAndBulkPricesTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("boss", "b@x.y", "p"))
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        self.p = Product.objects.create(sku="AN-1", name="A", base_price_currency="USD",
                                        base_prices=[{"min_qty": 1, "unit_price": "10"},
                                                     {"min_qty": 10, "unit_price": "9"}])
        ShopListing.objects.create(shop=self.shop, product=self.p)
        st = ShopSettings.get()
        st.fx_rates = {"USD": "0.8"}
        st.save()

    def test_changelist_shows_shops(self):
        r = self.client.get("/admin/inventory/product/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, ">webshop</a>")
        self.assertContains(r, "Базові ціни: ±% / валюта")
        Product.objects.create(sku="NO-SHOP", name="X")
        r = self.client.get("/admin/inventory/product/?in_shop=none")
        self.assertContains(r, "NO-SHOP")
        self.assertNotContains(r, ">AN-1<")
        self.assertContains(self.client.get(f"/admin/inventory/product/?in_shop={self.shop.pk}"), "AN-1")

    def test_bulk_percent_and_convert(self):
        url = "/admin/inventory/product/"
        self.client.post(url, {"action": "action_base_bulk", "apply": "1", "_selected_action": [self.p.pk],
                               "operation": "percent", "percent": "10", "rounding": "0.01"})
        self.p.refresh_from_db()
        self.assertEqual([r["unit_price"] for r in self.p.base_prices], ["11", "9.9"])
        self.client.post(url, {"action": "action_base_bulk", "apply": "1", "_selected_action": [self.p.pk],
                               "operation": "convert", "currency": "eur", "rounding": "0.01"})
        self.p.refresh_from_db()
        self.assertEqual((self.p.base_price_currency, self.p.base_prices[0]["unit_price"]), ("EUR", "8.8"))
        h = self.p.price_history.first()
        self.assertEqual((h.user_label, h.note[:17]), ("boss", "Масова зміна: USD"))

    def test_template(self):
        from inventory.services.base_prices import from_template
        rows = from_template([{"min_qty": 1, "unit_price": "100"}], [{"min_qty": 10, "discount": 5}], "0.01")
        self.assertEqual(rows, [{"min_qty": 1, "unit_price": "100"}, {"min_qty": 10, "unit_price": "95"}])



class BasePriceFallbackTests(TestCase):
    def setUp(self):
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        self.p = Product.objects.create(sku="LA220102-01A", name="LA", base_price_currency="USD", base_prices=[
            {"min_qty": 1, "unit_price": "279"}, {"min_qty": 10, "unit_price": "268.39"}])
        self.l = ShopListing.objects.create(shop=self.shop, product=self.p)
        key = APIKey.objects.create(name="site", scopes=["products:read", "orders:write"], shop=self.shop)
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")

    def test_fallback_needs_rate_then_used_in_api_and_orders(self):
        d = self.c.get("/api/v1/shop/products/LA220102-01A/").json()
        self.assertEqual((d["price"], d["price_origin"]), (None, None))
        self.assertEqual(d["base_prices"]["price_breaks"][0], {"min_qty": 1, "unit_price": 279.0})
        st = ShopSettings.get()
        st.fx_rates = {"USD": "0.86"}
        st.save()
        d = self.c.get("/api/v1/shop/products/LA220102-01A/").json()
        self.assertEqual((d["price"], d["price_origin"]), (239.94, "base"))
        self.assertEqual(d["price_breaks"], [{"min_qty": 1, "unit_price": 239.94}, {"min_qty": 10, "unit_price": 230.82}])
        r = self.c.post("/api/v1/orders/", {"order_number": "WS-F1", "client": "X", "shop": True,
                                            "lines": [{"sku": "LA220102-01A", "qty": 10}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["lines"][0]["unit_price"], 230.82)

    def test_own_or_sale_price_wins(self):
        self.p.sale_price = Decimal("250")
        self.p.save()
        d = self.c.get("/api/v1/shop/products/LA220102-01A/").json()
        self.assertEqual((d["price"], d["price_origin"]), (250.0, "sale_price"))
        self.l.price = Decimal("260")
        self.l.save()
        d = self.c.get("/api/v1/shop/products/LA220102-01A/").json()
        self.assertEqual((d["price"], d["price_origin"]), (260.0, "listing"))


class ShopSettingsFormTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("boss", "b@x.y", "p"))
        self.st = ShopSettings.get()
        self.url = f"/admin/shop/shopsettings/{self.st.pk}/change/"

    def _post(self, fx, breaks):
        import json
        return self.client.post(self.url, {"default_markup": "100", "rounding": "0.01",
                                           "fx_rates": json.dumps(fx), "price_breaks": json.dumps(breaks)})

    def test_page_and_valid_save(self):
        r = self.client.get(self.url)
        self.assertContains(r, "+ Додати валюту")
        r = self._post({"usd": "0,86"}, [{"min_qty": 100, "discount": 12}, {"min_qty": 10, "discount": "5,5"}])
        self.assertEqual(r.status_code, 302)
        self.st.refresh_from_db()
        self.assertEqual(self.st.fx_rates, {"USD": 0.86})
        self.assertEqual(self.st.price_breaks, [{"min_qty": 10, "discount": 5.5}, {"min_qty": 100, "discount": 12.0}])

    def test_invalid_values_rejected(self):
        self.assertContains(self._post({"US": 1}, []), "3 латинських літер")
        self.assertContains(self._post({"USD": -1}, []), "більше 0")
        self.assertContains(self._post({}, [{"min_qty": 1, "discount": 5}]), "від 2 шт.")
        self.assertContains(self._post({}, [{"min_qty": 10, "discount": 120}]), "від 0 до 99")


class ListingActionGroupsTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("boss", "b@x.y", "p"))
        shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        self.l = ShopListing.objects.create(shop=shop, product=Product.objects.create(sku="AN-1", name="A"))

    def test_grouped_and_working(self):
        r = self.client.get("/admin/shop/shoplisting/")
        for g in ("👁 Видимість", "💶 Ціна", "📊 Ступені цін", "🏷 Акції та новинки", "♻️ Статус", "📋 Інше"):
            self.assertContains(r, f'<optgroup label="{g}">')
        self.client.post("/admin/shop/shoplisting/", {"action": "action_hide", "_selected_action": [self.l.pk]})
        self.l.refresh_from_db()
        self.assertFalse(self.l.is_visible)


class ListingRateInfoTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("boss", "b@x.y", "p"))
        shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        p = Product.objects.create(sku="AN-1", name="A", base_price_currency="USD",
                                   base_prices=[{"min_qty": 1, "unit_price": "4.69"}])
        self.l = ShopListing.objects.create(shop=shop, product=p)
        self.url = f"/admin/shop/shoplisting/{self.l.pk}/change/"

    def test_rate_shown_with_link(self):
        r = self.client.get(self.url)
        self.assertContains(r, "Немає курсу USD → EUR")
        st = ShopSettings.get()
        st.fx_rates = {"USD": 0.86}
        st.save()
        r = self.client.get(self.url)
        self.assertContains(r, "курс 1 USD = 0,86 EUR")
        self.assertContains(r, f'href="/admin/shop/shopsettings/{st.pk}/change/"')
        self.assertContains(r, "≈ 4.03 EUR")
        self.assertContains(r, "взято базові ціни товару")
