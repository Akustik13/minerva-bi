"""💱 Базові ціни у валюті Minerva (EUR): переведення з історією, фільтр, сторінка й дія, імпорт з DigiKey.

    python manage.py test inventory.tests_base_currency --settings=tabele.settings_test
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from bots.models import DigiKeyListing, DigiKeyPriceSettings
from bots.services import dk_prices
from inventory.models import Product, ProductPriceHistory
from inventory.services import base_prices as bp

D = Decimal


class Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        from shop.models import ShopSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        ss = ShopSettings.get()
        ss.fx_rates = {"USD": "0.86"}
        ss.save()
        s = DigiKeyPriceSettings.get()
        s.currency = "USD"
        s.save()

    def product(self, sku, prices=(), currency="USD", **kw):
        return Product.objects.create(sku=sku, name=f"Name {sku}", base_price_currency=currency, base_prices=[
            {"min_qty": q, "unit_price": str(p)} for q, p in prices], **kw)


class ConvertTests(Base):
    def test_convert_with_history(self):
        p = self.product("AN-1", [(1, 279), (10, "268.39"), (1000, "0.0452")])
        self.assertEqual(bp.rate_to_home("USD"), D("0.86"))
        self.assertTrue(bp.convert(p, "EUR", bp.rate_to_home("USD")))
        p.refresh_from_db()
        self.assertEqual(p.base_price_currency, "EUR")
        self.assertEqual([r["unit_price"] for r in p.base_prices], ["239.94", "230.8154", "0.0389"])
        h = ProductPriceHistory.objects.get(product=p)
        self.assertEqual((h.currency, h.old_prices[0]["unit_price"]), ("EUR", "279"))
        self.assertIn("Переведено з USD в EUR за курсом 0.86", h.note)
        self.assertIn("Переведено з USD в EUR", p.base_prices_source)
        self.assertFalse(bp.convert(p, "EUR", D("0.86")))           # уже в EUR
        q = self.product("AN-4", [(10, "268.39")])
        bp.convert(q, "EUR", D("0.86"), cents=True)
        q.refresh_from_db()
        self.assertEqual(q.base_prices[0]["unit_price"], "230.82")
        self.assertIn("до центів", q.base_prices_source)
        self.assertFalse(bp.convert(self.product("NO-1"), "EUR", D("0.86")))   # без цін
        with self.assertRaises(ValueError):
            bp.convert(self.product("AN-2", [(1, 5)]), "EUR", D("0"))

    def test_digikey_prices_do_not_move(self):
        """Курс за замовчуванням — той самий, що для DigiKey: після переведення ціни на DigiKey ті самі."""
        p = self.product("AN-3", [(1, 279), (10, "268.39")])
        listing = DigiKeyListing.objects.create(product=p, dk_offer_id="o1", sync_status="published",
                                                dk_prices=[{"qty": 1, "price": 279}, {"qty": 10, "price": 268.39}])
        rules = dk_prices.Rules.from_settings()
        before = dk_prices.row_for(listing, rules, "USD").new
        bp.convert(p, "EUR", bp.rate_to_home("USD"))
        listing.product.refresh_from_db()
        self.assertEqual(dk_prices.row_for(listing, rules, "USD").new, before)
        self.assertEqual(before, [(1, D("279.00")), (10, D("268.39"))])

    def test_rate_follows_digikey_rate_setting(self):
        s = DigiKeyPriceSettings.get()
        s.fx_source, s.fx_manual_rate = DigiKeyPriceSettings.FX_MANUAL, D("1.25")
        s.save()
        self.assertEqual(bp.rate_to_home("USD"), D("0.8"))


class ImportTests(Base):
    def test_digikey_prices_land_in_eur(self):
        p = self.product("DK-1", currency="EUR")
        DigiKeyListing.objects.create(product=p, dk_offer_id="o2", sync_status="published",
                                      dk_prices=[{"qty": 1, "price": 10}, {"qty": 10, "price": "9.5"}])
        self.assertTrue(bp.import_from_digikey(p))
        p.refresh_from_db()
        self.assertEqual(p.base_price_currency, "EUR")
        self.assertEqual([r["unit_price"] for r in p.base_prices], ["8.6", "8.17"])
        self.assertIn("USD → EUR за курсом 0.86", p.base_prices_source)

    def test_without_rate_keeps_digikey_currency(self):
        s = DigiKeyPriceSettings.get()
        s.currency = "GBP"
        s.save()
        p = self.product("DK-2", currency="EUR")
        DigiKeyListing.objects.create(product=p, dk_offer_id="o3", sync_status="published",
                                      dk_prices=[{"qty": 1, "price": 10}])
        self.assertTrue(bp.import_from_digikey(p))
        p.refresh_from_db()
        self.assertEqual((p.base_price_currency, p.base_prices[0]["unit_price"]), ("GBP", "10"))
        self.assertIn("немає курсу GBP → EUR", p.base_prices_source)


class PageTests(Base):
    def setUp(self):
        super().setUp()
        self.root = User.objects.create_superuser("root", "r@x.y", "p")
        self.client.force_login(self.root)
        self.usd = self.product("U-1", [(1, 279)])
        self.eur = self.product("E-1", [(1, 10)], "EUR")
        self.empty = self.product("N-1", currency="EUR")
        self.url = "/admin/inventory/product/base-currency/"

    def test_filter(self):
        def skus(value):
            r = self.client.get("/admin/inventory/product/", {"base_cur": value})
            return sorted(o.sku for o in r.context["cl"].result_list)
        self.assertEqual(skus("not_home"), ["U-1"])
        self.assertEqual(skus("EUR"), ["E-1"])
        self.assertEqual(skus("none"), ["N-1"])

    def test_action_page_preview_and_convert(self):
        r = self.client.post("/admin/inventory/product/", {"action": "action_base_to_home",
                                                           "_selected_action": [self.usd.pk, self.eur.pk]})
        self.assertEqual(r.status_code, 302)
        ids = r["Location"].replace("%2C", ",").split("ids=")[1]
        self.assertEqual(sorted(ids.split(",")), sorted([str(self.usd.pk), str(self.eur.pk)]))
        page = self.client.get(self.url, {"all": 1})
        self.assertContains(page, 'name="rate_USD" value="0.86"')
        self.assertContains(page, "239.94 EUR")
        self.assertNotContains(page, "E-1")                         # уже в EUR — не в переліку
        preview = self.client.post(self.url, {"ids": "", "rate_USD": "0,9", "action": "preview"})
        self.assertContains(preview, "251.1 EUR")
        self.usd.refresh_from_db()
        self.assertEqual(self.usd.base_price_currency, "USD")       # перегляд нічого не змінює
        self.client.post(self.url, {"ids": "", "rate_USD": "0.9", "action": "convert"})   # без галочки
        self.usd.refresh_from_db()
        self.assertEqual(self.usd.base_price_currency, "USD")
        r = self.client.post(self.url, {"ids": "", "rate_USD": "0.9", "action": "convert", "confirm": "1"})
        self.assertEqual(r.status_code, 302)
        self.usd.refresh_from_db()
        self.assertEqual((self.usd.base_price_currency, self.usd.base_prices[0]["unit_price"]), ("EUR", "251.1"))
        self.assertContains(self.client.get(self.url, {"all": 1}), "вже в EUR")

    def test_needs_change_permission(self):
        viewer = User.objects.create_user("vera", "v@x.y", "p", is_staff=True)
        self.client.force_login(viewer)
        r = self.client.get(self.url, {"all": 1})
        self.assertIn(r.status_code, (302, 403))
        self.usd.refresh_from_db()
        self.assertEqual(self.usd.base_price_currency, "USD")
