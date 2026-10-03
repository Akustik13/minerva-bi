"""Тести магазину:  python manage.py test shop api --settings=tabele.settings_test"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from api.models import APIKey
from inventory.models import InventorySettings, Location, Product
from inventory.services.stock import create_movement

from . import services
from .models import ShopPriceTier, ShopSettings


class PricingTests(TestCase):
    def setUp(self):
        self.p = Product.objects.create(sku="AN-1", name="Antenne", sale_price=Decimal("20.00"),
                                        purchase_price=Decimal("8.00"), shop_visible=True)

    def test_round_modes(self):
        self.assertEqual(services.round_price(Decimal("12.344"), "0.01"), Decimal("12.3400"))
        self.assertEqual(services.round_price(Decimal("12.33"), "0.05"), Decimal("12.3500"))
        self.assertEqual(services.round_price(Decimal("12.05"), "x.99"), Decimal("11.9900"))
        self.assertEqual(services.round_price(Decimal("12.70"), "x.99"), Decimal("12.9900"))
        self.assertEqual(services.round_price(Decimal("0.30"), "x.90"), Decimal("0.9000"))

    def test_generate_tiers_and_unit_price(self):
        services.generate_tiers([self.p], [{"min_qty": 10, "discount": 10}, {"min_qty": 100, "discount": 25}], "0.01")
        self.assertEqual([(r["min_qty"], r["unit_price"]) for r in services.price_breaks(self.p)],
                         [(1, Decimal("20.00")), (10, Decimal("18.0000")), (100, Decimal("15.0000"))])
        self.assertEqual(services.unit_price_for(self.p, 9), Decimal("20.00"))
        self.assertEqual(services.unit_price_for(self.p, 10), Decimal("18.0000"))
        self.assertEqual(services.unit_price_for(self.p, 250), Decimal("15.0000"))

    def test_adjust_scales_tiers(self):
        services.generate_tiers([self.p], [{"min_qty": 10, "discount": 10}], "0.01")
        services.adjust_prices([self.p], 10, "0.01")
        self.p.refresh_from_db()
        self.assertEqual(self.p.shop_price, Decimal("22.0000"))
        self.assertEqual(self.p.shop_tiers.get().unit_price, Decimal("19.8000"))

    def test_price_from_purchase(self):
        n, skipped = services.price_from_purchase([self.p], 150, "0.01")
        self.p.refresh_from_db()
        self.assertEqual((n, skipped, self.p.shop_price), (1, [], Decimal("20.0000")))

    def test_settings_default_schedule(self):
        self.assertTrue(ShopSettings.get().price_breaks)


class ShopApiTierTests(TestCase):
    def setUp(self):
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.p = Product.objects.create(sku="AN-1", name="Antenne", sale_price=Decimal("20.00"), shop_visible=True)
        create_movement(product=self.p, tx_type="Incoming", qty=500)
        ShopPriceTier.objects.create(product=self.p, min_qty=10, unit_price=Decimal("18.00"))
        ShopPriceTier.objects.create(product=self.p, min_qty=100, unit_price=Decimal("15.00"))
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
        self.admin = User.objects.create_superuser("a", "a@x.y", "p")
        self.client.force_login(self.admin)
        self.p = Product.objects.create(sku="AN-1", name="Antenne", sale_price=Decimal("20.00"))

    def test_changelist_and_settings(self):
        self.assertEqual(self.client.get("/admin/shop/shopproduct/").status_code, 200)
        self.assertEqual(self.client.get("/admin/shop/shopproduct/?price_state=tiers&stock=in").status_code, 200)
        r = self.client.get("/admin/shop/shopsettings/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get(r["Location"]).status_code, 200)
        self.assertEqual(self.client.get(f"/admin/shop/shopproduct/{self.p.pk}/change/").status_code, 200)

    def test_publish_and_percent_actions(self):
        url = "/admin/shop/shopproduct/"
        self.client.post(url, {"action": "action_publish", "_selected_action": [self.p.pk]})
        self.p.refresh_from_db()
        self.assertTrue(self.p.shop_visible)
        r = self.client.post(url, {"action": "action_adjust", "_selected_action": [self.p.pk]})
        self.assertContains(r, "Зміна ціни")  # проміжна форма
        self.client.post(url, {"action": "action_adjust", "_selected_action": [self.p.pk], "apply": "1",
                               "percent": "-10", "scale_tiers": "on"})
        self.p.refresh_from_db()
        self.assertEqual(self.p.shop_price, Decimal("18.0000"))

    def test_set_price_action(self):
        self.client.post("/admin/shop/shopproduct/", {"action": "action_set_price", "_selected_action": [self.p.pk],
                                                      "apply": "1", "price": "12.5", "regenerate": "on"})
        self.p.refresh_from_db()
        self.assertEqual(self.p.shop_price, Decimal("12.5000"))
        self.assertTrue(self.p.shop_tiers.exists())

    def test_custom_tiers_action(self):
        self.client.post("/admin/shop/shopproduct/", {"action": "action_tiers_custom", "_selected_action": [self.p.pk],
                                                      "apply": "1", "schedule": "10: 5\n100: 20"})
        self.assertEqual(list(self.p.shop_tiers.values_list("min_qty", flat=True)), [10, 100])
