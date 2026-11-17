"""💲 Ціни DigiKey з редактора базових цін і «немає курсу» (bots/admin_prices.py: fx_fix, fx_use_view, editor_rows,
editor_config, push_products; inventory/admin_prices.py: _push_digikey). DigiKey і ЄЦБ імітуються — без мережі.

    python manage.py test bots.tests_price_editor --settings=tabele.settings_test
"""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User

from bots.models import DigiKeyPriceRun, DigiKeyPriceSettings
from bots.services import dk_prices as svc
from bots.tests_price_fx import ECB_XML, _clear_fx_cache
from bots.tests_prices import PriceBase
from core import fx_online
from inventory.models import Product

D = Decimal
EDITOR = "/admin/inventory/product/base-prices/"
COMPARE = "/admin/bots/digikeypricerun/compare/"
FX_USE = "/admin/bots/digikeypricesettings/fx-use/"


class Base(PriceBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))
        _clear_fx_cache()
        self.addCleanup(_clear_fx_cache)
        self.s.currency = "USD"                                    # акаунт продавця DigiKey — у доларах
        self.s.save()

    def no_usd(self):
        from shop.models import ShopSettings
        ss = ShopSettings.get()
        ss.fx_rates = {}
        ss.save()

    def post_editor(self, products, payload):
        ids = ",".join(str(p.pk) for p in products)
        return self.client.post(f"{EDITOR}?ids={ids}", {"ids": ids, "payload": json.dumps(payload)})


class NoRateTests(Base):
    def test_row_says_where_the_rate_comes_from(self):
        self.no_usd()
        l = self.listing(self.product("NR-1", [(1, 10)]), "nr1", [(1, 12)])
        row = svc.row_for(l, svc.Rules.from_settings(), "USD")
        self.assertEqual(row.state, "no_rate")
        self.assertIn("немає курсу EUR → USD у «Курсах валют» Minerva", row.state_text)
        self.assertIn("онлайн ЄЦБ", row.state_text)
        self.s.fx_source = DigiKeyPriceSettings.FX_ONLINE
        self.s.save()
        with mock.patch.object(fx_online, "_download", side_effect=OSError("offline")):
            self.assertIn("онлайн-курс ЄЦБ ще не отримано", svc.row_for(l, svc.Rules.from_settings(), "USD").state_text)

    def test_compare_page_offers_the_ecb_rate(self):
        self.no_usd()
        self.listing(self.product("NR-2", [(1, 10)]), "nr2", [(1, 12)])
        page = self.client.get(COMPARE)
        self.assertContains(page, "Ціни для DigiKey не рахуються")
        self.assertContains(page, 'id="dkfx-form"')
        self.assertContains(page, "Брати онлайн-курс ЄЦБ")

    def test_no_banner_when_rate_is_known(self):
        self.listing(self.product("NR-3", [(1, 10)]), "nr3", [(1, 12)])
        self.assertNotContains(self.client.get(COMPARE), "Ціни для DigiKey не рахуються")

    def test_fx_use_switches_to_ecb_and_goes_back(self):
        self.assertEqual(self.client.get(FX_USE).status_code, 403)          # лише POST
        with mock.patch.object(fx_online, "_download", return_value=ECB_XML):
            r = self.client.post(FX_USE, {"source": "online", "next": COMPARE + "?products=1"})
        self.assertRedirects(r, COMPARE + "?products=1", fetch_redirect_response=False)
        s = DigiKeyPriceSettings.get()
        self.assertEqual(s.fx_source, DigiKeyPriceSettings.FX_ONLINE)
        self.assertEqual(svc.fx("EUR", "USD", s), D("1.1"))

    def test_fx_use_never_redirects_outside(self):
        with mock.patch.object(fx_online, "_download", return_value=ECB_XML):
            r = self.client.post(FX_USE, {"source": "online", "next": "https://evil.example/x"})
        self.assertTrue(r["Location"].startswith(f"/admin/bots/digikeypricesettings/{self.s.pk}/change/"))

    def test_fx_use_needs_the_settings_right(self):
        with mock.patch("bots.admin_prices.DigiKeyPriceSettingsAdmin.has_change_permission", return_value=False):
            self.assertEqual(self.client.post(FX_USE, {"source": "online"}).status_code, 403)
        self.assertEqual(DigiKeyPriceSettings.get().fx_source, DigiKeyPriceSettings.FX_RATES)


class EditorTests(Base):
    def test_block_only_with_a_listing(self):
        p = self.product("ED-0", [(1, 10)])
        page = self.client.get(f"{EDITOR}?ids={p.pk}")
        self.assertNotContains(page, "Ціни на DigiKey з цих базових цін")
        self.listing(p, "ed0", [(1, 12)])
        page = self.client.get(f"{EDITOR}?ids={p.pk}")
        self.assertContains(page, "Ціни на DigiKey з цих базових цін")
        self.assertContains(page, "1 EUR = 1,1111 USD")                    # «Курси валют»: 1 USD = 0,9 EUR
        item = page.context["items"][0]
        self.assertEqual(item["dk"]["now"], "12.00 USD від 1 шт.")
        self.assertTrue(item["dk"]["sendable"])

    def test_editor_shows_the_rate_fix(self):
        self.no_usd()
        p = self.product("ED-1", [(1, 10)])
        self.listing(p, "ed1", [(1, 12)])
        page = self.client.get(f"{EDITOR}?ids={p.pk}")
        self.assertContains(page, "немає курсу EUR → USD")
        self.assertContains(page, 'id="dkfx-form"')

    def test_save_and_push_new_prices(self):
        p = self.product("ED-2", [(1, 9)])
        q = self.product("ED-3", [(1, 9)])                                  # не позначено — не надсилається
        self.listing(p, "ed2", [(1, 10)])
        self.listing(q, "ed3", [(1, 10)])
        r = self.post_editor([p, q], {"products": [{"id": p.pk, "currency": "EUR",
                                                    "rows": [{"min_qty": 1, "unit_price": "18"}]}],
                                      "digikey": {"mode": "push", "ids": [p.pk]}})
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.base_prices[0]["unit_price"], "18")
        run = DigiKeyPriceRun.objects.get()
        self.assertEqual([l.sku for l in run.lines.all()], ["ED-2"])
        self.assertEqual(self.dk.puts[0][0], "ed2")
        self.assertEqual(self.dk.puts[0][1]["prices"], [{"quantityBreak": 1, "price": 20.0}])   # 18 EUR ÷ 0,9

    def test_compare_mode_goes_to_the_compare_page(self):
        p = self.product("ED-4", [(1, 9)])
        self.listing(p, "ed4", [(1, 10)])
        r = self.post_editor([p], {"digikey": {"mode": "compare", "ids": [p.pk]}})
        self.assertRedirects(r, f"{COMPARE}?products={p.pk}", fetch_redirect_response=False)
        self.assertFalse(DigiKeyPriceRun.objects.exists())

    def test_failed_prices_are_not_pushed(self):
        p = self.product("ED-5", [(1, 9)])
        self.listing(p, "ed5", [(1, 10)])
        self.post_editor([p], {"products": [{"id": p.pk, "currency": "EUR", "rows": [{"min_qty": 5, "unit_price": "18"}]}],
                               "digikey": {"mode": "push", "ids": [p.pk]}})
        self.assertFalse(DigiKeyPriceRun.objects.exists())
        self.assertEqual(self.dk.puts, [])

    def test_push_needs_the_listing_right(self):
        p = self.product("ED-6", [(1, 18)])
        self.listing(p, "ed6", [(1, 10)])
        with mock.patch("bots.admin_prices._can_change", return_value=False):
            self.post_editor([p], {"digikey": {"mode": "push", "ids": [p.pk]}})
        self.assertFalse(DigiKeyPriceRun.objects.exists())

    def test_same_prices_nothing_sent(self):
        p = self.product("ED-7", [(1, 9)])
        self.listing(p, "ed7", [(1, 10)])                                    # 9 EUR ÷ 0,9 = 10 USD — уже так
        r = self.post_editor([p], {"digikey": {"mode": "push", "ids": [p.pk]}})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(DigiKeyPriceRun.objects.exists())

    def test_foreign_ids_ignored(self):
        p = self.product("ED-8", [(1, 18)])
        other = self.product("ED-9", [(1, 18)])
        self.listing(p, "ed8", [(1, 10)])
        self.listing(other, "ed9", [(1, 10)])
        self.post_editor([p], {"digikey": {"mode": "push", "ids": [p.pk, other.pk]}})
        self.assertEqual([o for o, _ in self.dk.puts], ["ed8"])
        self.assertTrue(Product.objects.filter(pk=other.pk).exists())
