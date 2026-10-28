"""Бейджі «де продається товар» (shop/badges.py): магазини + DigiKey Marketplace — у товарах, огляді складу й каталозі.
python manage.py test shop --settings=tabele.settings_test"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from bots.models import DigiKeyListing
from inventory.models import Product

from .badges import badges, dk_badge, dk_listing
from .models import Shop, ShopListing


class DigiKeyBadgeTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        self.p = Product.objects.create(sku="FT220401-2", name="Filter", sale_price=Decimal("5"))
        ShopListing.objects.create(shop=self.shop, product=self.p, is_visible=True)
        self.dk = DigiKeyListing.objects.create(product=self.p, dk_title="Filter", dk_description="RF filter",
                                                sync_status=DigiKeyListing.SYNC_PUBLISHED, dk_is_active=True,
                                                dk_quantity_available=6, dk_prices=[{"qty": 1, "price": 9.5}])

    def test_states(self):
        html = str(dk_badge(self.dk))
        self.assertIn(reverse("admin:bots_digikeylisting_change", args=[self.dk.pk]), html)
        self.assertIn("виставлено · на DigiKey 6 шт. · ціна за 1 шт. 9.50", html)
        self.assertIn("background:#c8102e", html)
        for status, active, expect in [(DigiKeyListing.SYNC_PUBLISHED, False, "вимкнено на DigiKey"),
                                       (DigiKeyListing.SYNC_STAGED, True, "очікує затвердження"),
                                       (DigiKeyListing.SYNC_DRAFT, True, "чернетка"),
                                       (DigiKeyListing.SYNC_ERROR, True, "помилка синхронізації")]:
            self.dk.sync_status, self.dk.dk_is_active = status, active
            self.assertIn(expect, str(dk_badge(self.dk)))
        self.assertEqual(dk_badge(None), "")
        other = Product.objects.create(sku="NO-DK", name="x")
        self.assertIsNone(dk_listing(other))
        self.assertIn("—", str(badges([], dk=None)))

    def test_shops_then_digikey_and_digikey_only(self):
        html = str(badges(self.p.shop_listings.all(), dk=dk_listing(self.p)))
        self.assertLess(html.index("webshop"), html.index("DigiKey"))
        ShopListing.objects.all().delete()
        only = str(badges([], dk=dk_listing(self.p)))
        self.assertIn("DigiKey", only)
        self.assertNotIn("—", only)                         # лише на DigiKey — не «ніде»

    def test_pages(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        url = reverse("admin:bots_digikeylisting_change", args=[self.dk.pk])
        for page in ("/admin/inventory/product/", "/admin/inventory/reorderproxy/supply/",
                     reverse("admin:shop_shopproduct_changelist")):
            self.assertContains(self.client.get(page), url, msg_prefix=page)
