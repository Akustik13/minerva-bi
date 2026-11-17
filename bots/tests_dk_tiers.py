"""Цінові тири лістингу DigiKey: ціна за 1 шт. і сума за кількість, «схоже на суму за всю кількість»
(bots/services/dk_prices.minerva_ref / price_suspects, перевірка якості, редактор у картці лістингу).

    python manage.py test bots.tests_dk_tiers --settings=tabele.settings_test
"""
from decimal import Decimal

from django.contrib.auth.models import User

from bots.admin import _local_quality_checks
from bots.services import dk_prices as svc
from bots.tests_prices import PriceBase

D = Decimal


class SuspectTests(PriceBase):
    def setUp(self):
        super().setUp()
        self.s.currency = "USD"                       # «Курси валют»: 1 USD = 0,9 EUR
        self.s.save()

    def test_minerva_ref_in_dk_currency(self):
        l = self.listing(self.product("T-1", [(1, "0.3"), (1000, "0.24")]), "t1", [(1000, 250)])
        ref = svc.minerva_ref(l)
        self.assertEqual([q for q, _ in ref], [1, 1000])
        self.assertAlmostEqual(float(ref[1][1]), 0.24 / 0.9, places=4)

    def test_total_instead_of_unit_price(self):
        l = self.listing(self.product("T-2", [(1, "0.3"), (1000, "0.24")]), "t2", [(1000, 250)])
        texts = svc.price_suspects(l.dk_prices, svc.minerva_ref(l))
        self.assertEqual(len(texts), 1)
        self.assertIn("схоже, вписано суму за всю кількість", texts[0])
        self.assertIn("тоді за 1 шт. — 0.25", texts[0])

    def test_unit_price_rising_with_quantity(self):
        texts = svc.price_suspects([{"qty": 1, "price": 0.3}, {"qty": 1000, "price": 240}])
        self.assertEqual(len(texts), 1)
        self.assertIn("більша, ніж на 1 шт.", texts[0])

    def test_normal_prices_are_fine(self):
        l = self.listing(self.product("T-3", [(1, "0.3"), (1000, "0.24")]), "t3", [(1, 0.35), (1000, 0.27)])
        self.assertEqual(svc.price_suspects(l.dk_prices, svc.minerva_ref(l)), [])

    def test_no_product_no_ref(self):
        self.assertEqual(svc.minerva_ref(type("L", (), {"product_id": None})()), [])

    def test_quality_check_warns(self):
        l = self.listing(self.product("T-4", [(1, "0.3"), (1000, "0.24")]), "t4", [(1000, 250)])
        hits = [i for i in _local_quality_checks(l) if i.get("field") == "dk_prices"]
        self.assertTrue(hits)
        self.assertEqual(hits[0]["severity"], "warning")


class EditorPageTests(PriceBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))
        self.s.currency = "USD"
        self.s.save()

    def test_card_has_sum_column_and_reference(self):
        l = self.listing(self.product("T-5", [(1, "0.3"), (1000, "0.24")]), "t5", [(1000, 250)])
        page = self.client.get(f"/admin/bots/digikeylisting/{l.pk}/change/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="dk-minerva-ref"')
        self.assertContains(page, "Сума за кількість, ")
        self.assertContains(page, "DK_PCUR = 'USD'")             # валюта — з «💲 Налаштувань цін DigiKey»
        self.assertEqual([r["qty"] for r in page.context["minerva_ref"]], [1, 1000])
