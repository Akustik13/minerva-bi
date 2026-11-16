"""Список замовлень: фільтри угорі й збоку (правила — ті самі, що дашборд і плитки), числа на кнопках = скільки
покаже кнопка, смуга «Діє:» з ✕ і «Скинути фільтри», підсвітка увімкнених кнопок; швидкість — кількість запитів не
залежить від кількості замовлень на сторінці, картки малюються лише для вигляду «Картки».

    python manage.py test sales.tests_list_filters --settings=tabele.settings_test
"""
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from crm.models import Customer
from inventory.models import Product
from sales import admin as sales_admin
from sales.models import SalesOrder, SalesOrderLine, SalesSource
from shipping.models import Carrier, Shipment

URL = "/admin/sales/salesorder/"


class Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.root = User.objects.create_superuser("root", "r@x.y", "p")
        self.client.force_login(self.root)
        SalesSource.objects.create(slug="manual", name="Вручну")
        SalesSource.objects.create(slug="digikey", name="DigiKey")
        self.today = timezone.localdate()
        self.carrier = Carrier.objects.create(name="UPS", carrier_type="ups")
        self.n = 0

    def order(self, **kw):
        """Замовлення без сигналів (bulk_create): лише поля, які перевіряє фільтр."""
        self.n += 1
        kw.setdefault("source", "manual")
        kw.setdefault("order_number", f"T{self.n:05d}")
        kw.setdefault("order_date", self.today)
        return SalesOrder.objects.bulk_create([SalesOrder(**kw)])[0]

    def ship(self, order, status="in_transit", delayed=False):
        return Shipment.objects.create(order=order, carrier=self.carrier, status=status, carrier_delayed=delayed)

    def listed(self, query="", **cookies):
        for k, v in cookies.items():
            self.client.cookies[k] = v
        r = self.client.get(URL + query)
        self.assertEqual(r.status_code, 200)
        return r, {o.pk for o in r.context["cl"].queryset}


class FilterRulesTests(Base):
    def test_overdue_follows_orders_to_ship(self):
        """🔴 Прострочені = чекають відправки (to_ship) і дедлайн минув — як дашборд і сповіщення."""
        yesterday = self.today - timedelta(days=1)
        late = self.order(status="received", shipping_deadline=yesterday)
        self.order(status="processing", shipping_deadline=self.today)                    # сьогодні — ще ні
        self.order(status="received", shipping_deadline=yesterday, document_type="QUOTE")  # запит пропозиції
        self.order(status="received", shipping_deadline=yesterday, affects_stock=False)    # без складу
        self.order(status="delivered", shipping_deadline=yesterday)                        # доставлено без дати
        self.order(status="processing", shipping_deadline=yesterday, shipped_at=self.today)  # уже поїхало
        r, pks = self.listed("?overdue=1")
        self.assertEqual(pks, {late.pk})
        self.assertEqual(r.context["sv_count_overdue"], 1)
        self.assertEqual(sales_admin._get_sales_stats()["overdue"], 1)           # плитка «Прострочені» Продажів

    def test_waiting_and_shipped(self):
        """⏳ Чекає відправки = to_ship (як плитка «Не відправлено» на головній); ✅ Відправлено — дата або статус."""
        waiting = self.order(status="received")
        delivered_no_date = self.order(status="delivered")
        shipped = self.order(status="processing", shipped_at=self.today)
        self.order(status="cancelled")
        self.order(status="received", document_type="QUOTE")
        _r, no = self.listed("?shipped=no")
        _r, yes = self.listed("?shipped=yes")
        self.assertEqual(no, {waiting.pk})
        self.assertEqual(yes, {delivered_no_date.pk, shipped.pk})
        from sales.revenue import to_ship
        self.assertEqual(no, set(to_ship().values_list("pk", flat=True)))

    def test_shipped_this_month_is_the_calendar_month(self):
        start = self.today.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        inside = {self.order(status="shipped", shipped_at=start).pk,
                  self.order(status="delivered", shipped_at=nxt - timedelta(days=1)).pk}
        self.order(status="shipped", shipped_at=start - timedelta(days=1))
        self.order(status="shipped", shipped_at=nxt)
        r, pks = self.listed("?shipped_period=month")
        self.assertEqual(pks, inside)
        self.assertEqual(sales_admin._get_sales_stats()["shipped_month"], 2)   # плитка веде саме сюди

    def test_in_transit_and_delayed_without_duplicates(self):
        two = self.order(status="shipped")
        self.ship(two)
        self.ship(two, delayed=True)
        delivered = self.order(status="delivered")
        self.ship(delivered)                                    # статус не «Відправлено» — не «в дорозі»
        r, pks = self.listed("?in_transit=1")
        self.assertEqual(list(r.context["cl"].result_list), [two])
        self.assertEqual(r.context["sv_count_in_transit"], 1)
        r, pks = self.listed("?delayed=1")
        self.assertEqual(pks, {two.pk})
        self.assertEqual(r.context["cl"].result_count, 1)


class TopCountsTests(Base):
    def setUp(self):
        super().setUp()
        for status in ("received", "received", "processing", "shipped"):
            self.order(status=status, source="digikey")
        for status in ("received", "shipped", "shipped"):
            self.order(status=status, source="manual")
        self.order(status="received", source="manual", is_flagged=True)

    def test_without_filters_numbers_are_totals(self):
        r, _ = self.listed()
        self.assertEqual(r.context["sv_status_counts"], {"received": 4, "processing": 1, "shipped": 3})
        self.assertEqual({s["slug"]: s["count"] for s in r.context["sv_sources"]}, {"digikey": 4, "manual": 4})
        self.assertNotIn('id="sv-af-bar"', r.content.decode())

    def test_each_button_number_is_what_the_click_shows(self):
        """З увімкненим «DigiKey» число на «Отримано» — скільки покаже «DigiKey + Отримано», і навпаки."""
        r, _ = self.listed("?source=digikey")
        status_counts = r.context["sv_status_counts"]
        for status in ("received", "processing", "shipped"):
            _r, pks = self.listed(f"?source=digikey&status={status}")
            self.assertEqual(status_counts.get(status, 0), len(pks), status)
        r, _ = self.listed("?status=received")
        for s in r.context["sv_sources"]:
            _r, pks = self.listed(f"?status=received&source={s['slug']}")
            self.assertEqual(s["count"], len(pks), s["slug"])
        self.assertEqual(r.context["sv_count_flagged"], 1)               # ⭐ серед «Отримано»
        _r, pks = self.listed("?status=received&flagged=1")
        self.assertEqual(len(pks), 1)

    def test_source_without_directory_entry_gets_a_button(self):
        self.order(status="received", source="ebay")
        r, _ = self.listed()
        self.assertIn("ebay", [s["slug"] for s in r.context["sv_sources"]])


class ActiveFiltersTests(Base):
    def setUp(self):
        super().setUp()
        self.order(status="received", source="digikey", client="Antenna GmbH", addr_country="DE",
                   email="buyer@antenna.example")

    def test_chips_remove_one_and_reset_all_but_sorting(self):
        r, _ = self.listed("?status=received&source=digikey&q=Antenna&order_date__year=2026&addr_country=DE&o=-3")
        active = r.context["sv_active"]
        self.assertEqual([c["key"] for c in active["chips"]], ["status", "source", "addr_country", "date", "q"])
        self.assertEqual(active["reset"], "?o=-3")
        by_key = {c["key"]: c for c in active["chips"]}
        self.assertEqual(by_key["status"]["value"], "📥 Отримано")                # без лічильника
        self.assertNotIn("status=", by_key["status"]["remove"])
        self.assertIn("source=digikey", by_key["status"]["remove"])
        self.assertNotIn("q=", by_key["q"]["remove"])
        self.assertNotIn("order_date__year", by_key["date"]["remove"])
        html = r.content.decode()
        self.assertIn('id="sv-af-bar"', html)
        self.assertIn('id="sv-af-reset" href="?o=-3"', html)

    def test_field_from_a_link_is_shown(self):
        """?email=… (посилання з інших сторінок) Django теж застосовує — у смузі, «Скинути» прибирає все."""
        r, pks = self.listed("?email=buyer@antenna.example")
        self.assertEqual(len(pks), 1)
        chips = r.context["sv_active"]["chips"]
        self.assertEqual([(c["key"], c["title"], c["value"]) for c in chips],
                         [("email", "Email", "buyer@antenna.example")])
        self.assertEqual(r.context["sv_active"]["reset"], "?")
        r, pks = self.listed("?email=buyer@antenna.example&addr_country=XX")    # країна без замовлень — 0
        self.assertEqual(pks, set())
        self.assertIn(("addr_country", "XX"), [(c["key"], c["value"]) for c in r.context["sv_active"]["chips"]])

    def test_side_filter_without_choices_is_not_shown(self):
        """Бічний фільтр без жодного варіанта Django не застосовує (усі замовлення без країни) — смуга теж мовчить."""
        SalesOrder.objects.update(addr_country="")
        r, pks = self.listed("?addr_country=XX")
        self.assertEqual(len(pks), 1)
        self.assertEqual(r.context["sv_active"]["chips"], [])

    def test_value_without_orders_is_still_shown(self):
        r, pks = self.listed("?status=cancelled")
        self.assertEqual(pks, set())
        self.assertEqual([(c["key"], c["value"]) for c in r.context["sv_active"]["chips"]], [("status", "cancelled")])

    def test_side_filter_and_date_month_label(self):
        r, _ = self.listed(f"?shipped=no&order_date__year={self.today.year}&order_date__month=11")
        chips = {c["key"]: c for c in r.context["sv_active"]["chips"]}
        self.assertEqual(chips["shipped"]["title"], "Відправка")
        self.assertEqual(chips["shipped"]["value"], "⏳ Чекає відправки")
        self.assertEqual(chips["date"]["value"], f"{self.today.year} · листопад")

    def test_active_buttons_are_marked_on_the_server(self):
        html = self.listed("?status=received&flagged=1")[0].content.decode()
        self.assertIn('class="sv-qf-btn sv-qf-active" id="svf-s-received"', html)
        self.assertIn('class="sv-qf-btn sv-qf-active" id="svf-flagged"', html)
        self.assertIn('class="sv-qf-btn" id="svf-s-all"', html)                # «Всі» — не підсвічено
        plain = self.listed("")[0].content.decode()
        self.assertIn('class="sv-qf-btn sv-qf-all-on" id="svf-s-all"', plain)


class SpeedTests(Base):
    def make(self, count, start=0):
        product = Product.objects.get_or_create(sku="AN220213-01C", defaults={"name": "Antenna"})[0]
        for i in range(start, start + count):
            c = Customer.objects.create(name=f"Kunde {i}", email=f"k{i}@kunde.example")
            o = self.order(status="shipped" if i % 2 else "received", email=c.email, customer_key=c.external_key,
                           client=f"Firma {i}", addr_country="DE", shipped_at=self.today if i % 2 else None)
            SalesOrderLine.objects.bulk_create([
                SalesOrderLine(order=o, product=product, sku_raw="AN220213-01C", qty=D("2"), unit_price=D("5"),
                               total_price=D("10")),
                SalesOrderLine(order=o, product=product, sku_raw="AN220213-01C-X", qty=D("1"), unit_price=D("3"),
                               total_price=D("3"))])
            if i % 2:
                self.ship(o, status="delivered")

    def queries(self, query="", **cookies):
        for k, v in cookies.items():
            self.client.cookies[k] = v
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(URL + query)
        self.assertEqual(r.status_code, 200)
        return len(ctx.captured_queries), r

    def test_queries_do_not_grow_with_orders(self):
        """Раніше 20–30 запитів на кожне замовлення (клієнт CRM і RFM, відправлення, сума, країна, етикетки)."""
        self.make(3)
        self.queries()                                           # перший запит створює налаштування (singleton)
        few, _ = self.queries()
        self.make(12, start=3)
        many, r = self.queries()
        self.assertEqual(few, many)
        html = r.content.decode()
        self.assertIn("Kunde 14", html)                          # колонка «Клієнт» знайшла клієнта CRM
        self.assertIn("13.00", html)                             # колонка «Сума»: 10 + 3

    def test_cards_only_for_the_cards_view(self):
        self.make(2)
        _n, r = self.queries(sv_view="table")
        html = r.content.decode()
        self.assertIn('data-rendered="0"', html)
        self.assertNotIn('class="sv-card sv-card-', html)
        _n, r = self.queries(sv_view="cards")
        html = r.content.decode()
        self.assertIn('data-rendered="1"', html)
        self.assertIn('class="sv-card sv-card-', html)
        self.assertIn("13,00", html)                               # «Разом» картки — з уже завантажених позицій

    def test_customer_by_company_name_and_rfm(self):
        c = Customer.objects.create(name="Kunde A GmbH", email="")
        o = self.order(status="received", client="kunde a gmbh")          # без email і ключа — за назвою
        _n, r = self.queries(sv_view="table")
        html = r.content.decode()
        self.assertIn(f"/admin/crm/customer/{c.pk}/change/", html)
        self.assertEqual(r.context["cl"].result_list[0]._crm_by_name, c)
        self.assertEqual(o.pk, r.context["cl"].result_list[0].pk)

    def test_label_file_found_once_per_page(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "AN220213-01C Alt.dymo").write_text("<x/>", encoding="utf-8")
        self.make(2)
        with override_settings(LABELS_DIR=tmp):
            _n, r = self.queries(sv_view="table")
        html = r.content.decode()
        self.assertIn('data-sku="AN220213-01C" data-qty="2"', html)
        self.assertIn('data-file="1"', html)                    # «AN220213-01C Alt.dymo» — файл артикула
        self.assertIn('data-sku="AN220213-01C-X" data-qty="1" data-order', html)
