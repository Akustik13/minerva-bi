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
        self.assertIn("Переведено з USD в EUR за курсом 1 USD = 0.86 EUR", h.note)
        self.assertEqual((h.details["old_currency"], h.details["rate"], h.details["rate_from"], h.details["rate_to"]),
                         ("USD", "0.86", "USD", "EUR"))
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
        self.assertIn("Зчитано з DigiKey у USD · переведено в EUR за курсом 1 USD = 0.86 EUR", p.base_prices_source)
        h = ProductPriceHistory.objects.get(product=p)
        self.assertEqual(h.details["read"], {"from": "DigiKey", "currency": "USD", "prices": [
            {"min_qty": 1, "unit_price": "10"}, {"min_qty": 10, "unit_price": "9.5"}]})

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


class CardTests(Base):
    """Картка товару: валюта Minerva, курс як для DigiKey, переведення одного товару."""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))
        self.usd = self.product("U-1", [(1, 279)])
        self.eur = self.product("E-1", [(1, 10)], "EUR")

    def config(self, product):
        import json
        import re
        html = self.client.get(f"/admin/inventory/product/{product.pk}/change/").content.decode()
        return json.loads(re.search(r'<script id="bp-config" type="application/json">(.*?)</script>', html, re.S).group(1))

    def test_usd_product_gets_digikey_rate_and_convert_link(self):
        cfg = self.config(self.usd)
        self.assertEqual(cfg["minerva"], "EUR")
        self.assertEqual(cfg["to_minerva"], {"USD": 0.86})            # як dk_prices.fx — ціни DigiKey не зрушать
        self.assertEqual(cfg["convert_url"], f"/admin/inventory/product/base-currency/?ids={self.usd.pk}")
        self.assertEqual(cfg["dk"]["currency"], "USD")
        self.assertAlmostEqual(cfg["dk"]["rate"], 1 / 0.86, places=6)

    def test_rate_follows_digikey_manual_rate(self):
        s = DigiKeyPriceSettings.get()
        s.fx_source, s.fx_manual_rate = DigiKeyPriceSettings.FX_MANUAL, D("1.25")
        s.save()
        cfg = self.config(self.eur)
        self.assertEqual((cfg["dk"]["rate"], cfg["dk"]["source"]), (1.25, "свій курс для DigiKey"))


ECB_XML = """<gesmes:Envelope><Cube><Cube time='2026-10-30'>
<Cube currency='USD' rate='1.1172'/><Cube currency='GBP' rate='0.8650'/></Cube></Cube></gesmes:Envelope>"""


def _clear_fx_cache():
    from core import fx_online
    from core.shared_cache import cache
    cache.delete(fx_online.CACHE_KEY)
    cache.delete(fx_online.FAIL_KEY)


class HistoryTests(Base):
    """Історія базових цін: що в що перевели, за яким курсом, що зчитано з DigiKey і в якій валюті."""

    def line(self, item, label):
        return next(ln for ln in item["lines"] if ln["label"] == label)

    def test_convert_shows_from_to_and_rate(self):
        p = self.product("H-1", [(1, "2.69"), (10, "2.587")])
        bp.convert(p, "EUR", D("0.86"), origin="ЄЦБ на 30.10.2026")
        item = bp.history(p)[0]
        self.assertEqual((item["icon"], item["title"]), ("💱", "Переведено з USD в EUR"))
        self.assertEqual(item["rate"], "1 USD = 0.86 EUR · 1 EUR = 1.1628 USD · курс: ЄЦБ на 30.10.2026")
        self.assertEqual(item["qtys"], [1, 10])
        self.assertEqual([c["v"] for c in self.line(item, "Було, USD")["cells"]], ["2.69", "2.587"])
        self.assertEqual([c["v"] for c in self.line(item, "Стало, EUR")["cells"]], ["2.3134", "2.2248"])
        self.assertEqual(item["note"], "")                       # усе вже в заголовку й курсі
        p.refresh_from_db()
        self.assertIn("(ЄЦБ на 30.10.2026)", p.base_prices_source)

    def test_digikey_read_in_usd_then_converted(self):
        p = self.product("H-2", currency="EUR")
        DigiKeyListing.objects.create(product=p, dk_offer_id="o9", sync_status="published",
                                      dk_prices=[{"qty": 1, "price": 10}, {"qty": 10, "price": "9.5"}])
        bp.import_from_digikey(p)
        item = bp.history(p)[0]
        self.assertEqual(item["title"], "Зчитано з DigiKey у USD → переведено в EUR")
        self.assertEqual([c["v"] for c in self.line(item, "DigiKey, USD")["cells"]], ["10", "9.5"])
        self.assertEqual([c["v"] for c in self.line(item, "Стало, EUR")["cells"]], ["8.6", "8.17"])
        self.assertFalse(any(ln["label"].startswith("Було") for ln in item["lines"]))   # раніше цін не було
        self.assertIn("офер o9", item["note"])

    def test_old_records_from_notes(self):
        """Записи до 11.2026 (без подробиць): валюту й курс видно з примітки та з попереднього запису."""
        p = self.product("H-3", [(1, "2.3134")], "EUR")
        rows_usd, rows_eur = [{"min_qty": 1, "unit_price": "2.69"}], [{"min_qty": 1, "unit_price": "2.3134"}]
        ProductPriceHistory.objects.create(
            product=p, source="migration", currency="USD", old_prices=[], new_prices=rows_usd, user_label="система",
            note="Перенесено з цін DigiKey при оновленні (DigiKey · офер 26e9 · синхр. 30.06.2026 17:57)")
        ProductPriceHistory.objects.create(
            product=p, source="manual", currency="EUR", old_prices=rows_usd, new_prices=rows_eur, user_label="admin",
            note="Переведено з USD в EUR за курсом 0.86")
        new, old = bp.history(p)
        self.assertEqual((new["title"], new["rate"]), ("Переведено з USD в EUR", "1 USD = 0.86 EUR · 1 EUR = 1.1628 USD"))
        self.assertEqual([ln["label"] for ln in new["lines"]], ["Було, USD", "Стало, EUR"])
        self.assertEqual((old["icon"], old["title"]), ("📥", "Зчитано з DigiKey у USD"))
        self.assertEqual([ln["label"] for ln in old["lines"]], ["Стало, USD"])
        self.assertEqual(old["note"], "перенесено під час оновлення Minerva · офер 26e9 · синхр. 30.06.2026 17:57")

    def test_manual_change_marks_changed_tiers(self):
        p = self.product("H-4", [(1, 10), (10, 9)], "EUR")
        bp.set_base_prices(p, [{"min_qty": 1, "unit_price": "10"}, {"min_qty": 10, "unit_price": "8.5"}],
                           note="знижка")
        item = bp.history(p)[0]
        self.assertEqual((item["icon"], item["title"], item["note"]), ("✏️", "Змінено вручну", "знижка"))
        self.assertEqual([c["changed"] for c in self.line(item, "Стало, EUR")["cells"]], [False, True])

    def test_card_converter_mark_goes_to_history(self):
        p = self.product("H-5", [(1, "2.69")])
        fx = bp.parse_fx("USD>EUR:0.895095:ЄЦБ на 30.10.2026")
        self.assertEqual(fx, {"rate": "0.895095", "rate_from": "USD", "rate_to": "EUR", "rate_origin": "ЄЦБ на 30.10.2026"})
        self.assertIsNone(bp.parse_fx("USD>EUR:abc"))
        self.assertIsNone(bp.parse_fx("<script>"))
        bp.set_base_prices(p, [{"min_qty": 1, "unit_price": "2.4078"}], currency="EUR", details=fx)
        p.refresh_from_db()
        self.assertEqual(p.base_prices_source, "Переведено з USD в EUR за курсом 1 USD = 0.895095 EUR (ЄЦБ на 30.10.2026)")
        self.assertEqual(bp.history(p)[0]["title"], "Переведено з USD в EUR")
        q = self.product("H-6", [(1, "2.69")])                  # позначка в іншу валюту — курс не пишемо
        bp.set_base_prices(q, [{"min_qty": 1, "unit_price": "2.5"}], currency="GBP", details=fx)
        h = ProductPriceHistory.objects.get(product=q)
        self.assertNotIn("rate", h.details)
        self.assertEqual(bp.history(q)[0]["title"], "Змінено вручну · валюта USD → GBP без перерахунку цін")

    def test_card_shows_history_table(self):
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))
        p = self.product("H-7", [(1, "2.69")])
        bp.convert(p, "EUR", D("0.86"))
        html = self.client.get(f"/admin/inventory/product/{p.pk}/change/").content.decode()
        self.assertIn("💱 Переведено з USD в EUR", html)
        self.assertIn("Було, USD", html)
        self.assertIn("Стало, EUR", html)
        self.assertIn('name="base_fx"', html)


class OnlineRatesTests(Base):
    """🌐 Курси ЄЦБ для сторінок: картка товару, «Курси валют», переведення."""

    def setUp(self):
        super().setUp()
        _clear_fx_cache()
        self.addCleanup(_clear_fx_cache)
        self.root = User.objects.create_superuser("root", "r@x.y", "p")

    def test_json_for_staff_only(self):
        from unittest import mock
        from core import fx_online
        self.assertEqual(self.client.get("/core/fx-online/").status_code, 302)       # без входу
        self.client.force_login(self.root)
        with mock.patch.object(fx_online, "_download", return_value=ECB_XML):
            j = self.client.get("/core/fx-online/").json()
        self.assertEqual((j["ok"], j["date_text"], j["rates"]["USD"]), (True, "30.10.2026", "1.1172"))
        _clear_fx_cache()
        with mock.patch.object(fx_online, "_download", side_effect=OSError("offline")):
            j = self.client.get("/core/fx-online/").json()
        self.assertFalse(j["ok"])
        self.assertIn("недоступні", j["error"])

    def test_card_config_has_online_url_and_digikey_settings(self):
        p = self.product("O-1", [(1, 10)], "EUR")
        from inventory.admin_currency import card_config
        cfg = card_config(p.pk)
        self.assertEqual(cfg["fx_online_url"], "/core/fx-online/")
        self.assertTrue(cfg["dk"]["settings_url"].startswith("/admin/bots/digikeypricesettings/"))
        self.assertFalse(cfg["dk"]["online"])

    def test_conversion_page_offers_ecb_and_records_origin(self):
        from unittest import mock
        from core import fx_online
        self.client.force_login(self.root)
        p = self.product("O-2", [(1, 279)])
        url = "/admin/inventory/product/base-currency/"
        with mock.patch.object(fx_online, "_download", return_value=ECB_XML):
            page = self.client.get(url, {"ids": p.pk})
        self.assertContains(page, 'data-origin="ЄЦБ на 30.10.2026"')
        self.assertContains(page, "1 EUR = 1.162791 USD")
        self.client.post(url, {"ids": str(p.pk), "rate_USD": "0.895095", "origin_USD": "ЄЦБ на 30.10.2026",
                               "action": "convert", "confirm": "1"})
        h = ProductPriceHistory.objects.get(product=p)
        self.assertEqual((h.details["rate"], h.details["rate_origin"]), ("0.895095", "ЄЦБ на 30.10.2026"))
        q = self.product("O-3", [(1, 279)])
        self.client.post(url, {"ids": str(q.pk), "rate_USD": "0.86", "action": "convert", "confirm": "1"})
        h = ProductPriceHistory.objects.get(product=q)
        self.assertEqual(h.details["rate_origin"], "як для DigiKey: «Курси валют» Minerva")

    def test_shop_rates_page_has_ecb_button(self):
        self.client.force_login(self.root)
        from shop.models import ShopSettings
        html = self.client.get(f"/admin/shop/shopsettings/{ShopSettings.get().pk}/change/").content.decode()
        self.assertIn("🌐 Підставити курси ЄЦБ", html)
        self.assertIn("/core/fx-online/", html)
