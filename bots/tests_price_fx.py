"""💱 Курс для цін DigiKey: «Курси валют» Minerva / свій / онлайн ЄЦБ (bots/services/dk_prices.fx, core/fx_online.py),
поріг дрібних змін автооновлення. Мережі немає: ЄЦБ імітується.

    python manage.py test bots.tests_price_fx --settings=tabele.settings_test
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from bots.models import DigiKeyPriceRun, DigiKeyPriceSettings
from bots.services import dk_prices as svc
from bots.tests_prices import PriceBase
from core import fx_online

D = Decimal

ECB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <Cube><Cube time='2026-10-30'>
    <Cube currency='USD' rate='1.1000'/>
    <Cube currency='GBP' rate='0.8500'/>
  </Cube></Cube>
</gesmes:Envelope>"""


def _clear_fx_cache():
    from core.shared_cache import cache
    cache.delete(fx_online.CACHE_KEY)
    cache.delete(fx_online.FAIL_KEY)


class EcbParseTests(TestCase):
    def test_parse_and_cross(self):
        data = fx_online.parse(ECB_XML)
        self.assertEqual(data["date"], "2026-10-30")
        self.assertEqual(data["rates"]["USD"], "1.1")
        self.assertEqual(fx_online.cross(data, "EUR", "USD"), D("1.1"))
        self.assertEqual(fx_online.cross(data, "USD", "GBP"), D("0.85") / D("1.1"))
        self.assertIsNone(fx_online.cross(data, "EUR", "XYZ"))
        self.assertIsNone(fx_online.parse("<html>Wartung</html>"))

    def test_failure_is_remembered_briefly(self):
        _clear_fx_cache()
        with mock.patch.object(fx_online, "_download", side_effect=OSError("offline")) as dl:
            self.assertIsNone(fx_online.latest())
            self.assertIsNone(fx_online.latest())
        self.assertEqual(dl.call_count, 1)                         # друга спроба — лише після паузи
        with mock.patch.object(fx_online, "_download", return_value=ECB_XML):
            self.assertEqual(fx_online.latest(force=True)["date"], "2026-10-30")
        _clear_fx_cache()


class DigiKeyRateTests(PriceBase):
    def setUp(self):
        super().setUp()
        _clear_fx_cache()
        self.addCleanup(_clear_fx_cache)
        self.s.currency = "USD"                                     # акаунт продавця DigiKey — у доларах
        self.s.save()

    def test_minerva_rates_as_before(self):
        self.assertEqual(svc.fx("USD", "EUR"), D("0.9"))
        self.assertEqual(svc.rate_source_text(), "«Курси валют» Minerva")

    def test_manual_rate(self):
        self.s.fx_source, self.s.fx_manual_rate = DigiKeyPriceSettings.FX_MANUAL, D("1.25")
        self.s.save()
        self.assertEqual(svc.fx("EUR", "USD"), D("1.25"))
        self.assertEqual(svc.fx("USD", "EUR"), D("0.8"))
        self.assertIsNone(svc.fx("GBP", "USD"))                    # GBP немає в «Курсах валют»
        l = self.listing(self.product("M-1", [(1, 100)]), "m1", [(1, 120)])
        self.assertEqual(svc.row_for(l, svc.Rules.from_settings(), "USD").new, [(1, D("125.00"))])
        self.s.fx_manual_rate = None                               # курс не вписано — як «Курси валют»
        self.s.save()
        self.assertEqual(svc.fx("USD", "EUR"), D("0.9"))

    def test_online_rate_saved_and_used_when_offline(self):
        self.s.fx_source = DigiKeyPriceSettings.FX_ONLINE
        self.s.save()
        with mock.patch.object(fx_online, "_download", return_value=ECB_XML):
            self.assertEqual(svc.fx("EUR", "USD"), D("1.1"))
        self.s.refresh_from_db()
        self.assertEqual(self.s.fx_online["date"], "2026-10-30")
        self.assertIn("30.10.2026", svc.rate_source_text(self.s))
        _clear_fx_cache()
        with mock.patch.object(fx_online, "_download", side_effect=OSError("offline")):
            self.assertEqual(svc.fx("EUR", "USD", DigiKeyPriceSettings.get()), D("1.1"))   # останній збережений
        l = self.listing(self.product("O-1", [(1, 100)]), "o1", [(1, 100)])
        with mock.patch.object(fx_online, "_download", side_effect=OSError("offline")):
            self.assertEqual(svc.row_for(l, svc.Rules.from_settings(), "USD").new, [(1, D("110.00"))])

    def test_online_without_any_rate_is_no_rate(self):
        self.s.fx_source = DigiKeyPriceSettings.FX_ONLINE
        self.s.save()
        l = self.listing(self.product("O-2", [(1, 100)]), "o2", [(1, 100)])
        with mock.patch.object(fx_online, "_download", side_effect=OSError("offline")):
            self.assertEqual(svc.row_for(l, svc.Rules.from_settings(), "USD").state, "no_rate")


class TinyChangeTests(PriceBase):
    def setUp(self):
        super().setUp()
        self.s.auto_mode, self.s.max_change_pct = "apply", D("20")
        self.s.notify_telegram = self.s.notify_email = False
        self.s.save()
        self.listing(self.product("T-1", [(1, "10.05")]), "t1", [(1, 10)])      # +0,5 % — курс трохи змінився
        self.listing(self.product("T-2", [(1, 11)]), "t2", [(1, 10)])           # +10 %

    def test_small_differences_skipped(self):
        self.s.auto_min_change_pct = D("1")
        self.s.save()
        svc.auto()
        run = DigiKeyPriceRun.objects.get()
        self.assertEqual(list(run.lines.values_list("sku", flat=True)), ["T-2"])
        self.assertEqual([p[0] for p in self.dk.puts], ["t2"])

    def test_zero_threshold_sends_everything(self):
        svc.auto()
        self.assertEqual(sorted(DigiKeyPriceRun.objects.get().lines.values_list("sku", flat=True)), ["T-1", "T-2"])


class SettingsPageTests(PriceBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))
        _clear_fx_cache()
        self.addCleanup(_clear_fx_cache)

    def test_rate_now_and_online_refresh(self):
        self.s.currency, self.s.fx_source, self.s.fx_manual_rate = "USD", DigiKeyPriceSettings.FX_MANUAL, D("1.16")
        self.s.save()
        page = self.client.get(f"/admin/bots/digikeypricesettings/{self.s.pk}/change/")
        self.assertContains(page, "1 EUR = 1.16 USD")
        self.assertContains(page, "свій курс для DigiKey")
        self.assertNotContains(page, "&lt;/span&gt;&lt;/div&gt;")        # блок «Сторінки» — HTML, не текст
        DigiKeyPriceSettings.objects.filter(pk=1).update(fx_source=DigiKeyPriceSettings.FX_ONLINE)
        with mock.patch.object(fx_online, "_download", return_value=ECB_XML):
            r = self.client.get("/admin/bots/digikeypricesettings/fx-refresh/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(DigiKeyPriceSettings.get().fx_online["rates"]["USD"], "1.1")

    def test_hint_about_base_prices_not_in_eur(self):
        self.product("U-1", [(1, 10)], "USD")
        page = self.client.get(f"/admin/bots/digikeypricesettings/{self.s.pk}/change/")
        self.assertContains(page, "Базові ціни 1 товарів не у валюті Minerva (EUR)")
        self.assertContains(page, "/admin/inventory/product/base-currency/?all=1")
