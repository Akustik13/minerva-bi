"""«📈 Ціни й продажі» і «📄 Звіт по товару» (inventory/services/product_report.py, inventory/admin_report.py).

    python manage.py test inventory.tests_product_report --settings=tabele.settings_test
"""
import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase
from django.utils import timezone

from inventory.models import Product, ProductPriceHistory
from inventory.services import product_report as pr
from sales.models import SalesOrder, SalesOrderLine, SalesSource
from shop.models import ShopSettings

D = Decimal


def months_ago(n: int, day: int = 10) -> datetime.date:
    """День day місяця n місяців тому (поточний місяць — не пізніше сьогодні)."""
    today = timezone.localdate()
    first = pr._add_months(today.replace(day=1), -n)
    d = first.replace(day=min(day, 28))
    return min(d, today) if n == 0 else d


def at(n: int, day: int = 10) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime.combine(months_ago(n, day), datetime.time(12)))


def staff(username, role="admin", **profile):
    user = User.objects.create_user(username, f"{username}@example.com", "p", is_staff=True)
    user.profile.role = role
    for key, value in profile.items():
        setattr(user.profile, key, value)
    user.profile.save()
    return User.objects.get(pk=user.pk)


class Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.default_currency = "EUR"
        ss.save()
        fx = ShopSettings.get()
        fx.fx_rates = {"USD": 0.5}          # 1 USD = 0,50 EUR; GBP — курсу немає
        fx.save()
        self.product = Product.objects.create(sku="FT220407", name="Filter", base_prices=[], base_price_currency="EUR")
        self.n = 0

    def order(self, qty, line_total, currency="EUR", when=None, source="webshop", client="Muster GmbH", **kw):
        self.n += 1
        values = dict(source=source, order_number=f"9{self.n:05d}", order_date=when or months_ago(0),
                      currency=currency, client=client)
        values.update(kw)
        o = SalesOrder.objects.create(**values)
        SalesOrderLine.objects.create(order=o, product=self.product, qty=qty, total_price=line_total,
                                      unit_price=None, currency=currency)
        return o

    def history(self, n, old, new, currency="EUR"):
        h = ProductPriceHistory.objects.create(
            product=self.product, source="manual", currency=currency, user_label="anna",
            old_prices=[{"min_qty": 1, "unit_price": str(old)}] if old else [],
            new_prices=[{"min_qty": 1, "unit_price": str(new)}] if new else [])
        ProductPriceHistory.objects.filter(pk=h.pk).update(created_at=at(n))
        return h

    def listing(self, prices, published=True):
        from bots.models import DigiKeyListing, DigiKeyPriceSettings
        s = DigiKeyPriceSettings.get()
        s.currency = "USD"
        s.save()
        return DigiKeyListing.objects.create(
            product=self.product, dk_title="DK FT220407", dk_description="Filter", dk_prices=prices,
            sync_status="published" if published else "draft")

    def dk_log(self, listing, n, old, new):
        from bots.models import DigiKeyPriceLog
        lg = DigiKeyPriceLog.objects.create(listing=listing, delta_pct=0, user="bot",
                                            old_prices=[{"qty": 1, "price": old}], new_prices=[{"qty": 1, "price": new}])
        DigiKeyPriceLog.objects.filter(pk=lg.pk).update(applied_at=at(n))
        return lg


class SalesSeriesTests(Base):
    def test_months_zero_currencies_and_only_real_sales(self):
        self.order(2, D("20"))                                   # цей місяць: 2 шт, €20
        self.order(4, D("40"), "USD", when=months_ago(2))        # 2 міс тому: 4 шт, $40 = €20
        self.order(9, D("90"), status="cancelled")
        self.order(9, D("90"), document_type="QUOTE", affects_stock=False)
        self.order(9, D("90"), document_type="SAMPLE")
        self.order(9, D("90"), affects_stock=False)              # тестовий магазин
        self.order(9, D("90"), when=months_ago(30))              # поза періодом
        s = pr.sales_series(self.product)
        self.assertEqual(len(s["months"]), 24)
        self.assertEqual(s["months"][-1], timezone.localdate().replace(day=1))
        self.assertEqual((s["units"][-1], s["units"][-3]), (D("2"), D("4")))
        self.assertEqual(sum(s["units"]), D("6"))
        self.assertEqual(s["units"][-2], 0)                      # місяць без продажів — 0
        self.assertEqual((s["revenue"][-1].total, s["revenue"][-3].total), (D("20"), D("20")))
        self.assertEqual((s["orders"][-1], s["orders"][-3], s["orders"][-2]), (1, 1, 0))
        self.assertEqual((s["avg_price"][-1], s["avg_price"][-3], s["avg_price"][-2]), (D("10"), D("5"), None))
        self.assertEqual(s["total_revenue"].total, D("40"))
        self.assertEqual(s["labels"][-1], pr.month_label(s["months"][-1]))

    def test_line_without_price_counts_units_but_not_average(self):
        self.order(3, D("30"))
        self.order(7, None)                                      # без ціни (зразок у рядку продажу)
        s = pr.sales_series(self.product)
        self.assertEqual((s["units"][-1], s["avg_price"][-1]), (D("10"), D("10")))

    def test_missing_rate_kept_apart(self):
        self.order(1, D("100"), "GBP")
        s = pr.sales_series(self.product)
        self.assertEqual(s["revenue"][-1].total, 0)
        self.assertEqual(s["revenue"][-1].missing, {"GBP": D("100")})
        self.assertIsNone(s["avg_price"][-1])
        self.assertTrue(any("без курсу" in n for n in s["notes"]))

    def test_top_three_channels_and_others(self):
        SalesSource.objects.create(slug="digikey", name="DigiKey")
        for source, qty in (("digikey", 50), ("webshop", 40), ("ebay", 30), ("amazon", 20), ("mail", 10)):
            self.order(qty, D(qty), source=source)
        s = pr.sales_series(self.product)
        names = [c["name"] for c in s["channels"]]
        self.assertEqual(names, ["DigiKey", "webshop", "ebay", pr.OTHERS])
        self.assertEqual(s["channels"][-1]["total"], D("30"))
        self.assertEqual(sorted(s["channels"][-1]["sources"]), ["amazon", "mail"])
        self.assertEqual(s["rows"][-1]["channels"], [D("50"), D("40"), D("30"), D("30")])

    def test_four_channels_named_without_others(self):
        for source, qty in (("a", 4), ("b", 3), ("c", 2), ("d", 1)):
            self.order(qty, D(qty), source=source)
        names = [c["name"] for c in pr.sales_series(self.product)["channels"]]
        self.assertEqual(names, ["a", "b", "c", "d"])


class PriceSeriesTests(Base):
    def test_minerva_history_steps_in_home_currency(self):
        self.history(18, None, "8")
        self.history(6, "8", "10")
        self.product.base_prices = [{"min_qty": 1, "unit_price": "10"}, {"min_qty": 10, "unit_price": "9"}]
        self.product.save()
        p = pr.price_series(self.product)
        pts = p["minerva"]["points"]
        self.assertEqual([x.price for x in pts], [D("8"), D("10"), D("10")])
        self.assertEqual([x.change for x in pts], [True, True, False])
        self.assertEqual(pts[0].at, at(18))
        self.assertEqual(p["minerva"]["now"], D("10"))
        self.assertEqual([e["change_pct"] for e in p["events"]], [25.0, None])     # новіші першими
        self.assertIsNone(p["digikey"])
        self.assertEqual(p["notes"], [])

    def test_price_before_period_starts_the_line(self):
        self.history(30, None, "7")                       # до початку періоду — діяла на його початку
        self.product.base_prices = [{"min_qty": 1, "unit_price": "7"}]
        self.product.save()
        pts = pr.price_series(self.product)["minerva"]["points"]
        self.assertEqual([x.price for x in pts], [D("7"), D("7")])
        self.assertEqual(pts[0].at, pr._aware(pr.window(timezone.localdate())[0]))

    def test_digikey_in_usd_converted_and_realized_average(self):
        listing = self.listing([{"qty": 1, "price": 24}, {"qty": 10, "price": 22}])
        self.dk_log(listing, 3, 22, 24)
        self.order(4, D("40"), "USD", when=months_ago(1))      # $10/шт = €5
        p = pr.price_series(self.product)
        self.assertEqual([x.price for x in p["digikey"]["points"]], [D("11"), D("12"), D("12")])
        self.assertEqual(p["digikey"]["currency"], "USD")
        self.assertEqual([(r["month"], r["price"], r["units"]) for r in p["realized"]],
                         [(months_ago(1).replace(day=1), D("5"), D("4"))])
        chart = pr.chart_data(p, pr.sales_series(self.product))
        self.assertEqual([pt["y"] for pt in chart["digikey"]], [11.0, 12.0, 12.0])
        self.assertEqual(chart["realized"][0]["y"], 5.0)

    def test_missing_rate_point_skipped_not_mixed(self):
        self.product.base_prices = [{"min_qty": 1, "unit_price": "9"}]
        self.product.base_price_currency = "GBP"
        self.product.save()
        p = pr.price_series(self.product)
        self.assertTrue(p["minerva"]["points"])
        self.assertTrue(all(x.price is None for x in p["minerva"]["points"]))
        self.assertTrue(any("GBP" in n for n in p["notes"]))
        chart = pr.chart_data(p, pr.sales_series(self.product))
        self.assertTrue(all(pt["y"] is None for pt in chart["minerva"]))

    def test_gap_keeps_previous_price_until_the_gap(self):
        self.history(10, None, "8")
        self.history(5, "8", "9", currency="GBP")             # ціна в GBP — курсу немає: розрив з цієї дати
        self.product.base_prices = [{"min_qty": 1, "unit_price": "9"}]
        self.product.base_price_currency = "GBP"
        self.product.save()
        chart = pr.chart_data(pr.price_series(self.product), pr.sales_series(self.product))
        ys = [pt["y"] for pt in chart["minerva"]]
        self.assertEqual(ys[:3], [8.0, 8.0, None])           # €8 тягнеться до розриву, далі без ціни
        self.assertEqual(chart["minerva"][1]["x"], chart["minerva"][2]["x"])

    def test_digikey_change_without_log_noted(self):
        listing = self.listing([{"qty": 1, "price": 30}])
        self.dk_log(listing, 8, 20, 24)
        from bots.models import DigiKeyListing
        DigiKeyListing.objects.filter(pk=listing.pk).update(price_checked_at=at(2))
        p = pr.price_series(self.product)
        self.assertEqual([x.price for x in p["digikey"]["points"]], [D("10"), D("12"), D("15"), D("15")])
        self.assertEqual(p["digikey"]["points"][2].at, at(2))
        self.assertTrue(any("без запису в історії" in n for n in p["notes"]))

    def test_sale_price_when_no_base_prices(self):
        self.product.sale_price = D("6.5")
        self.product.save()
        p = pr.price_series(self.product)
        self.assertEqual(p["minerva"]["source"], "sale")
        self.assertEqual(p["minerva"]["now"], D("6.5"))
        self.assertTrue(any("Ціну продажу" in n for n in p["notes"]))

    def test_draft_listing_noted(self):
        self.listing([{"qty": 1, "price": 10}], published=False)
        self.assertTrue(any("ще не діють" in n for n in pr.price_series(self.product)["notes"]))


class KpiTests(Base):
    def test_year_over_year_customers_stock_and_tiers(self):
        from inventory.models import Location
        from inventory.services import stock
        Location.objects.get_or_create(code="MAIN")
        stock.create_movement(product=self.product, tx_type="Incoming", qty=D("100"))
        self.order(4, D("40"), when=months_ago(2), client="Muster GmbH")
        self.order(6, D("30"), "USD", when=months_ago(3), client="Beta AG")      # $30 = €15
        self.order(2, D("20"), when=months_ago(14), client="Muster GmbH")
        self.product.base_prices = [{"min_qty": 1, "unit_price": "10"}, {"min_qty": 10, "unit_price": "9"}]
        self.product.save()
        self.listing([{"qty": 1, "price": 24}, {"qty": 5, "price": 22}])
        k = pr.kpis(self.product)
        self.assertEqual((k["units_12"], k["units_prev"], k["units_pct"]), (D("10"), D("2"), 400.0))
        self.assertEqual((k["revenue_12"].total, k["revenue_prev"].total, k["revenue_pct"]), (D("55"), D("20"), 175.0))
        self.assertEqual(k["avg_price"], D("5.5"))
        self.assertEqual(k["orders_12"], 2)
        self.assertEqual(k["customers"], 2)
        self.assertEqual([(c["name"], c["units"], c["orders"]) for c in k["top_customers"]],
                         [("Muster GmbH", D("6"), 2), ("Beta AG", D("6"), 1)])
        row = stock.annotate_stock(Product.objects.filter(pk=self.product.pk)).get()
        self.assertEqual((k["stock"]["on_hand"], k["stock"]["available"]), (row._on_hand, row._available))
        self.assertIsNotNone(k["stock"]["months_left"])
        t = k["tiers"]
        self.assertEqual([r["qty"] for r in t["rows"]], [1, 5, 10])
        self.assertEqual([r["minerva"] for r in t["rows"]], [D("10"), D("10"), D("9")])
        self.assertEqual([r["minerva_break"] for r in t["rows"]], [True, False, True])
        self.assertEqual([r["digikey_home"] for r in t["rows"]], [D("12"), D("11"), D("11")])
        self.assertEqual([r["diff_pct"] for r in t["rows"]], [20.0, 10.0, 22.2])

    def test_no_sales(self):
        k = pr.kpis(self.product)
        self.assertEqual((k["units_12"], k["units_pct"], k["customers"], k["top_customers"]), (0, None, 0, []))
        self.assertEqual(k["stock"]["status"], "no_sales")
        self.assertEqual(k["tiers"]["rows"], [])

    def test_build_and_csv(self):
        self.order(2, D("20"))
        self.history(4, "8", "10")
        data = pr.build(self.product)
        self.assertEqual(data["months"], 24)
        self.assertEqual(len(data["orders"]), 1)
        self.assertEqual(data["orders"][0]["unit_price"], D("10.00"))
        rows = pr.csv_rows(data)
        self.assertIn("Місяць", rows[1])
        self.assertTrue(any(r and r[0] == timezone.localdate().strftime("%Y-%m") for r in rows))
        self.assertTrue(any("Minerva" in r for r in rows if r))
        self.assertNotIn("orders", pr.build(self.product, tables=False))
        self.assertEqual(pr.build(self.product, months=7)["months"], 24)          # лише 12 / 24 / 36


class PageTests(Base):
    def setUp(self):
        super().setUp()
        self.order(2, D("20"), client="R&D <GmbH>")
        self.history(3, "8", "10")

    def url(self, pk=None, tail=""):
        return f"/admin/inventory/product/{pk or self.product.pk}/report/{tail}"

    def test_report_for_view_only_staff(self):
        self.client.force_login(staff("acc", role="accountant"))       # склад — лише перегляд
        r = self.client.get(self.url())
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("📄 FT220407", html)
        self.assertIn('id="prr-chart-data"', html)                      # дані графіків — json_script
        self.assertIn("R&amp;D &lt;GmbH&gt;", html)
        self.assertNotIn("R&D <GmbH>", html)
        self.assertIn("Продажі по місяцях", html)
        self.assertEqual(self.client.get(self.url() + "?months=12").context["r"]["months"], 12)
        csv = self.client.get(self.url(tail="csv/"))
        self.assertEqual(csv.status_code, 200)
        self.assertIn("text/csv", csv["Content-Type"])
        self.assertIn("FT220407-report-", csv["Content-Disposition"])
        text = csv.content.decode("utf-8")
        self.assertTrue(text.startswith(chr(0xFEFF)))                  # BOM для Excel
        self.assertIn("Місяць;Штук;Замовлень;Виручка, EUR", text)
        self.assertIn(";25,0;anna;", text)                               # зміна ціни 8 → 10: +25 %

    def test_card_has_block_and_link(self):
        self.client.force_login(User.objects.create_superuser("root", "r@example.com", "p"))
        r = self.client.get(f"/admin/inventory/product/{self.product.pk}/change/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("📈 Ціни й продажі", html)
        self.assertIn('id="prw-chart-data"', html)
        self.assertIn(self.url(), html)

    def test_empty_product_and_add_form(self):
        self.client.force_login(User.objects.create_superuser("root", "r@example.com", "p"))
        empty = Product.objects.create(sku="AN050201")
        html = self.client.get(f"/admin/inventory/product/{empty.pk}/change/").content.decode()
        self.assertIn("Цін ще немає", html)
        self.assertIn("За цей період продажів не було", html)
        self.assertEqual(self.client.get(self.url(pk=empty.pk)).status_code, 200)
        add = self.client.get("/admin/inventory/product/add/")
        self.assertEqual(add.status_code, 200)
        self.assertNotIn("product_report", add.context)

    def test_forbidden_and_missing(self):
        self.client.force_login(staff("mgr", role="manager"))          # без розділу «Склад»
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.assertEqual(self.client.get(self.url(tail="csv/")).status_code, 403)
        self.client.force_login(staff("boss"))
        self.assertEqual(self.client.get(self.url(pk=999999)).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.url()).status_code, 302)  # вхід

    def test_view_checks_view_permission(self):
        from django.contrib import admin
        from inventory.admin_report import report_view
        request = RequestFactory().get(self.url())
        request.user = staff("reader", role="readonly")
        with self.assertRaises(PermissionDenied):
            report_view(admin.site._registry[Product], request, self.product.pk)
