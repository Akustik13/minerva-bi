"""Копія магазину без ключів (shop/copying.py), підсвічування полів, бейджі магазинів, маскування ключів.
python manage.py test shop --settings=tabele.settings_test"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from api.models import APIKey, Webhook
from core.secrets import mask, placeholder
from inventory.models import Product

from . import copying
from .badges import badges
from .models import ShippingZone, Shop, ShopListing, ShopMailTemplate, ShopPriceTier


class Base(TestCase):
    def setUp(self):
        self.src = Shop.objects.create(
            name="Web", slug="webshop", is_default=True, url="https://web.example.com", public_name="Sevskiy GmbH",
            payment_methods=["stripe", "prepayment"], accounts_enabled=True, assistant_mode="on",
            assistant_url="https://rag.example.com", assistant_key="bridge-secret-1234",
            stripe_secret_key="sk_test_51abcdefghijklmnop9876", stripe_webhook_secret="whsec_abcdefghijklmn5432",
            account_links={"de": "https://web.example.com/de/konto/"}, free_shipping_enabled=True,
            site_mail_reports={"web.example.com": {"at": "2026-10-26T08:00:00+00:00", "host": "smtp.example.com"}},
            assistant_sites={"web.example.com": {"at": "2026-10-26T08:00:00+00:00", "file": True}},
            free_shipping_threshold=Decimal("250"))
        ShippingZone.objects.create(shop=self.src, name="DE", countries=["DE"], price=Decimal("6.90"),
                                    delivery_days_min=1, delivery_days_max=2, overrides={"AT": {"price": "9"}})
        self.p = Product.objects.create(sku="AN-1", name="Antenne", sale_price=Decimal("20"))
        listing = ShopListing.objects.create(shop=self.src, product=self.p, price=Decimal("19.50"))
        ShopPriceTier.objects.create(listing=listing, min_qty=10, unit_price=Decimal("17"))
        ShopMailTemplate.objects.create(shop=self.src, kind="verify", lang="de", subject="Bitte bestätigen", intro="Hallo")
        APIKey.objects.create(name="Сайт", scopes=["products:read"], shop=self.src)


class CopyTests(Base):
    def test_copy_everything_except_keys(self):
        new = copying.copy_shop(self.src, "Web (копія)", "webshop-2")
        self.assertEqual((new.is_active, new.is_default, new.url, new.copied_from), (False, False, "", self.src))
        self.assertEqual((new.stripe_secret_key, new.stripe_webhook_secret, new.assistant_key), ("", "", ""))
        self.assertEqual((new.assistant_mode, new.assistant_url, new.public_name), ("on", "https://rag.example.com",
                                                                                  "Sevskiy GmbH"))
        self.assertEqual((new.payment_methods, new.accounts_enabled, new.account_links), (["stripe", "prepayment"],
                                                                                         True, {}))
        zone = new.shipping_zones.get()
        self.assertEqual((zone.price, zone.delivery_days_max, zone.overrides), (Decimal("6.90"), 2, {"AT": {"price": "9"}}))
        listing = new.listings.get()
        self.assertEqual((listing.product, listing.price, [t.min_qty for t in listing.tiers.all()]),
                         (self.p, Decimal("19.50"), [10]))
        self.assertEqual(new.mail_templates.get().subject, "Bitte bestätigen")
        self.assertEqual(new.api_keys.count(), 0)   # ключі API сайту — свої для кожного сайту
        self.assertEqual((new.site_mail_reports, new.assistant_sites), ({}, {}))   # звіти — лише від свого сайту
        self.assertEqual(self.src.listings.count(), 1)   # оригінал не змінився
        self.src.refresh_from_db()
        self.assertTrue(self.src.is_default)
        from sales.models import SalesSource
        self.assertTrue(SalesSource.objects.filter(slug="webshop-2").exists())   # нове джерело замовлень

    def test_review_fields_and_refresh(self):
        new = copying.copy_shop(self.src, "Web (копія)", "webshop-2")
        self.assertEqual(set(copying.pending(new)), {"name", "slug", "url", "public_name", "stripe_secret_key",
                                                     "stripe_webhook_secret", "assistant_key", "is_active"})
        new.name, new.url, new.is_active, new.stripe_secret_key = "Shop B", "https://b.example.com", True, "sk_test_x"
        new.save()
        self.assertEqual(set(copying.refresh(new)), {"slug", "public_name", "stripe_webhook_secret", "assistant_key"})
        new.refresh_from_db()
        self.assertEqual(set(new.review_fields), {"slug", "public_name", "stripe_webhook_secret", "assistant_key"})

    def test_deliberate_values_are_not_flagged(self):
        plain = Shop.objects.create(name="Plain", slug="plain")
        new = copying.copy_shop(plain, "Neuer Shop", "neu", "https://neu.example.com", listings=False, zones=False,
                                templates=False)
        self.assertEqual(copying.pending(new), ["is_active"])   # назву, код і адресу вже задано; ключів не було

    def test_optional_parts(self):
        new = copying.copy_shop(self.src, "B", "b", listings=False, zones=False, templates=False)
        self.assertEqual((new.listings.count(), new.shipping_zones.count(), new.mail_templates.count()), (0, 0, 0))

    def test_free_slug(self):
        self.assertEqual(copying.free_slug("webshop-2"), "webshop-2")
        Shop.objects.create(name="x", slug="webshop-2")
        self.assertEqual(copying.free_slug("webshop-2"), "webshop-2-2")
        self.assertEqual(copying.suggest(self.src)["name"], "Web (копія)")


class CopyAdminTests(Base):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))

    def test_copy_page_and_create(self):
        page = self.client.get(f"/admin/shop/shop/{self.src.pk}/copy/")
        self.assertContains(page, "Не копіюється")
        self.assertContains(page, "webshop-2")
        self.assertNotContains(page, "sk_test_51")   # ключі на сторінці не показуються
        r = self.client.post(f"/admin/shop/shop/{self.src.pk}/copy/", {
            "name": "Web (копія)", "slug": "webshop-2", "url": "", "listings": "on", "zones": "on"})
        new = Shop.objects.get(slug="webshop-2")
        self.assertRedirects(r, f"/admin/shop/shop/{new.pk}/change/", fetch_redirect_response=False)
        self.assertEqual((new.listings.count(), new.shipping_zones.count(), new.mail_templates.count()), (1, 1, 0))
        card = self.client.get(f"/admin/shop/shop/{new.pk}/change/")
        self.assertContains(card, "cp-banner")
        self.assertContains(card, 'data-field="url"')
        self.assertContains(card, "Копія магазину «Web»")
        listing = self.client.get("/admin/shop/shop/")
        self.assertContains(listing, "⚠ змінити")

    def test_duplicate_slug_rejected_and_action(self):
        r = self.client.post(f"/admin/shop/shop/{self.src.pk}/copy/", {"name": "X", "slug": "webshop"})
        self.assertContains(r, "Такий код уже є")
        r = self.client.post("/admin/shop/shop/", {"action": "action_copy", "_selected_action": [self.src.pk]})
        self.assertRedirects(r, f"/admin/shop/shop/{self.src.pk}/copy/", fetch_redirect_response=False)

    def test_review_done(self):
        new = copying.copy_shop(self.src, "Web (копія)", "webshop-2")
        self.assertEqual(self.client.get(f"/admin/shop/shop/{new.pk}/review-done/").status_code, 403)
        self.client.post(f"/admin/shop/shop/{new.pk}/review-done/")
        new.refresh_from_db()
        self.assertEqual(new.review_fields, {})

    def test_secret_fields_masked(self):
        card = self.client.get(f"/admin/shop/shop/{self.src.pk}/change/")
        self.assertContains(card, mask("sk_test_51abcdefghijklmnop9876"))
        self.assertContains(card, "sk_test_51••••••••9876")
        self.assertNotContains(card, "sk_test_51abcdefghijklmnop9876")
        self.assertNotContains(card, "bridge-secret-1234")
        key = APIKey.objects.get(name="Сайт")
        self.assertContains(card, mask(key.key))
        page = self.client.get(f"/admin/api/apikey/{key.pk}/change/")
        self.assertContains(page, "📋 Копіювати")
        hook = Webhook.objects.create(name="Сайт", url="https://web.example.com/api/shop/webhook.php")
        page = self.client.get(f"/admin/api/webhook/{hook.pk}/change/")
        self.assertContains(page, mask(hook.secret))


class BadgeAndMaskTests(Base):
    def test_badges_show_shops_and_inactive_copy(self):
        copying.copy_shop(self.src, "Web B", "webshop-b")
        html = str(badges(self.p.shop_listings.select_related("shop")))
        self.assertIn(">webshop<", html)
        self.assertIn(">webshop-b<", html)
        self.assertIn("dashed", html)   # копія вимкнена — пунктир
        self.assertIn("магазин вимкнено", html)
        self.assertIn("—", str(badges([])))

    def test_mask(self):
        self.assertEqual(mask("sk_live_51ABCDEFGHIJKLMN0123"), "sk_live_51••••••••0123")
        self.assertEqual(mask("whsec_abcdefghijklmnop9999"), "whsec_ab••••••••9999")
        self.assertEqual(mask("0123456789abcdef0123456789abcdef"), "0123••••••••cdef")
        self.assertEqual(mask("short"), "••••••••rt")
        self.assertEqual(mask(""), "")
        self.assertIn("введіть новий", placeholder("abcdefghijklmnopqrstuvwxyz"))
        self.assertEqual(placeholder(""), "не задано")
