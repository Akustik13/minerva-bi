"""Підказки в пошуку сайту (shop/search_assist.py, GET /api/v1/shop/search/assist/): журнал запитів, «часто купують
разом», AI-підказки (модель підмінено, без мережі). python manage.py test shop --settings=tabele.settings_test"""
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from api.models import APIKey
from inventory.models import Product
from sales.models import SalesOrder, SalesOrderLine

from . import search_assist
from .models import Shop, ShopListing, ShopSearchQuery

URL = "/api/v1/shop/search/assist/"


def fake_response(tips, usage=(5000, 60, 4800, 0), stop="end_turn"):
    text = json.dumps({"tips": tips})
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop, model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1],
                                                 cache_read_input_tokens=usage[2], cache_creation_input_tokens=usage[3]))


class SearchAssistTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.shop = Shop.objects.create(name="Web", slug="webshop", search_assist=Shop.SEARCH_RULES)
        self.p = {}
        for sku, name in [("AN-868", "Antenne 868 MHz MHF1"), ("CA-MHF1", "Kabel MHF1 auf SMA"), ("AN-GNSS", "GNSS-Antenne"),
                          ("HIDDEN", "Versteckt")]:
            self.p[sku] = Product.objects.create(sku=sku, name=name, sale_price=Decimal("5"),
                                                 tech_attributes={"Frequency Range": "863-870 MHz", "hts": "8529"})
            ShopListing.objects.create(shop=self.shop, product=self.p[sku], is_visible=sku != "HIDDEN")
        key = APIKey.objects.create(name="site", shop=self.shop, scopes=["products:read"])
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")

    def order(self, number, *skus):
        o = SalesOrder.objects.create(source="manual", order_number=number, client="X")
        for sku in skus:
            SalesOrderLine.objects.create(order=o, product=self.p[sku], qty=1)

    def test_config_log_and_together(self):
        Shop.objects.filter(pk=self.shop.pk).update(search_assist=Shop.SEARCH_OFF)
        self.assertEqual(self.c.get(URL, {"config": "1"}).json(), {"mode": "off"})
        Shop.objects.filter(pk=self.shop.pk).update(search_assist=Shop.SEARCH_RULES)
        self.order("A", "AN-868", "CA-MHF1")
        self.order("B", "AN-868", "CA-MHF1", "HIDDEN")
        self.order("C", "AN-868", "AN-GNSS")
        d = self.c.get(URL, {"q": "  Antenne  868 ", "n": "0", "skus": "AN-868"}).json()
        self.assertEqual(d["mode"], "rules")
        self.assertEqual(d["together"], [{"sku": "CA-MHF1", "orders": 2}])   # прихований і разовий — ні
        self.assertNotIn("ai", d)
        self.c.get(URL, {"q": "antenne 868", "n": "3", "skus": ""})
        row = ShopSearchQuery.objects.get(shop=self.shop, query="antenne 868")
        self.assertEqual((row.searches, row.zero_results, row.last_results), (2, 1, 3))
        other = APIClient()
        other.credentials(HTTP_AUTHORIZATION="Token " + APIKey.objects.create(name="rag", scopes=["products:read"]).key)
        self.assertEqual(other.get(URL, {"config": "1"}).status_code, 403)   # лише ключ, прив'язаний до магазину

    @mock.patch("shop.search_assist._call")
    def test_ai_tips_validated_cached_limited(self, call):
        Shop.objects.filter(pk=self.shop.pk).update(search_assist=Shop.SEARCH_AI, search_ai_daily_limit=2)
        call.return_value = fake_response([{"sku": "CA-MHF1", "reason": "Passendes Kabel für MHF1."},
                                           {"sku": "AN-868", "reason": "schon in den Treffern"},
                                           {"sku": "NOPE-1", "reason": "erfunden"},
                                           {"sku": "HIDDEN", "reason": "versteckt"}])
        d = self.c.get(URL, {"q": "868 mhz", "n": "1", "skus": "AN-868", "ai": "1", "lang": "de"}).json()
        self.assertEqual((d["ai"]["status"], d["ai"]["cached"]), ("ok", False))
        self.assertEqual(d["ai"]["tips"], [{"sku": "CA-MHF1", "reason": "Passendes Kabel für MHF1."}])
        system = call.call_args[0][1]
        self.assertIn("CA-MHF1 | Kabel MHF1 auf SMA", system)
        self.assertNotIn("hts", system)            # службові коди DigiKey — ні
        self.assertNotIn("HIDDEN", system)
        again = self.c.get(URL, {"q": "868 MHz", "n": "1", "skus": "AN-868", "ai": "1", "lang": "de", "log": "0"}).json()
        self.assertTrue(again["ai"]["cached"])
        self.assertEqual(call.call_count, 1)       # з кешу — без токенів
        self.shop.refresh_from_db()
        u = self.shop.search_ai_usage
        self.assertEqual((u["calls"], u["cached"], u["input"], u["cache_read"], u["output"]), (1, 1, 5000, 4800, 60))
        self.c.get(URL, {"q": "gnss", "n": "1", "skus": "AN-GNSS", "ai": "1"})
        limited = self.c.get(URL, {"q": "kabel sma", "n": "1", "skus": "", "ai": "1"}).json()
        self.assertEqual(limited["ai"]["status"], "limit")   # денний ліміт — лише правила
        self.assertEqual(ShopSearchQuery.objects.get(query="868 mhz").searches, 1)   # log=0 — не рахується

    @mock.patch("shop.search_assist._call", side_effect=RuntimeError("no_api_key"))
    def test_ai_failure_falls_back(self, call):
        Shop.objects.filter(pk=self.shop.pk).update(search_assist=Shop.SEARCH_AI)
        d = self.c.get(URL, {"q": "868", "n": "0", "ai": "1"}).json()
        self.assertEqual(d["ai"], {"status": "unavailable", "tips": []})
        self.c.get(URL, {"q": "gnss", "n": "0", "ai": "1"})
        self.assertEqual(call.call_count, 1)       # 5 хв без звернень після збою
        Shop.objects.filter(pk=self.shop.pk).update(search_assist=Shop.SEARCH_RULES)
        self.assertEqual(self.c.get(URL, {"q": "x y z", "n": "0", "ai": "1"}).json()["ai"]["status"], "off")

    def test_card_and_report(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        search_assist.record(self.shop, "rp-sma 2,4 ghz", 0)
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        page = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        self.assertContains(page, "🔎 Пошук на сайті")
        self.assertContains(page, "«rp-sma 2,4 ghz» ×1")
        report = self.client.get("/admin/shop/shopsearchquery/?zero=1")
        self.assertContains(report, "rp-sma 2,4 ghz")
