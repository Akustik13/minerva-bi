"""Списки й картки адмінки, що форматують числа: «Лог змін цін» DigiKey, митні артикули відправлення.

python manage.py test bots.tests_admin_lists --settings=tabele.settings_test
"""
from django.contrib.auth.models import User
from django.test import TestCase

from bots.models import DigiKeyListing, DigiKeyPriceLog
from inventory.models import Product


class AdminListsTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))

    def test_price_log_list_and_card(self):
        listing = DigiKeyListing.objects.create(product=Product.objects.create(sku="A1"), dk_title="t")
        log = DigiKeyPriceLog.objects.create(listing=listing, delta_pct=-3.25, old_prices=[{"qty": 1, "price": "1.5"}],
                                             new_prices=[{"qty": 1, "price": "bad"}])
        r = self.client.get("/admin/bots/digikeypricelog/")
        self.assertEqual(r.status_code, 200)                       # раніше 500: «{:.1f}» у format_html
        self.assertContains(r, "-3.2%")
        self.assertEqual(self.client.get(f"/admin/bots/digikeypricelog/{log.pk}/change/").status_code, 200)

    def test_customs_articles_panel_escapes_and_sums(self):
        from django.contrib import admin
        from shipping.models import Shipment
        panel = admin.site._registry[Shipment].customs_articles_panel
        html = str(panel(Shipment(customs_articles={"type": "commercial", "customs_line_items": [
            {"description": "<b>Filter</b>", "quantity": 2, "value": "10.5", "currency": "EUR"},
            {"description": "Cable", "quantity": 1, "value": 4}]})))
        self.assertIn("Всього: 14.50", html)
        self.assertIn("&lt;b&gt;Filter&lt;/b&gt;", html)
        self.assertNotIn("<b>Filter</b>", html)
