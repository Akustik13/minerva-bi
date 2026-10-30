"""Продажі: імпорт Excel (маппінг, часткові помилки), джерело в картці замовлення.

    python manage.py test sales --settings=tabele.settings_test
"""
import io
import uuid
from decimal import Decimal as D

import openpyxl
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from inventory.models import InventorySettings, InventoryTransaction, Location, Product
from inventory.services import stock
from sales import importing
from sales.models import SalesOrder, SalesOrderLine, SalesSource


class MappingTests(TestCase):
    def test_order_date_is_not_order_number(self):
        cols = ["order_number", "order_date", "client", "email", "addr_country", "sku_raw", "qty", "unit_price"]
        self.assertEqual(importing.guess_mapping(cols), cols)
        self.assertEqual(importing.guess_mapping(["order_date", "order_number"]), ["order_date", "order_number"])

    def test_whole_words_beat_substrings_and_no_field_twice(self):
        m = importing.guess_mapping(["Order", "Order Date", "Customer", "Customer Email", "Ship Date",
                                     "SKU", "Quantity", "Unit Price", "Total Price", "Order Total"])
        self.assertEqual(m[:9], ["order_number", "order_date", "client", "email", "shipped_at", "sku_raw", "qty",
                                 "unit_price", "total_price_line"])
        used = [f for f in m if f != "--"]
        self.assertEqual(len(used), len(set(used)))        # «Order Total» не дублює вже вибране поле

    def test_german_and_ukrainian_headers(self):
        self.assertEqual(importing.guess_mapping(["Bestellnummer", "Bestelldatum", "Artikelnummer", "Menge"]),
                         ["order_number", "order_date", "sku_raw", "qty"])
        self.assertEqual(importing.guess_mapping(["Номер замовлення", "Дата", "Артикул", "Кількість"]),
                         ["order_number", "order_date", "sku_raw", "qty"])

    def test_duplicate_fields(self):
        self.assertEqual(importing.duplicate_fields({0: "order_number", 3: "order_number", 1: "qty"}),
                         {"order_number": [0, 3]})
        self.assertEqual(importing.column_letter(27), "AB")


class AdminBase(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        st = InventorySettings.get()
        st.deduct_on, st.use_reservation, st.default_location = "creation", False, "MAIN"
        st.save()
        self.loc = Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.p = Product.objects.create(sku="FT220407", name="Filter")
        self.q = Product.objects.create(sku="AN220213", name="Antenna")
        for prod in (self.p, self.q):
            InventoryTransaction.objects.create(tx_type="Incoming", qty=50, product=prod, location=self.loc,
                                                external_key=f"in:{uuid.uuid4()}")


class ImportTests(AdminBase):
    URL = "/admin/sales/salesorder/import-excel/"
    HEAD = ["order_number", "order_date", "client", "sku_raw", "qty", "unit_price"]

    def xlsx(self, rows):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(self.HEAD)
        for r in rows:
            ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        return SimpleUploadedFile("orders.xlsx", buf.getvalue())

    def run_import(self, rows, mapping=None, **extra):
        r = self.client.post(self.URL, {"step": "1", "excel_file": self.xlsx(rows)})
        self.assertEqual(r.status_code, 200)
        auto = r.context["auto_map_json"]
        mapping = mapping or {i: f for i, f in enumerate(self.HEAD)}
        data = {"step": "2", "sheet_name": "Sheet", "conflict_mode": "skip", "default_source": "manual"}
        data.update({f"col_{i}": f for i, f in mapping.items()})
        data.update(extra)
        return auto, self.client.post(self.URL, data)

    def test_mapping_page_script_is_valid(self):
        r = self.client.post(self.URL, {"step": "1", "excel_file": self.xlsx([])})
        html = r.content.decode()
        # апостроф «Обов'язкові» в JS-рядку в одинарних лапках ламав увесь скрипт маппінгу
        self.assertNotIn("'Обов'язкові", html)
        self.assertIn("Обов\\u0027язкові поля не замаплені", html)
        self.assertIn('const AUTO_MAP = {"Sheet": ["order_number", "order_date"', html)

    def test_one_bad_sku_skips_only_that_order(self):
        auto, r = self.run_import([
            ["1001", "28.10.2026", "Muster GmbH", "FT220407", 5, "12,50 €"],
            ["1002", "28.10.2026", "Bad GmbH", "NOPE-1", 1, "1"],
            ["1002", "28.10.2026", "Bad GmbH", "FT220407", 2, "1"],
            ["1003", "28.10.2026", "Other AG", "AN220213", 3, "24"],
        ])
        self.assertIn('"order_number"', auto)
        self.assertEqual(r.status_code, 200)
        stats = r.context["stats"]
        self.assertEqual((stats["created"], stats["failed_orders"], stats["lines_created"]), (2, 1, 2))
        self.assertTrue(any("1002" in e and "NOPE-1" in e for e in stats["errors"]), stats["errors"])
        self.assertEqual(sorted(SalesOrder.objects.values_list("order_number", flat=True)), ["1001", "1003"])
        # склад: лише імпортовані замовлення, без часткового 1002
        self.assertEqual(stock.get_available(self.p), 45)
        self.assertEqual(stock.get_available(self.q), 47)

    def test_dry_run_saves_nothing(self):
        _, r = self.run_import([["2001", "28.10.2026", "X", "FT220407", 5, "1"]], dry_run="1")
        self.assertTrue(r.context["dry_run"])
        self.assertEqual(r.context["stats"]["created"], 1)
        self.assertFalse(SalesOrder.objects.exists())
        self.assertEqual(stock.get_available(self.p), 50)

    def test_one_field_from_two_columns_is_rejected(self):
        mapping = {0: "order_number", 1: "order_number", 3: "sku_raw", 4: "qty"}
        _, r = self.run_import([["3001", "28.10.2026", "X", "FT220407", 1, "1"]], mapping=mapping)
        self.assertEqual(r.status_code, 302)
        self.assertFalse(SalesOrder.objects.exists())

    def test_update_mode_replaces_lines_without_double_deduction(self):
        self.run_import([["4001", "28.10.2026", "X", "FT220407", 5, "1"]])
        self.assertEqual(stock.get_available(self.p), 45)
        o = SalesOrder.objects.get(order_number="4001")
        o.status = "shipped"
        o.save()
        self.run_import([["4001", "28.10.2026", "X", "FT220407", 3, "1"]], conflict_mode="update")
        self.assertEqual(stock.get_available(self.p), 47)
        self.assertEqual(SalesOrderLine.objects.get(order__order_number="4001").qty, 3)

    def test_existing_order_of_another_source_is_not_touched(self):
        SalesOrder.objects.create(source="digikey", order_number="5001")
        self.run_import([["5001", "28.10.2026", "X", "FT220407", 1, "1"]])
        self.assertEqual(SalesOrder.objects.filter(order_number="5001").count(), 2)


class SourceFieldTests(AdminBase):
    def setUp(self):
        super().setUp()
        SalesSource.objects.create(slug="digikey", name="DigiKey", order=1)
        SalesSource.objects.create(slug="manual", name="Вручну", order=2)

    def test_unknown_source_stays_selected(self):
        o = SalesOrder.objects.create(source="legacy_shop", order_number="L-1")
        html = self.client.get(f"/admin/sales/salesorder/{o.pk}/change/").content.decode()
        self.assertIn('<option value="legacy_shop" selected>', html)
        from sales.admin import source_choices
        self.assertEqual(source_choices("legacy_shop")[0][0], "legacy_shop")
        self.assertEqual([c[0] for c in source_choices("manual")], ["digikey", "manual"])

    def test_change_source_action_moves_stock_keys(self):
        o = SalesOrder.objects.create(source="manual", order_number="S-1")
        SalesOrderLine.objects.create(order=o, product=self.p, qty=4)
        self.client.post("/admin/sales/salesorder/", {"action": "action_change_source", "apply": "1",
                                                       "source_slug": "digikey", "_selected_action": [o.pk]})
        o.refresh_from_db()
        self.assertEqual(o.source, "digikey")
        self.assertEqual(stock.get_available(self.p), 46)
        o.status = "cancelled"
        o.save()
        self.assertEqual(stock.get_available(self.p), 50)

    def test_sync_button_uses_stock_service(self):
        o = SalesOrder.objects.create(source="manual", order_number="S-2")
        line = SalesOrderLine.objects.create(order=o, product=self.p, qty=4)
        InventoryTransaction.objects.filter(external_key__startswith="so:").update(qty=D("-9"))  # розсинхрон
        r = self.client.get(f"/admin/sales/salesorder/{o.pk}/sync-inventory/").json()
        self.assertEqual(len(r["mismatches"]), 1)
        self.assertIn("9 → 4", r["mismatches"][0]["text"])
        r = self.client.post(f"/admin/sales/salesorder/{o.pk}/sync-inventory/").json()
        self.assertTrue(r["ok"], r)
        self.assertEqual(stock.get_available(self.p), 46)
        self.assertEqual(line.pk, SalesOrderLine.objects.get(order=o).pk)

    def test_order_pages_render(self):
        o = SalesOrder.objects.create(source="manual", order_number="S-3")
        SalesOrderLine.objects.create(order=o, product=self.p, qty=4)
        for url in ("/admin/sales/salesorder/", f"/admin/sales/salesorder/{o.pk}/change/",
                    f"/admin/sales/salesorder/{o.pk}/stock-avail/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        d = self.client.get(f"/admin/sales/salesorder/{o.pk}/stock-avail/").json()
        self.assertEqual((d["lines"][0]["stock"], d["lines"][0]["status"]), (50, "ok"))   # власне списання не нестача
