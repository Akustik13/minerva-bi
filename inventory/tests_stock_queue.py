"""Черга на товар: на всі незакриті замовлення не вистачає — кому товар уже є, а кому чекати (stock.stock_queue).

    python manage.py test inventory.tests_stock_queue --settings=tabele.settings_test

Приклад з робочої бази (11.2026): AN220208-01C — 14 на складі, замовлення 8 (дедлайн 10.12) + 1 (дедлайн 11.12) +
8 (без дедлайну), у закупівлі ще 15. Раніше кожне з трьох показувало «бракує 3», хоча двом товару вистачає.
"""
import json
from datetime import date, timedelta
from decimal import Decimal as D

from django.contrib.auth.models import User
from django.utils import timezone

from inventory.models import PurchaseOrder, PurchaseOrderLine, Supplier
from inventory.services import stock
from inventory.tests_stock import StockBase
from sales.models import SalesOrder, SalesOrderLine


class QueueBase(StockBase):
    def setUp(self):
        super().setUp()
        self.mode("shipped")
        self.an = self.make_product("AN220208-01C", 14)
        today = timezone.localdate()
        # як на скриншоті: магазин без дедлайну, два замовлення DigiKey з дедлайнами
        self.shop = self.add_order("PO-31734-001", 8, deadline=None, day=today - timedelta(days=2), source="webshop")
        self.dk1 = self.add_order("102934847", 8, deadline=today + timedelta(days=13), day=today - timedelta(days=2))
        self.dk2 = self.add_order("102926880", 1, deadline=today + timedelta(days=14), day=today - timedelta(days=1))

    def make_product(self, sku, qty):
        from inventory.models import Product
        p = Product.objects.create(sku=sku, name=sku)
        if qty:
            self.receive(p, qty)
        return p

    def add_order(self, number, qty, *, deadline, day, source="digikey", status="processing", product=None):
        o = SalesOrder.objects.create(source=source, order_number=number, status=status, affects_stock=True,
                                      order_date=day, shipping_deadline=deadline)
        SalesOrderLine.objects.create(order=o, product=product or self.an, sku_raw=(product or self.an).sku, qty=qty)
        return SalesOrder.objects.get(pk=o.pk)

    def place(self, order):
        """(free, місце в черзі) єдиної позиції замовлення — як картка замовлення."""
        c = next(iter(stock.order_lines_cover(SalesOrder.objects.get(pk=order.pk)).values()))
        return c["free"], c["queue"]

    def po(self, qty):
        po = PurchaseOrder.objects.create(supplier=Supplier.objects.create(name="Jlcpcb"), status="ordered")
        PurchaseOrderLine.objects.create(purchase_order=po, product=self.an, qty_ordered=qty)
        return po


class QueueOrderTests(QueueBase):
    def test_numbers_are_the_same_as_before(self):
        self.assertEqual(self.nums(self.an), (14, 17, -3))

    def test_deadline_first_then_order_without_deadline(self):
        free, q = self.place(self.dk1)
        self.assertEqual((free, q["rank"], q["of"], q["short"]), (14, 1, 3, 0))    # раніше free 5 — «бракує 3»
        free, q = self.place(self.dk2)
        self.assertEqual((free, q["rank"], q["short"]), (6, 2, 0))                 # раніше −2
        free, q = self.place(self.shop)
        self.assertEqual((free, q["rank"], q["give"], q["short"]), (5, 3, 5, 3))
        self.assertEqual((q["pool"], q["total"]), (14, 17))

    def test_incoming_purchase_covers_the_shortage_in_queue_order(self):
        self.po(15)
        _, q = self.place(self.shop)
        self.assertEqual((q["short"], q["inc"], q["wait"]), (3, 3, 0))
        PurchaseOrderLine.objects.update(qty_ordered=2)
        _, q = self.place(self.shop)
        self.assertEqual((q["inc"], q["wait"]), (2, 1))                            # дозамовити ще 1

    def test_draft_purchase_is_not_counted_as_coming(self):
        self.po(15)
        PurchaseOrder.objects.update(status="draft")
        _, q = self.place(self.shop)
        self.assertEqual((q["inc"], q["wait"]), (0, 3))

    def test_pinned_order_goes_first(self):
        SalesOrder.objects.filter(pk=self.shop.pk).update(stock_first_at=timezone.now())
        free, q = self.place(self.shop)
        self.assertEqual((free, q["rank"], q["pinned"], q["short"]), (14, 1, True, 0))
        free, q = self.place(self.dk1)
        self.assertEqual((free, q["rank"], q["short"]), (6, 2, 2))
        free, q = self.place(self.dk2)
        self.assertEqual((free, q["give"], q["short"]), (-2, 0, 1))

    def test_earlier_pin_wins(self):
        now = timezone.now()
        SalesOrder.objects.filter(pk=self.shop.pk).update(stock_first_at=now - timedelta(hours=1))
        SalesOrder.objects.filter(pk=self.dk2.pk).update(stock_first_at=now)
        self.assertEqual(self.place(self.shop)[1]["rank"], 1)
        self.assertEqual(self.place(self.dk2)[1]["rank"], 2)

    def test_enough_for_everyone_keeps_old_numbers_and_no_queue(self):
        self.receive(self.an, 3)                                   # 17 на складі — вистачає всім
        free, q = self.place(self.dk1)
        self.assertIsNone(q)
        self.assertEqual(free, 8)                                  # доступно 0 + своє 8, як раніше

    def test_cancelled_and_delivered_do_not_take_part(self):
        self.set_status(self.dk1, "cancelled")
        self.assertIsNone(self.place(self.shop)[1])                # 14 на 8 + 1 — вистачає

    def test_reservation_mode_gives_the_same_queue(self):
        self.mode("shipped", reservation=True)
        for o in (self.shop, self.dk1, self.dk2):
            stock.sync_order(SalesOrder.objects.get(pk=o.pk))
        self.assertEqual(self.nums(self.an), (14, 17, -3))
        self.assertEqual(self.place(self.dk1)[1]["short"], 0)
        self.assertEqual(self.place(self.shop)[1]["short"], 3)

    def test_deducted_on_creation_mode_gives_the_same_queue(self):
        self.mode("creation")
        for o in (self.shop, self.dk1, self.dk2):
            stock.sync_order(SalesOrder.objects.get(pk=o.pk))
        self.assertEqual(self.nums(self.an), (-3, 0, -3))
        self.assertEqual(self.place(self.dk1)[0], 14)
        self.assertEqual(self.place(self.shop)[1]["short"], 3)

    def test_shipped_but_not_written_off_goes_first(self):
        self.mode("delivered")
        late = self.add_order("900", 2, deadline=None, day=timezone.localdate(), status="shipped")
        SalesOrder.objects.filter(pk=late.pk).update(shipped_at=timezone.localdate())
        self.assertEqual(self.nums(self.an), (14, 19, -5))
        self.assertEqual(self.place(late)[1]["rank"], 1)
        self.assertEqual(self.place(self.dk1)[0], 12)              # 2 шт уже поїхали

    def test_manual_reservation_outside_orders_stays_taken(self):
        from inventory.models import InventoryTransaction
        InventoryTransaction.objects.create(tx_type=InventoryTransaction.TxType.RESERVED, qty=-4, product=self.an,
                                            location=self.loc, external_key="manual:hold")
        _, q = self.place(self.dk1)
        self.assertEqual(q["pool"], 10)
        self.assertEqual(self.place(self.dk2)[1]["short"], 0)      # 10 − 8 = 2 ≥ 1
        self.assertEqual(self.place(self.shop)[1]["give"], 1)

    def test_other_products_are_not_affected(self):
        o = self.add_order("777", 2, deadline=None, day=timezone.localdate(), product=self.p)
        free, q = self.place(o)
        self.assertIsNone(q)
        self.assertEqual(free, 20)


class QueueAdviceTests(QueueBase):
    def test_ship_agent_warns_only_the_one_who_waits(self):
        from shipping.services import ship_advice
        texts = lambda o: " ".join(c["text"] for c in ship_advice.checks(SalesOrder.objects.get(pk=o.pk)))
        self.assertNotIn("бракує", texts(self.dk1))
        self.assertNotIn("бракує", texts(self.dk2))
        self.assertIn("черга 3/3", texts(self.shop))

    def test_digikey_auto_confirm_sees_enough_stock(self):
        from bots.services import digikey
        if not hasattr(digikey, "_check_stock_for_order"):
            self.skipTest("немає перевірки DigiKey")
        self.assertTrue(digikey._check_stock_for_order(SalesOrder.objects.get(pk=self.dk1.pk)))


class QueuePagesTests(QueueBase):
    def test_order_list_sends_queue_place(self):
        c = self.admin_client()
        r = c.get("/admin/sales/salesorder/")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.context_data["sv_order_stock_json"])
        self.assertEqual(data[str(self.dk1.pk)][0]["q"]["r"], 1)
        self.assertEqual(data[str(self.dk1.pk)][0]["stock"], 14)
        self.assertEqual(data[str(self.shop.pk)][0]["q"]["short"], 3)

    def test_order_card_shows_queue_and_pin_button(self):
        c = self.admin_client()
        r = c.get(f"/admin/sales/salesorder/{self.shop.pk}/change/")
        self.assertContains(r, "🔢 черга 3/3")
        self.assertContains(r, "📌 Віддати товар першим")
        self.assertContains(r, "бракує 3")

    def test_stock_overview_lists_reservations_in_queue_order(self):
        c = self.admin_client()
        r = c.get("/admin/inventory/reorderproxy/supply/")
        self.assertEqual(r.status_code, 200)
        row = next(x for x in r.context["rows"] if x["product"].pk == self.an.pk)
        self.assertTrue(row["queue_short"])
        self.assertEqual([res["order_number"] for res in row["reservations"]],
                         ["102934847", "102926880", "PO-31734-001"])
        self.assertContains(r, "ЧЕРГА")


class StockFirstViewTests(QueueBase):
    url = "/admin/sales/salesorder/{}/stock-first/"

    def test_pin_and_unpin(self):
        c = self.admin_client()
        r = c.post(self.url.format(self.shop.pk), data=json.dumps({"on": True}), content_type="application/json")
        self.assertEqual(r.json(), {"ok": True, "on": True})
        self.assertIsNotNone(SalesOrder.objects.get(pk=self.shop.pk).stock_first_at)
        self.assertEqual(self.place(self.shop)[1]["rank"], 1)
        r = c.post(self.url.format(self.shop.pk), data=json.dumps({"on": False}), content_type="application/json")
        self.assertEqual(r.json()["on"], False)
        self.assertIsNone(SalesOrder.objects.get(pk=self.shop.pk).stock_first_at)

    def test_get_is_refused(self):
        c = self.admin_client()
        r = c.get(self.url.format(self.shop.pk))
        self.assertEqual(r.status_code, 405)
        self.assertIsNone(SalesOrder.objects.get(pk=self.shop.pk).stock_first_at)

    def test_viewer_without_change_right_is_refused(self):
        from core.models import UserProfile
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        u = User.objects.create_user("viewer", "v@example.com", "p", is_staff=True)
        prof, _ = UserProfile.objects.get_or_create(user=u)
        prof.role = "readonly"
        prof.save()
        self.client.force_login(u)
        r = self.client.post(self.url.format(self.shop.pk), data="{}", content_type="application/json")
        self.assertIn(r.status_code, (302, 403))
        self.assertIsNone(SalesOrder.objects.get(pk=self.shop.pk).stock_first_at)
