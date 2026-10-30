"""Склад ↔ замовлення: списання, резерв, скасування, видалення, «Доступно» в усіх режимах.

    python manage.py test inventory.tests_stock --settings=tabele.settings_test
"""
import uuid
from decimal import Decimal as D

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from inventory.models import (
    InventorySettings, InventoryTransaction, Location, Product, ProductComponent,
    PurchaseOrder, PurchaseOrderLine,
)
from inventory.services import stock
from sales.models import SalesOrder, SalesOrderLine

TX = InventoryTransaction.TxType


class StockBase(TestCase):
    def setUp(self):
        self.mode("creation")
        self.loc = Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.p = Product.objects.create(sku="FT220407", name="Filter")
        self.q = Product.objects.create(sku="AN220213", name="Antenna")
        self.receive(self.p, 20)
        self.receive(self.q, 20)
        self._n = 0

    def mode(self, deduct_on, reservation=False, **extra):
        s = InventorySettings.get()
        s.deduct_on, s.use_reservation, s.default_location = deduct_on, reservation, "MAIN"
        for k, v in extra.items():
            setattr(s, k, v)
        s.save()

    def receive(self, product, qty):
        InventoryTransaction.objects.create(tx_type=TX.INCOMING, qty=qty, product=product, location=self.loc,
                                            external_key=f"test:in:{uuid.uuid4()}")

    def order(self, *lines, status="received", affects_stock=True, source="manual"):
        self._n += 1
        o = SalesOrder.objects.create(source=source, order_number=f"{100 + self._n}", status=status,
                                      affects_stock=affects_stock, order_date=timezone.localdate())
        for product, qty in lines:
            SalesOrderLine.objects.create(order=o, product=product, sku_raw=product.sku, qty=qty)
        return o

    def set_status(self, order, status):
        order = SalesOrder.objects.get(pk=order.pk)
        order.status = status
        order.save()
        return order

    def nums(self, product):
        """(на складі, резерв, доступно) — як API /stock/ і огляд складу."""
        p = stock.annotate_stock(Product.objects.filter(pk=product.pk)).get()
        return (p._on_hand, p._reserved, p._available)

    def admin_client(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        return self.client


# ── «Списувати при створенні» (за замовчуванням) ──────────────────────────────

class CreationModeTests(StockBase):
    def test_new_order_deducts_and_is_not_reserved_twice(self):
        self.order((self.p, 5))
        self.assertEqual(self.nums(self.p), (15, 0, 15))   # раніше огляд складу показував доступно 10

    def test_admin_cancel_returns_stock_once(self):
        o = self.order((self.p, 5))
        o = self.set_status(o, "cancelled")
        self.assertEqual(self.nums(self.p), (20, 0, 20))
        o.save()                                    # ще раз «Зберегти» скасоване
        stock.release_order_stock(o)                # і ще раз сервісом
        self.assertEqual(self.nums(self.p), (20, 0, 20))

    def test_cancel_uncancel_cancel_never_double_moves(self):
        o = self.order((self.p, 5), (self.q, 2))
        for status, expected in [("cancelled", 20), ("received", 15), ("cancelled", 20),
                                 ("processing", 15), ("shipped", 15), ("delivered", 15)]:
            o = self.set_status(o, status)
            self.assertEqual(self.nums(self.p)[0], expected, status)
        self.assertEqual(self.nums(self.q), (18, 0, 18))
        # жодної «осиротілої» компенсації: позиція тримає один рух
        self.assertEqual(InventoryTransaction.objects.filter(external_key__startswith="so-cancel:").count(), 0)

    def test_api_and_admin_cancel_agree(self):
        api = APIClient()
        from api.models import APIKey
        key = APIKey.objects.create(name="t", scopes=["orders:read", "orders:write"])
        api.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        o = self.order((self.p, 5))
        self.set_status(o, "cancelled")                          # спершу в адмінці
        r = api.post(f"/api/v1/orders/{o.pk}/cancel/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["restocked_transactions"], 0)  # уже повернуто — вдруге нічого
        self.assertEqual(self.nums(self.p), (20, 0, 20))
        o2 = self.order((self.p, 4))
        r = api.post(f"/api/v1/orders/{o2.pk}/cancel/", {}, format="json")
        self.assertEqual(r.json()["restocked_transactions"], 1)
        self.set_status(o2, "cancelled")
        self.assertEqual(self.nums(self.p), (20, 0, 20))

    def test_line_qty_change_returns_only_the_difference(self):
        o = self.order((self.p, 5))
        line = o.lines.get()
        for qty, on_hand in [(3, 17), (0, 20), (4, 16), (6, 14)]:
            line.qty = qty
            line.save()
            self.assertEqual(self.nums(self.p)[0], on_hand, qty)   # раніше 5 → 3 давало 22 (подвійне повернення)

    def test_line_product_change(self):
        line = self.order((self.p, 5)).lines.get()
        line.product = self.q
        line.save()
        self.assertEqual((self.nums(self.p)[0], self.nums(self.q)[0]), (20, 15))

    def test_delete_line_and_order_return_stock(self):
        o = self.order((self.p, 5), (self.q, 3))
        o.lines.get(product=self.q).delete()
        self.assertEqual(self.nums(self.q), (20, 0, 20))
        self.assertEqual(self.nums(self.p)[0], 15)
        o.delete()
        self.assertEqual(self.nums(self.p), (20, 0, 20))

    def test_deleted_line_is_not_restored_on_uncancel(self):
        o = self.order((self.p, 5), (self.q, 3))
        o = self.set_status(o, "cancelled")
        o.lines.get(product=self.q).delete()
        self.set_status(o, "received")
        self.assertEqual((self.nums(self.p)[0], self.nums(self.q)[0]), (15, 20))

    def test_line_edited_while_cancelled_is_applied_on_uncancel(self):
        o = self.order((self.p, 5))
        o = self.set_status(o, "cancelled")
        line = o.lines.get()
        line.qty = 2
        line.save()
        self.assertEqual(self.nums(self.p)[0], 20)
        self.set_status(o, "processing")
        self.assertEqual(self.nums(self.p)[0], 18)

    def test_cancelling_shipped_order_returns_stock(self):
        o = self.set_status(self.order((self.p, 5)), "shipped")
        self.set_status(o, "cancelled")
        self.assertEqual(self.nums(self.p), (20, 0, 20))

    def test_affects_stock_off_never_touches_stock(self):
        o = self.order((self.p, 5), affects_stock=False)
        self.assertEqual(self.nums(self.p), (20, 0, 20))
        self.set_status(o, "cancelled")
        self.set_status(o, "received")
        self.assertEqual(self.nums(self.p), (20, 0, 20))
        self.assertFalse(InventoryTransaction.objects.filter(external_key__startswith="so").exists())

    def test_source_change_keeps_the_order_stock(self):
        o = self.order((self.p, 5))
        o.source = "webshop"
        o.save()
        self.assertTrue(InventoryTransaction.objects.filter(
            external_key__startswith=f"so:webshop:{o.order_number}:line:").exists())
        self.assertEqual(self.nums(self.p), (15, 0, 15))     # не «загубилось» і не порахувалось ще й резервом
        self.set_status(o, "cancelled")
        self.assertEqual(self.nums(self.p), (20, 0, 20))

    def test_settings_change_is_not_retroactive(self):
        o = self.order((self.p, 5))
        self.mode("shipped")
        self.set_status(o, "processing")         # уже списане назад не повертається
        self.assertEqual(self.nums(self.p), (15, 0, 15))

    def test_order_card_cover_counts_own_deduction(self):
        o = self.order((self.p, 5))
        cover = stock.order_lines_cover(o)[o.lines.get().pk]
        self.assertEqual((cover["available"], cover["held"], cover["free"]), (15, 5, 20))

    def test_legacy_cancelled_order_can_be_released_by_sync(self):
        o = self.order((self.p, 5))
        SalesOrder.objects.filter(pk=o.pk).update(status="cancelled")   # як скасовували до виправлення
        o.refresh_from_db()
        self.assertEqual(self.nums(self.p)[0], 15)
        issues = stock.order_stock_issues(o)
        self.assertEqual(len(issues), 1)
        self.assertIn("ще тримає 5", issues[0]["text"])
        c = self.admin_client()
        r = c.post(f"/admin/sales/salesorder/{o.pk}/sync-inventory/").json()
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.nums(self.p), (20, 0, 20))
        self.assertEqual(stock.order_stock_issues(o), [])
        c.post(f"/admin/sales/salesorder/{o.pk}/sync-inventory/")
        self.assertEqual(self.nums(self.p), (20, 0, 20))                # удруге — нічого

    def test_sync_reports_and_fixes_missing_deduction(self):
        o = self.order((self.p, 5))
        InventoryTransaction.objects.filter(external_key__startswith="so:").delete()   # «розсинхрон»
        issues = stock.order_stock_issues(o)
        self.assertEqual(len(issues), 1)
        self.assertIn("не записано", issues[0]["text"])
        stock.sync_order(o)
        self.assertEqual(self.nums(self.p), (15, 0, 15))
        self.assertEqual(stock.order_stock_issues(o), [])


# ── «Бронювати замовлення» ────────────────────────────────────────────────────

class ReservationModeTests(StockBase):
    def setUp(self):
        super().setUp()
        self.mode("creation", reservation=True)

    def test_reserve_then_ship(self):
        o = self.order((self.p, 5))
        self.assertEqual(self.nums(self.p), (20, 5, 15))
        self.set_status(o, "shipped")
        self.assertEqual(self.nums(self.p), (15, 0, 15))

    def test_cancel_before_ship_removes_reservation_and_uncancel_restores_it(self):
        o = self.set_status(self.order((self.p, 5)), "cancelled")
        self.assertEqual(self.nums(self.p), (20, 0, 20))
        self.assertFalse(InventoryTransaction.objects.filter(tx_type=TX.RESERVED).exists())
        self.set_status(o, "processing")
        self.assertEqual(self.nums(self.p), (20, 5, 15))

    def test_cancel_shipped_then_back_to_processing_is_a_reservation_again(self):
        o = self.set_status(self.order((self.p, 5)), "shipped")
        o = self.set_status(o, "cancelled")
        self.assertEqual(self.nums(self.p), (20, 0, 20))
        self.set_status(o, "processing")
        self.assertEqual(self.nums(self.p), (20, 5, 15))

    def test_api_restock_false_still_releases_reservation(self):
        from api.models import APIKey
        key = APIKey.objects.create(name="t", scopes=["orders:read", "orders:write"])
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        o = self.order((self.p, 5))
        api.post(f"/api/v1/orders/{o.pk}/cancel/", {"restock": False}, format="json")
        self.assertEqual(self.nums(self.p), (20, 0, 20))


# ── «Списувати при Відправлено / Доставлено» ──────────────────────────────────

class ShippedModeTests(StockBase):
    def setUp(self):
        super().setUp()
        self.mode("shipped")

    def test_open_order_is_reserved_until_shipped(self):
        o = self.order((self.p, 5))
        self.assertEqual(self.nums(self.p), (20, 5, 15))     # раніше API показувало доступно 20
        self.assertFalse(InventoryTransaction.objects.filter(external_key__startswith="so:").exists())
        self.set_status(o, "shipped")
        self.assertEqual(self.nums(self.p), (15, 0, 15))

    def test_cancel_never_deducted_order_gives_nothing_back(self):
        o = self.set_status(self.order((self.p, 5)), "cancelled")
        self.assertEqual(self.nums(self.p), (20, 0, 20))
        self.assertFalse(InventoryTransaction.objects.filter(tx_type=TX.ADJUSTMENT).exists())

    def test_jump_to_delivered_deducts(self):
        self.set_status(self.order((self.p, 5)), "delivered")
        self.assertEqual(self.nums(self.p), (15, 0, 15))

    def test_cancel_shipped_and_reopen(self):
        o = self.set_status(self.order((self.p, 5)), "shipped")
        o = self.set_status(o, "cancelled")
        self.assertEqual(self.nums(self.p), (20, 0, 20))
        o = self.set_status(o, "received")                    # знову чекає відправки — лише бронь
        self.assertEqual(self.nums(self.p), (20, 5, 15))
        self.set_status(o, "shipped")
        self.assertEqual(self.nums(self.p), (15, 0, 15))

    def test_order_created_already_shipped_is_deducted(self):
        self.order((self.p, 5), status="shipped")
        self.assertEqual(self.nums(self.p), (15, 0, 15))

    def test_bulk_status_action_deducts_and_cancel_action_returns(self):
        c = self.admin_client()
        o = self.order((self.p, 5))
        url = "/admin/sales/salesorder/"
        r = c.post(url, {"action": "action_set_status_shipped", "_selected_action": [o.pk]})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.nums(self.p), (15, 0, 15))
        c.post(url, {"action": "action_set_status_cancelled", "_selected_action": [o.pk]})
        self.assertEqual(self.nums(self.p), (20, 0, 20))


class DeliveredModeTests(StockBase):
    def test_shipped_is_still_reserved_until_delivered(self):
        self.mode("delivered")
        o = self.set_status(self.order((self.p, 5)), "shipped")
        self.assertEqual(self.nums(self.p), (20, 5, 15))
        self.set_status(o, "delivered")
        self.assertEqual(self.nums(self.p), (15, 0, 15))


# ── Одні й ті самі числа всюди ────────────────────────────────────────────────

class SameNumbersEverywhereTests(StockBase):
    def setUp(self):
        super().setUp()
        self.mode("shipped")
        self.order((self.p, 5))                                  # ще не списане → у резерві
        self.set_status(self.order((self.p, 3)), "shipped")      # списане → лише на складі
        self.mode("creation", reservation=True)
        self.order((self.p, 2))                                  # 🔒 резерв
        # на складі 17, резерв 5 + 2 = 7, доступно 10

    def test_service_api_overview_analysis_offers(self):
        self.assertEqual(self.nums(self.p), (17, 7, 10))
        self.assertEqual(stock.get_available(self.p), 10)
        self.assertEqual(stock.check_availability([{"sku": "FT220407", "qty": 10}])[0]["ok"], True)
        self.assertEqual(stock.check_availability([{"sku": "FT220407", "qty": 11}])[0]["ok"], False)
        main = [r for r in stock.location_breakdown(self.p) if r["location"] == "MAIN"][0]
        self.assertEqual((main["on_hand"], main["reserved"], main["available"]), (17, 7, 10))

        from api.models import APIKey
        key = APIKey.objects.create(name="t", scopes=["stock:read"])
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        d = api.get("/api/v1/stock/FT220407/").json()
        self.assertEqual((d["on_hand"], d["reserved"], d["available"]), (17, 7, 10))

        c = self.admin_client()
        rows = {r["product"].pk: r for r in c.get("/admin/inventory/reorderproxy/supply/").context["rows"]}
        self.assertEqual((rows[self.p.pk]["stock"], rows[self.p.pk]["reserved"], rows[self.p.pk]["net_avail"]),
                         (17, 7, 10))
        states = sorted(r["state"] for r in rows[self.p.pk]["reservations"])
        self.assertEqual(states, ["pending", "reserved"])          # відправлене списане не показується

        cl = c.get("/admin/inventory/reorderproxy/").context["cl"]
        obj = [o for o in cl.result_list if o.pk == self.p.pk][0]
        self.assertEqual((obj._stock_total, obj._reserved_total, obj._available), (17, 7, 10))

        from offers.pricing import quote_lead       # Angebot: «ab Lager», лише якщо доступно вистачає
        ok, short = quote_lead(self.p, 10, "de"), quote_lead(self.p, 11, "de")
        self.assertEqual((ok.source, bool(ok.text)), ("stock", True))
        self.assertIn("на складі 10 з 11", short.note)

    def test_digikey_quantity_is_available(self):
        from bots.models import DigiKeyListing
        listing = DigiKeyListing.objects.create(product=self.p, dk_offer_id="o-1", dk_product_id="p-1",
                                                sync_status="published", dk_title="DK")
        self.assertEqual((listing.get_stock_qty(), listing.get_dk_quantity()), (10, 10))   # не 17 і не 15
        c = self.admin_client()
        self.assertEqual(c.get("/admin/bots/digikeylisting/").status_code, 200)
        self.assertEqual(c.get("/admin/bots/digikeylisting/sku-check/?sku=FT220407").json()["qty"], 10)
        qs = __import__("bots.admin", fromlist=["x"]).admin.site._registry[DigiKeyListing].get_queryset(None)
        self.assertEqual(qs.get(pk=listing.pk)._stock_qty, 10)    # сортування колонки «Склад»

    def test_ai_assistant_tool_uses_available(self):
        from ai_assistant.tools import _stock_map_for_products
        self.assertEqual(_stock_map_for_products([self.p.pk])[self.p.pk], 10.0)   # раніше продаж додавався

    def test_inventory_start_page_counts_available(self):
        Product.objects.filter(pk=self.p.pk).update(reorder_point=10)   # доступно 10 ≤ 10
        c = self.admin_client()
        stats = c.get("/admin/inventory/").context["inventory_stats"]
        self.assertEqual(stats["critical"], 1)


# ── Що замовити: точка дозамовлення й уже замовлене ───────────────────────────

class ReorderAdviceTests(TestCase):
    def test_classic_example_from_guide(self):
        d = stock.reorder_advice(available=40, monthly=40)
        self.assertEqual((d["months_left"], d["reorder_qty"], d["status"]), (1.0, 100, "warning"))

    def test_reorder_point_without_sales(self):
        d = stock.reorder_advice(available=3, monthly=0, reorder_point=10)
        self.assertEqual((d["status"], d["reorder_qty"], d["below_reorder_point"]), ("warning", 7, True))
        d = stock.reorder_advice(available=15, monthly=0, reorder_point=10)
        self.assertEqual((d["status"], d["reorder_qty"]), ("no_sales", 0))
        self.assertEqual(stock.reorder_advice(available=0, monthly=0, reorder_point=10)["status"], "critical")

    def test_reorder_point_is_a_floor_and_raises_status(self):
        # продажі малі (ціль 3,6 шт.), але точка дозамовлення 20 — замовити до 20
        d = stock.reorder_advice(available=12, monthly=1, reorder_point=20)
        self.assertEqual((d["status"], d["reorder_qty"]), ("warning", 8))
        d = stock.reorder_advice(available=6, monthly=1, reorder_point=20)
        self.assertGreaterEqual(6 + d["reorder_qty"], 20)        # округлення не опускає нижче точки

    def test_already_ordered_is_subtracted(self):
        self.assertEqual(stock.reorder_advice(available=40, monthly=40, incoming=110)["reorder_qty"], 0)
        self.assertEqual(stock.reorder_advice(available=40, monthly=40, incoming=100)["reorder_qty"], 4)
        self.assertEqual(stock.reorder_advice(available=40, monthly=40, incoming=50)["reorder_qty"], 50)

    def test_no_sales_no_point(self):
        self.assertEqual(stock.reorder_advice(available=5, monthly=0)["status"], "no_sales")


class ReorderAnalysisPageTests(StockBase):
    def test_analysis_uses_reorder_point_and_open_purchases(self):
        Product.objects.filter(pk=self.q.pk).update(reorder_point=30)   # продажів немає, доступно 20
        po = PurchaseOrder.objects.create(status="ordered")
        PurchaseOrderLine.objects.create(purchase_order=po, product=self.q, qty_ordered=4)
        c = self.admin_client()
        cl = c.get("/admin/inventory/reorderproxy/").context["cl"]
        admin_obj = cl.model_admin
        obj = [o for o in cl.result_list if o.pk == self.q.pk][0]
        d = admin_obj._get_reorder_cached(obj)
        self.assertEqual((d["status"], d["reorder_qty"], d["incoming"]), ("warning", 6, 4))
        r = c.get("/admin/inventory/reorderproxy/?reorder_status=warning")
        self.assertIn(self.q.pk, [o.pk for o in r.context["cl"].result_list])

    def test_cancelled_orders_are_not_sales(self):
        o = self.order((self.p, 9))
        self.set_status(o, "cancelled")
        from inventory.admin import _get_monthly_sales
        self.assertEqual(_get_monthly_sales(self.p), 0)

    def test_sales_source_filter_works(self):
        self.order((self.p, 6), source="digikey")
        self.order((self.p, 3), source="manual")
        c = self.admin_client()
        cl = c.get("/admin/inventory/reorderproxy/?sales_source=digikey").context["cl"]
        obj = [o for o in cl.result_list if o.pk == self.p.pk][0]
        self.assertEqual(obj._sales_period, 6)                   # раніше фільтр ламав весь розрахунок


# ── «Попередження про низький залишок» ────────────────────────────────────────

class LowStockFlagTests(StockBase):
    def setUp(self):
        super().setUp()
        Product.objects.filter(pk=self.q.pk).update(reorder_point=25)   # доступно 20 ≤ 25

    def test_flag_drives_signals_page_and_dashboard(self):
        self.assertIn(self.q.pk, [d["pk"] for d in stock.low_stock_items()])
        c = self.admin_client()
        self.assertContains(c.get("/dashboard/signals/"), "Нижче точки дозамовлення: AN220213")
        self.mode("creation", low_stock_alert_enabled=False)
        self.assertEqual(stock.low_stock_items(), [])
        self.assertNotContains(c.get("/dashboard/signals/"), "AN220213")
        self.assertTrue(c.get("/dashboard/").context["stock_alerts_off"])


# ── Ручні рухи: знак кількості ────────────────────────────────────────────────

class ManualTransactionSignTests(StockBase):
    def post_tx(self, tx_type, qty):
        c = self.admin_client() if not hasattr(self, "_c") else self._c
        self._c = c
        r = c.post("/admin/inventory/inventorytransaction/add/", {
            "tx_type": tx_type, "qty": qty, "product": self.p.pk, "location": self.loc.pk,
            "ref_doc": "test", "tx_date_0": "2026-10-29", "tx_date_1": "10:00:00",
        })
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["adminform"].form.errors)
        return InventoryTransaction.objects.filter(ref_doc="test").latest("pk")

    def test_outgoing_positive_is_stored_negative(self):
        self.assertEqual(self.post_tx("Outgoing", "5").qty, D("-5"))
        self.assertEqual(self.nums(self.p)[0], 15)

    def test_incoming_negative_is_stored_positive_and_adjustment_keeps_sign(self):
        self.assertEqual(self.post_tx("Incoming", "-3").qty, D("3"))
        self.assertEqual(self.post_tx("Adjustment", "-2").qty, D("-2"))
        self.assertEqual(self.post_tx("Reserved", "4").qty, D("-4"))

    def test_type_change_actions_fix_sign(self):
        c = self.admin_client()
        tx = InventoryTransaction.objects.create(tx_type=TX.OUTGOING, qty=-5, product=self.p, location=self.loc,
                                                 external_key="manual:x")
        c.post("/admin/inventory/inventorytransaction/", {"action": "mark_as_incoming", "_selected_action": [tx.pk]})
        tx.refresh_from_db()
        self.assertEqual((tx.tx_type, tx.qty), (TX.INCOMING, D("5")))
        c.post("/admin/inventory/inventorytransaction/", {"action": "release_reservations",
                                                           "_selected_action": [tx.pk]})
        tx.refresh_from_db()
        self.assertEqual((tx.tx_type, tx.qty), (TX.OUTGOING, D("-5")))

    def test_service_movements_unchanged(self):
        tx, _ = stock.create_movement(product=self.p, tx_type=TX.OUTGOING, qty=2)
        self.assertEqual(tx.qty, D("-2"))
        tx, _ = stock.create_movement(product=self.p, tx_type=TX.ADJUSTMENT, qty=-1)
        self.assertEqual(tx.qty, D("-1"))


class InventoryStartPageTests(StockBase):
    def test_tiles_link_to_real_types(self):
        c = self.admin_client()
        html = c.get("/admin/inventory/").content.decode()
        for t in ("Incoming", "Outgoing", "Adjustment"):
            self.assertIn(f"/admin/inventory/inventorytransaction/?tx_type__exact={t}", html)
        self.assertNotIn("tx_type=RECEIPT", html)
        self.order((self.p, 5))
        r = c.get("/admin/inventory/inventorytransaction/?tx_type__exact=Incoming")
        self.assertEqual(r.status_code, 200)
        self.assertEqual({t.tx_type for t in r.context["cl"].result_list}, {TX.INCOMING})


class LabelsTests(TestCase):
    def test_ukrainian_labels(self):
        f = Product._meta.get_field
        self.assertEqual((f("sku").verbose_name, f("kind").verbose_name, f("bom_type").verbose_name),
                         ("Артикул (SKU)", "Тип товару", "BOM (комплектація)"))
        t = InventoryTransaction._meta.get_field
        self.assertEqual((t("tx_type").verbose_name, t("qty").verbose_name), ("Тип руху", "Кількість"))
        self.assertEqual(SalesOrder._meta.verbose_name_plural, "Замовлення")
        s = SalesOrder._meta.get_field
        self.assertEqual((s("source").verbose_name, s("order_number").verbose_name,
                          s("shipping_deadline").verbose_name, s("affects_stock").verbose_name),
                         ("Джерело", "Номер замовлення", "Дедлайн відправки", "Впливає на склад"))


# ── BOM: компоненти при «Відправлено» ─────────────────────────────────────────

class BomTests(StockBase):
    def test_reship_and_cancel_components(self):
        comp_loc = Location.objects.create(code="COMP", location_type=Location.LocationType.COMPONENTS)
        kit = Product.objects.create(sku="KIT-1", bom_type=Product.BomType.KEY)
        part = Product.objects.create(sku="PART-1", kind=Product.Kind.COMPONENT)
        InventoryTransaction.objects.create(tx_type=TX.INCOMING, qty=10, product=part, location=comp_loc,
                                            external_key="in:part")
        ProductComponent.objects.create(parent=kit, component=part, qty_per=2)
        o = self.set_status(self.order((kit, 1)), "shipped")
        self.assertEqual(self.nums(part)[0], 8)
        o = self.set_status(o, "processing")
        o = self.set_status(o, "shipped")                     # ще раз «Відправлено» — без дубля й помилки
        self.assertEqual(self.nums(part)[0], 8)
        o = self.set_status(o, "cancelled")
        self.assertEqual(self.nums(part)[0], 10)
        self.set_status(o, "shipped")
        self.assertEqual(self.nums(part)[0], 8)
