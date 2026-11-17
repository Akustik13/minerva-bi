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


class RoundingNoteTests(PriceBase):
    def test_rounding_shift_on_cheap_part_is_noted(self):
        new, notes = svc.compute([(1, D("0.006"))], None, svc.Rules(), D("1.1"))
        self.assertEqual(new, [(1, D("0.01"))])                         # 0,0066 → 0,01
        self.assertTrue(any("округлення змінює ціну" in n and "Без округлення" in n for n in notes))
        new, notes = svc.compute([(1, D("0.006"))], None, svc.Rules(rounding="none"), D("1.1"))
        self.assertFalse(any("округлення" in n for n in notes))

    def test_normal_price_no_note(self):
        _, notes = svc.compute([(1, D("10"))], None, svc.Rules(), D("1.1"))
        self.assertFalse(any("округлення" in n for n in notes))


class QtyAlertTests(PriceBase):
    """DigiKey: «When provided, Min Quantity Alert must be greater than or equal to Min Order Quantity» (400)."""

    def test_rule(self):
        from bots.services.dk_marketplace import qty_alert
        self.assertIsNone(qty_alert(0, 1))                 # 0 — не надсилаємо (без алерту)
        self.assertIsNone(qty_alert(None, 4000))
        self.assertEqual(qty_alert(3, 1), 3)
        self.assertEqual(qty_alert(3, 4000), 4000)        # менше за MOQ — MOQ

    def test_offer_payload_from_listing(self):
        from bots.services.dk_marketplace import _offer_payload
        l = self.listing(self.product("QA-1", [(1, 1)]), "qa1", [(1, 1.2)], dk_qty_alert=0)
        self.assertNotIn("minQuantityAlert", _offer_payload(l))
        l.dk_qty_alert, l.dk_min_order_qty = 3, 4000
        body = _offer_payload(l)
        self.assertEqual((body["minOrderQuantity"], body["minQuantityAlert"]), (4000, 4000))

    def test_price_put_keeps_digikey_alert_valid(self):
        offer = {"supplierSku": "X", "title": "X", "quantityAvailable": 5, "minOrderQuantity": 100,
                 "leadTimeToShip": 3, "minQuantityAlert": 0}
        body = svc.offer_payload(offer, [(100, D("1.5"))])
        self.assertNotIn("minQuantityAlert", body)

    def test_push_only_moq_raises_alert_too(self):
        from bots.services import dk_view
        l = self.listing(self.product("QA-2", [(1, 1)]), "qa2", [(1, 1.2)], dk_qty_alert=3, dk_min_order_qty=1000)
        self.dk.offers["qa2"]["minQuantityAlert"] = 3
        dk_view.push_offer_fields(l, ["minOrderQuantity"])
        sent = self.dk.puts[-1][1]
        self.assertEqual((sent["minOrderQuantity"], sent["minQuantityAlert"]), (1000, 1000))
