"""Дорожня карта підключення магазину (shop/onboarding.py): кроки, підказки, картка й список магазинів.
python manage.py test shop --settings=tabele.settings_test"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from api.models import APIKey, Webhook
from inventory.models import Product

from . import copying, onboarding
from .models import ShippingZone, Shop, ShopListing, ShopStaff


def by_key(shop):
    return {s["key"]: s for s in onboarding.steps(shop)}


class RoadmapTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("anna", "anna@example.com", "pw", first_name="Anna")
        self.shop = Shop.objects.create(name="Web", slug="webshop", url="https://shop.example.com",
                                        public_name="Sevskiy GmbH", payment_methods=["prepayment"])

    def ready(self):
        """Усе обов'язкове зроблено."""
        p = Product.objects.create(sku="AN-1", name="Antenne", sale_price=Decimal("20"))
        ShopListing.objects.create(shop=self.shop, product=p, price=Decimal("19.50"))
        ShippingZone.objects.create(shop=self.shop, name="DE", countries=["DE"], price=Decimal("6.90"))
        ShopStaff.objects.create(shop=self.shop, user=self.user, role=ShopStaff.ROLE_ADMIN)
        return APIKey.objects.create(name="Сайт", scopes=["products:read", "orders:read", "orders:write"],
                                     shop=self.shop, last_used=timezone.now())

    def test_new_shop_shows_what_is_missing(self):
        steps = by_key(self.shop)
        self.assertEqual(steps["identity"]["state"], onboarding.DONE)
        self.assertEqual(steps["url"]["state"], onboarding.DONE)
        for key in ("products", "shipping", "key", "staff", "connected"):
            self.assertEqual(steps[key]["state"], onboarding.TODO, key)
        self.assertIn("Підключити сайт (код)", steps["key"]["hint"])
        self.assertEqual(steps["staff"]["go"], "#staff-group")
        s = onboarding.summary(onboarding.steps(self.shop))
        self.assertFalse(s["complete"])
        self.assertEqual(s["next"]["key"], "products")
        Shop.objects.filter(pk=self.shop.pk).update(is_active=False)
        self.shop.refresh_from_db()
        self.assertEqual(by_key(self.shop)["active"]["state"], onboarding.TODO)

    def test_ready_shop_is_complete(self):
        self.ready()
        s = onboarding.summary(onboarding.steps(self.shop))
        self.assertTrue(s["complete"])
        self.assertEqual((s["passed"], s["required"]), (9, 9))
        self.assertEqual(s["next"]["key"], "webhook")   # далі — рекомендовані кроки

    def test_copy_fields_and_missing_scopes(self):
        self.ready()
        copy = copying.copy_shop(self.shop, "Web (копія)", "webshop-2")
        steps = by_key(copy)
        self.assertEqual(steps["identity"]["state"], onboarding.TODO)   # назва й код ще як у копії
        self.assertEqual(steps["url"]["state"], onboarding.TODO)
        self.assertEqual(steps["active"]["state"], onboarding.TODO)
        self.assertEqual(steps["key"]["state"], onboarding.TODO)        # ключі не копіюються
        APIKey.objects.create(name="Старий", scopes=["products:read"], shop=copy)
        self.assertIn("orders:write", by_key(copy)["key"]["detail"])

    def test_second_site_on_same_key_warns(self):
        self.ready()
        now = timezone.now().isoformat(timespec="seconds")
        old = (timezone.now() - timedelta(days=30)).isoformat(timespec="seconds")
        Shop.objects.filter(pk=self.shop.pk).update(site_mail_reports={
            "shop.example.com": {"at": now}, "test.example.com": {"at": now}, "old.example.com": {"at": old}})
        self.shop.refresh_from_db()
        step = by_key(self.shop)["connected"]
        self.assertEqual(step["state"], onboarding.WARN)
        self.assertIn("test.example.com", step["hint"])
        self.assertNotIn("old.example.com", step["hint"])   # давні звіти — не враховуються

    def test_webhook_wrong_path_and_stripe_without_key(self):
        key = self.ready()
        Webhook.objects.create(name="Сайт", url="https://shop.example.com/dist/api/shop/webhook.php", api_key=key,
                               last_status_code=404, last_delivery_at=timezone.now())
        step = by_key(self.shop)["webhook"]
        self.assertEqual(step["state"], onboarding.WARN)
        self.assertIn("https://shop.example.com/api/shop/webhook.php", step["hint"])
        Shop.objects.filter(pk=self.shop.pk).update(payment_methods=["stripe"])
        self.shop.refresh_from_db()
        self.assertEqual(by_key(self.shop)["payment"]["state"], onboarding.TODO)

    def test_card_and_list(self):
        self.ready()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        page = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        self.assertContains(page, "🧭 Підключення магазину")
        self.assertContains(page, "✅ Магазин підключено")
        self.assertContains(page, 'data-go="#staff-group"')
        listing = self.client.get("/admin/shop/shop/")
        self.assertContains(listing, "✅ підключено")
