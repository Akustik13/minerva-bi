"""Склад ↔ замовлення: виправлення аудиту 10.2026 — BOM, блокування, сироти, «очікується», резерви, обходи сервісу.

    python manage.py test inventory.tests_stock_audit --settings=tabele.settings_test

Числа в перевірках — (на складі, резерв, доступно), як API /stock/ і огляд складу.
"""
import json
import uuid
from decimal import Decimal as D
from io import StringIO
from types import SimpleNamespace
from unittest import mock

import pandas as pd
from django.core.management import call_command
from rest_framework.test import APIClient

from inventory.models import (
    InventoryTransaction, Location, Product, ProductAlias, ProductComponent, PurchaseOrder, PurchaseOrderLine,
)
from inventory.services import stock
from inventory.tests_stock import StockBase
from sales.models import SalesOrder, SalesOrderLine

TX = InventoryTransaction.TxType


class BomBase(StockBase):
    """Виріб KIT-1 (власний залишок 10, BOM: 2 × PART-1) і компонент PART-1 (10 на складі компонентів)."""

    def setUp(self):
        super().setUp()
        self.comp_loc = Location.objects.create(code="COMP", location_type=Location.LocationType.COMPONENTS)
        self.kit = Product.objects.create(sku="KIT-1", bom_type=Product.BomType.KEY)
        self.part = Product.objects.create(sku="PART-1", kind=Product.Kind.COMPONENT)
        self.receive(self.kit, 10)
        InventoryTransaction.objects.create(tx_type=TX.INCOMING, qty=10, product=self.part, location=self.comp_loc,
                                            external_key=f"test:in:{uuid.uuid4()}")
        ProductComponent.objects.create(parent=self.kit, component=self.part, qty_per=2)

    def part_on_hand(self):
        return self.nums(self.part)[0]


# ── 1. Замовлення з виробом BOM не губить синхронізацію складу при «Відправлено» ──

class BomOrderShippedTests(BomBase):
    def test_shipped_mode_deducts_kit_and_other_lines(self):
        self.mode("shipped")
        o = self.order((self.kit, 2), (self.p, 3))
        self.assertEqual(self.nums(self.kit), (10, 2, 8))
        self.set_status(o, "shipped")
        self.assertEqual(self.nums(self.kit), (8, 0, 8))      # раніше (10, 2, 8): вкладений save() у сигналі BOM
        self.assertEqual(self.nums(self.p), (17, 0, 17))      # раніше (20, 3, 17)
        self.assertEqual(self.part_on_hand(), 6)

    def test_reservation_mode_converts_reservations(self):
        self.mode("creation", reservation=True)
        o = self.order((self.kit, 2), (self.p, 3))
        self.assertEqual((self.nums(self.kit), self.nums(self.p)), ((10, 2, 8), (20, 3, 17)))
        self.assertEqual(self.part_on_hand(), 10)             # компоненти не бронюються — списуються з виробом
        self.set_status(o, "shipped")
        self.assertEqual((self.nums(self.kit), self.nums(self.p)), ((8, 0, 8), (17, 0, 17)))
        self.assertEqual(self.part_on_hand(), 6)

    def test_note_is_written_without_resaving_the_order(self):
        self.mode("shipped")
        o = self.set_status(self.order((self.kit, 2)), "shipped")
        o.refresh_from_db()
        self.assertIn("✅ БОМ: вилучено компоненти для KIT-1", o.internal_note)
        self.assertIn("PART-1: 10→6", o.internal_note)

    def test_other_receiver_saving_note_does_not_hide_status_change(self):
        """Інший сигнал post_save, що спрацьовує раніше за склад, зберігає замовлення ще раз (лише нотатку й
        повністю) — склад однаково бачить зміну статусу зовнішнього збереження."""
        from django.db.models.signals import post_save
        from inventory import signals as inv
        self.mode("shipped")
        o = self.order((self.p, 3))

        def nested(sender, instance, **kw):
            if instance.status == "shipped" and "nested" not in (instance.internal_note or ""):
                instance.internal_note = "nested"
                instance.save(update_fields=["internal_note"])
                instance.save()                       # і повне збереження теж

        def reconnect_stock():                        # сигнал складу — після тестового, як колись після BOM-сигналу
            post_save.disconnect(sender=SalesOrder, dispatch_uid="inventory_salesorder_deduct")
            post_save.connect(inv._salesorder_post_save, sender=SalesOrder,
                              dispatch_uid="inventory_salesorder_deduct", weak=False)

        post_save.connect(nested, sender=SalesOrder, dispatch_uid="test_nested_save")
        reconnect_stock()
        try:
            self.set_status(o, "shipped")
        finally:
            post_save.disconnect(sender=SalesOrder, dispatch_uid="test_nested_save")
        self.assertEqual(self.nums(self.p), (17, 0, 17))


# ── 2. Компоненти BOM — за тими самими правилами, що й позиція ────────────────

class BomComponentsFollowLineTests(BomBase):
    def test_order_created_already_shipped(self):
        self.mode("shipped")
        self.order((self.kit, 2), status="shipped")           # позиції — після замовлення (API, DigiKey, Excel)
        self.assertEqual(self.part_on_hand(), 6)               # раніше 10

    def test_jump_straight_to_delivered(self):
        self.mode("shipped")
        o = self.order((self.kit, 2))
        self.assertEqual(self.part_on_hand(), 10)
        self.set_status(o, "delivered")                       # минаючи «Відправлено»
        self.assertEqual(self.part_on_hand(), 6)               # раніше 10

    def test_deduct_on_delivered_mode(self):
        self.mode("delivered")
        o = self.set_status(self.order((self.kit, 2)), "shipped")
        self.assertEqual(self.part_on_hand(), 10)              # сам виріб ще не списано — компоненти теж
        self.set_status(o, "delivered")
        self.assertEqual(self.part_on_hand(), 6)

    def test_creation_mode_with_the_line(self):
        self.order((self.kit, 2))
        self.assertEqual((self.nums(self.kit)[0], self.part_on_hand()), (8, 6))

    def test_cancel_uncancel_delete_return_components_once(self):
        self.mode("shipped")
        o = self.set_status(self.order((self.kit, 2)), "shipped")
        self.assertEqual(self.part_on_hand(), 6)
        o = self.set_status(o, "cancelled")
        self.assertEqual(self.part_on_hand(), 10)
        o = self.set_status(o, "delivered")
        self.assertEqual(self.part_on_hand(), 6)
        o = self.set_status(o, "processing")                  # уже списане назад не повертається
        self.assertEqual(self.part_on_hand(), 6)
        o.delete()
        self.assertEqual((self.part_on_hand(), self.nums(self.kit)[0]), (10, 10))

    def test_cancel_before_shipping_and_reopen_in_shipped_mode(self):
        self.mode("shipped")
        o = self.set_status(self.order((self.kit, 2)), "shipped")
        o = self.set_status(o, "cancelled")
        o = self.set_status(o, "received")                    # знову чекає відправки — компонентів не тримає
        self.assertEqual(self.part_on_hand(), 10)
        self.set_status(o, "shipped")
        self.assertEqual(self.part_on_hand(), 6)

    def test_line_qty_change_delete_and_product_change(self):
        o = self.order((self.kit, 2))
        line = o.lines.get()
        line.qty = 3
        line.save()
        self.assertEqual(self.part_on_hand(), 4)               # на різницю, без подвійного списання
        line.product = self.p
        line.save()
        self.assertEqual((self.part_on_hand(), self.nums(self.kit)[0]), (10, 10))   # інший товар — компоненти назад
        line.product = self.kit
        line.save()
        self.assertEqual(self.part_on_hand(), 4)
        line.delete()
        self.assertEqual((self.part_on_hand(), self.nums(self.kit)[0]), (10, 10))

    def test_two_lines_of_the_same_kit_each_take_components(self):
        self.order((self.kit, 2), (self.kit, 1))
        self.assertEqual(self.part_on_hand(), 4)               # раніше друга позиція компонентів не списувала

    def test_not_enough_components_waits_and_notes_once(self):
        o = self.order((self.kit, 6))                          # треба 12, є 10
        self.assertEqual(self.part_on_hand(), 10)
        o.refresh_from_db()
        self.assertEqual(o.internal_note.count("⚠️ БОМ KIT-1: компоненти не списано"), 1)
        issues = stock.order_stock_issues(o)
        self.assertIn("компонент BOM PART-1 не списано (12)", " ".join(i["text"] for i in issues))
        self.set_status(o, "processing")
        o.refresh_from_db()
        self.assertEqual(o.internal_note.count("⚠️ БОМ KIT-1"), 1)  # подія повторюється — нотатка одна
        InventoryTransaction.objects.create(tx_type=TX.INCOMING, qty=5, product=self.part, location=self.comp_loc,
                                            external_key="test:in:more")
        stock.sync_order(o)                                    # «🔄 Синхронізувати склад»
        self.assertEqual(self.part_on_hand(), 3)
        self.assertEqual(stock.order_stock_issues(o), [])

    def test_legacy_bom_key_is_adopted_not_doubled(self):
        """Замовлення, відправлене до 10.2026: компоненти списані ключем bom_deduction_<виріб>_<замовлення>_<…>."""
        self.mode("shipped")
        o = self.order((self.kit, 2))
        InventoryTransaction.objects.create(
            tx_type=TX.OUTGOING, qty=-4, product=self.part, location=self.comp_loc,
            external_key=f"bom_deduction_{self.kit.pk}_{o.pk}_{self.part.pk}")
        o = self.set_status(o, "shipped")
        self.assertEqual(self.part_on_hand(), 6)               # не 2
        line = o.lines.get()
        line.qty = 3
        line.save()
        self.assertEqual(self.part_on_hand(), 4)
        self.set_status(o, "cancelled")
        self.assertEqual(self.part_on_hand(), 10)

    def test_api_order_with_kit_already_shipped(self):
        from api.models import APIKey
        self.mode("shipped")
        key = APIKey.objects.create(name="t", scopes=["orders:read", "orders:write"], default_source="crm")
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        r = api.post("/api/v1/orders/", {"order_number": "API-KIT", "status": "shipped", "client": "X",
                                         "lines": [{"sku": "KIT-1", "qty": 2}]}, format="json")
        self.assertIn(r.status_code, (200, 201), r.content)
        self.assertEqual((self.nums(self.kit)[0], self.part_on_hand()), (8, 6))


# ── 3. DigiKey Marketplace: авто-підтвердження «є на складі» ──────────────────

class DigiKeyAutoConfirmInStockTests(StockBase):
    def process(self, qty, number="DK-777"):
        from bots.services import digikey
        order = {"businessId": number, "orderState": "New", "createDateUtc": "2026-10-29T10:00:00Z",
                 "customer": {"firstName": "Ann", "lastName": "Lee", "customerEmail": "ann@example.com",
                              "shippingAddress": {"countryCode": "DE", "city": "Berlin", "street1": "Str. 1",
                                                  "postalCode": "10115"}},
                 "orderDetails": [{"supplierSku": "FT220407", "quantity": qty, "unitPrice": 1, "totalPrice": qty}]}
        stats = {"created": 0, "updated": 0, "skipped": 0, "changes": [], "lines_created": 0,
                 "lines_skipped": 0, "unmatched_skus": []}
        config = SimpleNamespace(auto_confirm_mode="in_stock", locale_currency="EUR")
        with mock.patch.object(digikey, "confirm_marketplace_order", return_value={"ok": True}) as confirm, \
                mock.patch("dashboard.notifications.notify_digikey_auto_confirmed"), \
                mock.patch("dashboard.notifications.send_order_confirm_notification"):
            digikey._process_marketplace_order(order, stats, config)   # раніше FieldError «quantity» → відкат
        return confirm, SalesOrder.objects.get(source="digikey", order_number=number)

    def test_all_in_stock_is_confirmed_counting_own_deduction(self):
        confirm, sale = self.process(20)                       # усе, що є: своє списання — не нестача
        self.assertTrue(confirm.called)
        self.assertEqual(sale.lines.count(), 1)
        self.assertEqual(self.nums(self.p), (0, 0, 0))

    def test_not_enough_is_imported_but_not_confirmed(self):
        confirm, sale = self.process(21)
        self.assertFalse(confirm.called)
        self.assertEqual(sale.status, "received")


# ── 4. Повторний авто-імпорт зі зміненою кількістю ────────────────────────────

class AutoImportLinesTests(StockBase):
    def run_import(self, rows, strategy="update"):
        from autoimport.importers import import_sales
        return import_sales(pd.DataFrame(rows), source="manual", conflict_strategy=strategy)

    def test_changed_qty_updates_the_same_line(self):
        self.run_import([{"order_number": "IMP-1", "sku": "FT220407", "qty": 5}])
        self.assertEqual(self.nums(self.p), (15, 0, 15))
        self.run_import([{"order_number": "IMP-1", "sku": "FT220407", "qty": 3}])
        o = SalesOrder.objects.get(order_number="IMP-1")
        self.assertEqual([l.qty for l in o.lines.all()], [3])   # раніше [5, 3]
        self.assertEqual(self.nums(self.p), (17, 0, 17))        # раніше (12, 0, 12)
        self.run_import([{"order_number": "IMP-1", "sku": "FT220407", "qty": 3}])
        self.assertEqual((o.lines.count(), self.nums(self.p)), (1, (17, 0, 17)))

    def test_all_rows_of_a_new_order_become_lines(self):
        self.run_import([{"order_number": "IMP-2", "sku": "FT220407", "qty": 2},
                         {"order_number": "IMP-2", "sku": "AN220213", "qty": 1},
                         {"order_number": "IMP-2", "sku": "FT220407", "qty": 4}], strategy="skip")
        o = SalesOrder.objects.get(order_number="IMP-2")
        self.assertEqual(sorted((l.product.sku, l.qty) for l in o.lines.all()),
                         [("AN220213", 1), ("FT220407", 2), ("FT220407", 4)])
        self.assertEqual(self.nums(self.p)[0], 14)


# ── 5–6. Закупівлі: переотримана позиція ──────────────────────────────────────

class OverReceiptTests(StockBase):
    def test_supply_receive_does_not_close_po_by_one_over_delivered_line(self):
        po = PurchaseOrder.objects.create(status="ordered")
        a = PurchaseOrderLine.objects.create(purchase_order=po, product=self.p, qty_ordered=10)
        PurchaseOrderLine.objects.create(purchase_order=po, product=self.q, qty_ordered=10)
        c = self.admin_client()
        r = c.post("/admin/inventory/reorderproxy/supply/receive/", json.dumps({"line_pk": a.pk, "qty": 20}),
                   content_type="application/json")
        self.assertEqual(r.json()["po_status"], "partial")         # раніше «received» (20 ≥ 20 разом)
        po.refresh_from_db()
        self.assertEqual(po.status, "partial")
        self.assertEqual(self.nums(self.p)[0], 40)

    def test_incoming_is_never_negative_per_line(self):
        po = PurchaseOrder.objects.create(status="ordered")
        over = PurchaseOrderLine.objects.create(purchase_order=po, product=self.q, qty_ordered=10)
        PurchaseOrderLine.objects.create(purchase_order=po, product=self.p, qty_ordered=5)
        over.qty_received = 12
        over.save()
        PurchaseOrder.objects.filter(pk=po.pk).update(status="partial")
        po2 = PurchaseOrder.objects.create(status="ordered")
        PurchaseOrderLine.objects.create(purchase_order=po2, product=self.q, qty_ordered=5)
        q = stock.annotate_stock(Product.objects.filter(pk=self.q.pk)).get()
        self.assertEqual(q._incoming, 5)                          # раніше 3 (−2 + 5)
        from inventory.admin import _annotate_reorder, _confirmed_incoming
        from django.utils import timezone
        self.assertEqual(_confirmed_incoming(self.q), 5.0)
        row = _annotate_reorder(Product.objects.filter(pk=self.q.pk), timezone.localdate()).get()
        self.assertEqual(row._confirmed_incoming, 5)
        Product.objects.filter(pk=self.q.pk).update(reorder_point=100)
        item = [d for d in stock.low_stock_items() if d["pk"] == self.q.pk][0]
        self.assertEqual(item["incoming"], 5.0)
        from api.models import APIKey
        key = APIKey.objects.create(name="t", scopes=["stock:read"])
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        self.assertEqual(api.get("/api/v1/stock/AN220213/").json()["incoming"], 5)


# ── 7. Одночасні збереження: стан замовлення — з бази під блокуванням ─────────

class LockedFreshStateTests(StockBase):
    def test_stale_instance_does_not_deduct_cancelled_order(self):
        self.mode("shipped")
        o = self.order((self.p, 5))
        stale = SalesOrder.objects.get(pk=o.pk)
        stale.status = "shipped"                                # «перший» запит ще не дійшов до складу
        SalesOrder.objects.filter(pk=o.pk).update(status="cancelled")   # «другий» уже скасував
        self.assertEqual(stock.sync_order(stale), [])           # раніше списувало 5 у скасованому замовленні
        self.assertEqual(self.nums(self.p), (20, 0, 20))
        self.assertEqual(stale.status, "shipped")               # об'єкт того, хто викликав, не змінено

    def test_stale_affects_flag_is_re_read(self):
        o = self.order((self.p, 5), affects_stock=False)
        stale = SalesOrder.objects.get(pk=o.pk)
        stale.affects_stock = True
        self.assertEqual(stock.sync_order(stale), [])
        self.assertEqual(self.nums(self.p), (20, 0, 20))

    def test_restore_reads_fresh_status(self):
        o = self.set_status(self.order((self.p, 5)), "cancelled")
        stale = SalesOrder.objects.get(pk=o.pk)
        stale.status = "received"
        self.assertEqual(stock.restore_order_stock(stale), [])  # у базі ще «Скасовано» — нічого не списується
        self.assertEqual(self.nums(self.p), (20, 0, 20))

    def test_order_row_is_locked(self):
        """sync_order / release / restore / release_line блокують рядок замовлення (SELECT … FOR UPDATE; SQLite
        його ігнорує, PostgreSQL — друге збереження чекає першого)."""
        o = self.order((self.p, 5))
        manager = SalesOrder.objects
        with mock.patch.object(manager, "select_for_update", wraps=manager.select_for_update) as lock:
            stock.sync_order(o)
            self.assertEqual(lock.call_count, 1)
            stock.restore_order_stock(o)
            stock.release_line_stock(o.lines.get())
            stock.release_order_stock(o)
        self.assertGreaterEqual(lock.call_count, 4)


# ── 8. Скасування не повертає сироти старого замовлення з тим самим номером ───

class OrphanMovementsTests(StockBase):
    def test_cancel_returns_only_current_lines(self):
        o = self.order((self.p, 5))
        InventoryTransaction.objects.create(           # залишок старого замовлення з тим самим джерелом і номером
            tx_type=TX.OUTGOING, qty=-4, product=self.p, location=self.loc,
            external_key=f"so:manual:{o.order_number}:line:9999:{uuid.uuid4()}")
        self.assertEqual(self.nums(self.p)[0], 11)
        o = self.set_status(o, "cancelled")
        self.assertEqual(self.nums(self.p), (16, 0, 16))       # раніше (20, 0, 20)
        self.assertEqual(stock.order_stock_issues(o), [])
        self.set_status(o, "received")
        self.assertEqual(self.nums(self.p)[0], 11)

    def test_order_delete_still_returns_its_lines(self):
        o = self.order((self.p, 5), (self.q, 2))
        InventoryTransaction.objects.create(
            tx_type=TX.OUTGOING, qty=-4, product=self.p, location=self.loc,
            external_key=f"so:manual:{o.order_number}:line:9999:{uuid.uuid4()}")
        o.delete()
        self.assertEqual((self.nums(self.p)[0], self.nums(self.q)[0]), (16, 20))


# ── 10. Залишки по локаціях: резерв ще не списаних замовлень не губиться ──────

class LocationBreakdownTests(StockBase):
    def test_pending_reserved_shows_on_default_location_without_rows(self):
        other = Location.objects.create(code="B-SHELF", name="Полиця B",
                                        location_type=Location.LocationType.FINISHED)
        p = Product.objects.create(sku="SHELF-ONLY")
        InventoryTransaction.objects.create(tx_type=TX.INCOMING, qty=20, product=p, location=other,
                                            external_key="test:shelf")
        self.mode("shipped")
        self.order((p, 5))
        rows = {r["location"]: r for r in stock.location_breakdown(p)}
        self.assertEqual((rows["B-SHELF"]["on_hand"], rows["B-SHELF"]["reserved"]), (20, 0))
        self.assertEqual((rows["MAIN"]["on_hand"], rows["MAIN"]["reserved"], rows["MAIN"]["available"]), (0, 5, -5))
        self.assertEqual(sum(r["reserved"] for r in rows.values()), self.nums(p)[1])


# ── 11. «Списувати при Доставлено»: відправлене замовлення не показує нестачу свого товару ──

class ShippedCoverTests(StockBase):
    def test_shipped_not_yet_deducted_counts_own_goods(self):
        self.mode("delivered")
        p = Product.objects.create(sku="EXACT-5")
        self.receive(p, 5)
        o = self.set_status(self.order((p, 5)), "shipped")
        cover = stock.order_lines_cover(o)[o.lines.get().pk]
        self.assertEqual((cover["available"], cover["held"], cover["free"]), (0, 5, 5))   # раніше free 0
        c = self.admin_client()
        r = c.get("/admin/sales/salesorder/")
        data = json.loads(r.context_data["sv_order_stock_json"])
        self.assertEqual(data[str(o.pk)][0]["stock"], 5)

    def test_shipped_and_deducted_is_unchanged(self):
        o = self.set_status(self.order((self.p, 5)), "shipped")
        cover = stock.order_lines_cover(o)[o.lines.get().pk]
        self.assertEqual((cover["available"], cover["held"], cover["free"]), (15, 0, 15))


# ── 12. Обходи сервісу: «Звірка зі складом» DigiKey, check_tasks ──────────────

class ServiceBypassTests(StockBase):
    def test_digikey_inventory_check_shows_what_goes_to_digikey(self):
        from bots.models import DigiKeyListing
        self.mode("shipped")
        self.order((self.p, 5))                                  # ще не списано — у резерві
        listing = DigiKeyListing.objects.create(product=self.p, dk_offer_id="o-1", dk_product_id="p-1",
                                                sync_status="published", dk_title="DK")
        c = self.admin_client()
        items = c.get("/admin/bots/digikeylisting/inventory-check/").context["items"]
        self.assertEqual(items[0]["stock"], listing.get_stock_qty())
        self.assertEqual(items[0]["stock"], 15)                  # раніше 20

    def test_check_tasks_uses_available_and_reorder_point_inclusive(self):
        from tasks.models import Task
        self.mode("shipped")
        Product.objects.filter(pk=self.p.pk).update(reorder_point=15)
        self.order((self.p, 5))                                  # доступно 15 ≤ 15
        call_command("check_tasks", stdout=StringIO())
        self.assertTrue(Task.objects.filter(product=self.p, task_type=Task.TaskType.STOCK_ALERT,
                                            status=Task.Status.PENDING).exists())   # раніше: на складі 20 — ні


# ── 13. API: скасування без повернення на склад ───────────────────────────────

class ApiCancelWithoutRestockTests(StockBase):
    def test_writeoff_is_not_offered_back_and_not_returned_on_delete(self):
        from api.models import APIKey
        key = APIKey.objects.create(name="t", scopes=["orders:read", "orders:write"])
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        o = self.order((self.p, 5))
        r = api.post(f"/api/v1/orders/{o.pk}/cancel/", {"restock": False}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.nums(self.p), (15, 0, 15))
        o.refresh_from_db()
        self.assertEqual(stock.order_stock_issues(o), [])       # раніше «ще тримає 5 — повернути»
        o.delete()
        self.assertEqual(self.nums(self.p), (15, 0, 15))

    def test_reopen_after_writeoff_does_not_deduct_twice_and_can_be_cancelled_again(self):
        from api.models import APIKey
        key = APIKey.objects.create(name="t", scopes=["orders:read", "orders:write"])
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        o = self.order((self.p, 5))
        api.post(f"/api/v1/orders/{o.pk}/cancel/", {"restock": False}, format="json")
        o = self.set_status(o, "received")
        self.assertEqual(self.nums(self.p), (15, 0, 15))
        self.set_status(o, "cancelled")                          # тепер — звичайне скасування з поверненням
        self.assertEqual(self.nums(self.p), (20, 0, 20))


# ── 14. Перемикач «Впливає на склад» ──────────────────────────────────────────

class AffectsStockToggleTests(StockBase):
    def toggle(self, order, value):
        order = SalesOrder.objects.get(pk=order.pk)
        order.affects_stock = value
        order.save()
        return order

    def test_switching_off_releases_reservation_and_on_restores_it(self):
        self.mode("creation", reservation=True)
        o = self.order((self.p, 5))
        self.assertEqual(self.nums(self.p), (20, 5, 15))
        o = self.toggle(o, False)
        self.assertEqual(self.nums(self.p), (20, 0, 20))         # раніше резерв лишався (невидимий в огляді)
        self.assertFalse(InventoryTransaction.objects.filter(tx_type=TX.RESERVED).exists())
        self.toggle(o, True)
        self.assertEqual(self.nums(self.p), (20, 5, 15))

    def test_bulk_actions_go_through_the_stock_rules(self):
        self.mode("creation", reservation=True)
        o = self.order((self.p, 5))
        c = self.admin_client()
        url = "/admin/sales/salesorder/"
        c.post(url, {"action": "action_set_affects_stock_no", "_selected_action": [o.pk]})
        self.assertEqual(self.nums(self.p), (20, 0, 20))         # раніше .update() — резерв лишався
        c.post(url, {"action": "action_set_affects_stock_yes", "_selected_action": [o.pk]})
        self.assertEqual(self.nums(self.p), (20, 5, 15))

    def test_switching_off_keeps_deducted_goods(self):
        o = self.order((self.p, 5))
        self.toggle(o, False)
        self.assertEqual(self.nums(self.p), (15, 0, 15))

    def test_legacy_lingering_reservation_is_reported_and_released_by_sync(self):
        self.mode("creation", reservation=True)
        o = self.order((self.p, 5))
        SalesOrder.objects.filter(pk=o.pk).update(affects_stock=False)   # як вимикали до виправлення
        o.refresh_from_db()
        issues = stock.order_stock_issues(o)
        self.assertEqual(len(issues), 1)
        self.assertIn("резерв 5", issues[0]["text"])
        c = self.admin_client()
        self.assertTrue(c.post(f"/admin/sales/salesorder/{o.pk}/sync-inventory/").json()["ok"])
        self.assertEqual(self.nums(self.p), (20, 0, 20))


# ── 15. Артикул і аліас в одному кошику ───────────────────────────────────────

class CheckAvailabilityAliasTests(StockBase):
    def test_sku_and_alias_are_checked_together(self):
        ProductAlias.objects.create(alias="FT-ALIAS", product=self.p)
        res = stock.check_availability([{"sku": "FT220407", "qty": 15}, {"sku": "FT-ALIAS", "qty": 10}])
        self.assertEqual([r["ok"] for r in res], [False, False])  # раніше обидва True (25 > 20)
        self.assertEqual([r["requested_total"] for r in res], [25, 25])
        res = stock.check_availability([{"sku": "FT220407", "qty": 15}, {"sku": "FT-ALIAS", "qty": 5}])
        self.assertEqual([r["ok"] for r in res], [True, True])
