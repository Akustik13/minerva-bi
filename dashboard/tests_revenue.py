"""Дашборд, тенденції й сигнали: лише продажі, виручка у валюті Minerva, рівні періоди (sales/revenue.py).

    python manage.py test dashboard --settings=tabele.settings_test
"""
import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from dashboard import views
from inventory.models import Product
from sales import revenue as rv
from sales.models import SalesOrder, SalesOrderLine
from shop.models import ShopSettings

TODAY = datetime.date(2026, 10, 30)


def ready_admin(test):
    from config.models import SystemSettings
    ss = SystemSettings.objects.get_or_create(pk=1)[0]
    ss.is_onboarding_complete = True
    ss.default_currency = "EUR"
    ss.save()
    test.user = User.objects.create_user("boss", password="x", is_staff=True, is_superuser=True)
    test.client.force_login(test.user)


class Data(TestCase):
    def setUp(self):
        ready_admin(self)
        ss = ShopSettings.get()
        ss.fx_rates = {"USD": 0.5}          # 1 USD = 0,50 EUR
        ss.save()
        self.product = Product.objects.create(sku="FT220407", name="Filter")
        self.n = 0

    def order(self, total=None, currency="EUR", lines=(), **kw):
        self.n += 1
        values = dict(source="webshop", order_number=f"9{self.n:05d}", order_date=TODAY,
                      total_price=total, currency=currency, client="Muster GmbH")
        values.update(kw)
        o = SalesOrder.objects.create(**values)
        for qty, unit, line_total in lines:
            SalesOrderLine.objects.create(order=o, product=self.product, qty=qty, unit_price=unit,
                                          total_price=line_total, currency=currency)
        return o


class RevenueRuleTests(Data):
    def test_sale_orders_skip_cancelled_quotes_and_test_orders(self):
        sale = self.order(100)
        self.order(100, status="cancelled")
        self.order(100, document_type="QUOTE", affects_stock=False)
        self.order(100, affects_stock=False)            # тестовий магазин
        self.order(100, document_type="SAMPLE")
        self.assertEqual(list(rv.sale_orders()), [sale])

    def test_to_ship_keeps_samples_but_not_cancelled_or_quotes(self):
        sale = self.order(100)
        sample = self.order(None, document_type="SAMPLE")
        self.order(100, status="cancelled")
        self.order(None, document_type="QUOTE")
        self.order(100, status="shipped")
        self.order(100, shipped_at=TODAY)
        self.assertEqual(set(rv.to_ship()), {sale, sample})

    def test_currencies_converted_and_missing_rate_kept_apart(self):
        self.order(100, "EUR")
        self.order(200, "USD")                          # → 100 EUR
        self.order(1000, "GBP")                         # курсу немає
        money = rv.revenue(rv.sale_orders())
        self.assertEqual(money.total, Decimal("200"))
        self.assertEqual(money.missing, {"GBP": Decimal("1000")})
        self.assertEqual(money.text, "€200")
        self.assertEqual(money.missing_text, "+ £1,000 без курсу")

    def test_order_without_total_uses_lines_unit_times_qty(self):
        self.order(None, "USD", lines=[(3, Decimal("10"), None), (1, None, Decimal("4"))])
        self.assertEqual(rv.revenue(rv.sale_orders()).total, Decimal("17"))   # (30 + 4) × 0,5

    def test_grouped_by_month(self):
        self.order(100, order_date=datetime.date(2026, 9, 3))
        self.order(None, lines=[(2, Decimal("5"), None)], order_date=datetime.date(2026, 9, 20))
        self.order(40, "USD", order_date=TODAY)
        by = rv.revenue(rv.sale_orders(), by={"month": views._month})
        totals = {views._month_key(k[0]): m.total for k, m in by.items()}
        self.assertEqual(totals, {datetime.date(2026, 9, 1): Decimal("110"), datetime.date(2026, 10, 1): Decimal("20")})


class PeriodTests(TestCase):
    def test_preset_is_n_days_and_previous_is_as_long(self):
        b = views.period_bounds(TODAY, period="30")
        self.assertEqual((b["to"] - b["from"]).days + 1, 30)
        self.assertEqual((b["prev_to"] - b["prev_from"]).days + 1, 30)
        self.assertEqual(b["prev_to"], b["from"] - datetime.timedelta(days=1))

    def test_custom_range_inclusive(self):
        b = views.period_bounds(TODAY, datetime.date(2026, 10, 1), datetime.date(2026, 10, 10))
        self.assertEqual(b["days"], 10)
        self.assertEqual((b["prev_from"], b["prev_to"]), (datetime.date(2026, 9, 21), datetime.date(2026, 9, 30)))

    def test_ytd_spans_are_equal(self):
        cur, prev = views.ytd_spans(TODAY, 2026)
        self.assertEqual(cur, (datetime.date(2026, 1, 1), TODAY))
        self.assertEqual(prev, (datetime.date(2025, 1, 1), datetime.date(2025, 10, 30)))
        cur, prev = views.ytd_spans(datetime.date(2028, 2, 29), 2028)
        self.assertEqual(prev[1], datetime.date(2027, 2, 28))
        self.assertEqual(views.ytd_spans(TODAY, 2024)[0], (datetime.date(2024, 1, 1), datetime.date(2024, 12, 31)))

    def test_js_json_cannot_close_script(self):
        self.assertNotIn("</script>", views._js(["</script><b>&"]))


@mock.patch("django.utils.timezone.localdate", return_value=TODAY)
class DashboardViewTests(Data):
    def test_kpi_and_tables(self, _today):
        self.order(100, lines=[(1, Decimal("50"), Decimal("50")), (1, Decimal("50"), Decimal("50"))])
        self.order(200, "USD", lines=[(4, Decimal("50"), Decimal("200"))])
        self.order(1000, "GBP", client="Other Ltd")
        self.order(500, status="cancelled")
        self.order(None, document_type="QUOTE", affects_stock=False, client="Quote AG")
        r = self.client.get("/dashboard/", {"date_from": "2026-10-01", "date_to": "2026-10-30"})
        self.assertEqual(r.status_code, 200)
        kpi = r.context["kpi"]
        self.assertEqual(kpi["revenue"]["val"], 200.0)
        self.assertEqual(kpi["revenue"]["note"], "+ £1,000 без курсу")
        self.assertEqual(kpi["orders"]["val"], 3)
        self.assertContains(r, "+ £1,000 без курсу")
        # Топ клієнти: 2 замовлення (не 3 рядки), виручка не помножена на кількість рядків
        top = {c["client"]: c for c in r.context["top_clients"]}
        self.assertEqual(top["Muster GmbH"]["orders"], 2)
        self.assertEqual(top["Muster GmbH"]["revenue"], 200.0)
        self.assertEqual(top["Muster GmbH"]["units"], Decimal("6"))
        self.assertNotIn("Quote AG", top)
        # Не відправлені — без скасованого й запиту пропозиції
        self.assertEqual(kpi["unshipped"]["val"], 3)
        self.assertEqual(r.context["cur_sym"], "€")

    def test_shipping_costs_in_minerva_currency(self, _today):
        self.order(10, shipping_courier="DHL", shipping_region="DE", shipping_cost=Decimal("20"), shipping_currency="USD")
        self.order(10, shipping_courier="DHL", shipping_region="DE", shipping_cost=Decimal("6"), shipping_currency="EUR")
        self.order(10, shipping_courier="DHL", shipping_region="PL", shipping_cost=Decimal("9"), shipping_currency="CHF")
        r = self.client.get("/dashboard/", {"date_from": "2026-10-01", "date_to": "2026-10-30"})
        dhl = r.context["by_courier"][0]
        self.assertEqual((dhl["orders"], dhl["total_cost"], dhl["avg_cost"]), (3, 16.0, 8.0))
        self.assertEqual(dhl["missing"], "+ 9 CHF без курсу")
        de = next(x for x in r.context["shipping_by_country"] if x["shipping_region"] == "DE")
        self.assertEqual((de["total_cost"], de["couriers"]), (16.0, "DHL"))

    def test_previous_period_has_same_length(self, _today):
        r = self.client.get("/dashboard/", {"period": "30"})
        self.assertEqual(r.context["date_from_str"], "2026-10-01")
        self.assertEqual((r.context["prev_date_from_str"], r.context["prev_date_to_str"]), ("2026-09-01", "2026-09-30"))

    def test_trends_page(self, _today):
        self.order(100, order_date=datetime.date(2026, 3, 5))
        self.order(80, order_date=datetime.date(2025, 3, 5))
        self.order(999, order_date=datetime.date(2025, 12, 5))      # після 30.10.2025 — не в YTD
        self.order(500, status="cancelled", order_date=datetime.date(2026, 3, 6))
        r = self.client.get("/dashboard/trends/", {"year_from": "2025", "year_to": "2026"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["yoy_growth"], 25.0)                # 100 vs 80 — однакові відрізки
        self.assertEqual(r.context["curr_year_total"], 100.0)


@mock.patch("django.utils.timezone.localdate", return_value=TODAY)
class SignalsTests(Data):
    def test_unshipped_signal_only_for_orders_to_ship(self, _today):
        old = TODAY - datetime.timedelta(days=10)
        self.order(10, order_date=old, order_number="WAIT-1")
        self.order(10, order_date=old, status="cancelled", order_number="CANC-1")
        self.order(None, order_date=old, document_type="QUOTE", order_number="QUOTE-1")
        from dashboard.signals_views import _collect_signals
        titles = " ".join(s["title"] for s in _collect_signals())
        self.assertIn("WAIT-1", titles)
        self.assertNotIn("CANC-1", titles)
        self.assertNotIn("QUOTE-1", titles)
