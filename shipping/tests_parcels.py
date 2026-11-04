"""📦 Відправлення частинами (посилки): статус замовлення, склад у кожному режимі, підтвердження DigiKey, форми.

    python manage.py test shipping.tests_parcels --settings=tabele.settings_test

Числа складу — (на складі, резерв, доступно), як API /stock/ і огляд складу (inventory.tests_stock.StockBase).
Замовлення: FT220407 × 10 і AN220213 × 6 (на складі по 20). Посилка 1 — 10 + 4, посилка 2 — залишок 2.
"""
from decimal import Decimal as D
from unittest import mock

from inventory.models import InventoryTransaction
from inventory.services import stock
from inventory.tests_stock import StockBase
from sales.models import SalesOrder
from shipping.models import Carrier, Shipment, ShipmentLine
from shipping.services import parcels

TX = InventoryTransaction.TxType


class ParcelBase(StockBase):
    def setUp(self):
        super().setUp()
        self.carrier = Carrier.objects.create(name="UPS", carrier_type="ups")

    def make_order(self, status="processing"):
        o = self.order((self.p, 10), (self.q, 6), status=status)
        self.lp, self.lq = list(o.lines.order_by("pk"))
        return o

    def ship(self, order, contents=None, status="draft"):
        s = Shipment.objects.create(order=order, carrier=self.carrier, status=status)
        if contents is not None:
            parcels.set_contents(s, {self.lp.pk: contents[0], self.lq.pk: contents[1]})
        return s

    def move(self, shipment, status):
        s = Shipment.objects.get(pk=shipment.pk)
        s.status = status
        s.save()
        return s

    def status(self, order):
        return SalesOrder.objects.get(pk=order.pk).status

    def assertClean(self, order):
        self.assertEqual(stock.order_stock_issues(SalesOrder.objects.get(pk=order.pk)), [])


class ContentsTests(ParcelBase):
    def test_whole_order_stores_no_lines(self):
        o = self.make_order()
        s = self.ship(o, (10, 6))
        self.assertFalse(ShipmentLine.objects.filter(shipment=s).exists())
        self.assertFalse(parcels.has_parcels(o))

    def test_part_stores_lines_and_remainder(self):
        o = self.make_order()
        self.ship(o, (10, 4))
        p = parcels.progress(o)
        self.assertTrue(p.active)
        self.assertEqual(p.to_pack, 2)
        self.assertEqual(p.to_send, 16)                      # ще нічого не поїхало: відправити — усе
        self.assertEqual(p.remainder_text(), "FT220407 × 10, AN220213 × 6")

    def test_validation(self):
        o = self.make_order()
        s = Shipment.objects.create(order=o, carrier=self.carrier)
        for bad in ({self.lp.pk: 11}, {self.lp.pk: -1}, {self.lp.pk: 0, self.lq.pk: 0}, {self.lp.pk: "x"}):
            with self.assertRaises(parcels.ParcelError):
                parcels.set_contents(s, bad)

    def test_new_shipment_gets_remainder_automatically(self):
        o = self.make_order()
        self.ship(o, (10, 4))
        s2 = Shipment.objects.create(order=o, carrier=self.carrier)       # «Відправка сьогодні», швидке створення
        self.assertEqual(parcels.contents(s2), {self.lq.pk: D(2)})

    def test_view_rows(self):
        o = self.make_order()
        s1 = self.ship(o, (10, 4))
        rows = {r["pk"]: r for r in parcels.view_rows(o)}
        self.assertEqual((rows[self.lq.pk]["other"], rows[self.lq.pk]["here"], rows[self.lq.pk]["left"]), (4, 2, 0))
        rows = {r["pk"]: r for r in parcels.view_rows(o, s1)}
        self.assertEqual((rows[self.lq.pk]["other"], rows[self.lq.pk]["here"]), (0, 4))


class StatusTests(ParcelBase):
    def test_first_parcel_keeps_order_open_last_closes(self):
        o = self.make_order(status="received")
        s1 = self.ship(o, (10, 4))
        self.move(s1, "in_transit")
        self.assertEqual(self.status(o), "processing")
        # будь-який шлях (трекінг, DigiKey, кнопка) хоче «Відправлено» — замовлення лишається відкритим
        order = SalesOrder.objects.get(pk=o.pk)
        order.status = "shipped"
        order.save(update_fields=["status"])
        self.assertEqual(self.status(o), "processing")
        s2 = Shipment.objects.create(order=o, carrier=self.carrier)
        self.move(s2, "in_transit")
        self.assertEqual(self.status(o), "shipped")
        self.move(s1, "delivered")
        self.assertEqual(self.status(o), "shipped")          # друга посилка ще в дорозі
        self.move(s2, "delivered")
        self.assertEqual(self.status(o), "delivered")

    def test_auto_delivered_held_until_everything_delivered(self):
        o = self.make_order()
        s1 = self.ship(o, (10, 4))
        self.move(s1, "delivered")
        order = SalesOrder.objects.get(pk=o.pk)
        order.status = "delivered"
        order.save()
        self.assertEqual(self.status(o), "processing")
        self.assertIsNone(SalesOrder.objects.get(pk=o.pk).delivered_at)

    def test_orders_without_parcels_unchanged(self):
        o = self.make_order()
        s = self.ship(o)
        self.move(s, "in_transit")
        self.assertEqual(self.status(o), "processing")       # як завжди: статус ставлять шляхи трекінгу
        self.set_status(o, "shipped")
        self.assertEqual(self.status(o), "shipped")

    def test_contents_set_after_auto_close_reopens_order(self):
        """Замовлення вже стало «Відправлено» з першою посилкою (до посилок у Minerva) — людина вказує, що в ній
        була лише частина: замовлення знову «В обробці»."""
        o = self.make_order()
        s = self.ship(o, status="in_transit")
        self.set_status(o, "shipped")
        parcels.set_contents(Shipment.objects.get(pk=s.pk), {self.lp.pk: 10, self.lq.pk: 4})
        self.assertEqual(self.status(o), "processing")
        self.assertEqual(parcels.progress(o).remainder_text(), "AN220213 × 2")

    def test_line_reduced_to_sent_closes_order(self):
        o = self.make_order()
        self.move(self.ship(o, (10, 4)), "in_transit")
        self.lq.qty = 4                                       # покупець скасував залишок (DigiKey)
        self.lq.save()
        self.assertEqual(self.status(o), "shipped")

    def test_cancelled_parcel_not_counted(self):
        o = self.make_order()
        s1 = self.move(self.ship(o, (10, 4)), "in_transit")
        self.move(s1, "cancelled")
        self.assertEqual(parcels.progress(o).sent, 0)


class StockModesTests(ParcelBase):
    def run_flow(self):
        o = self.make_order()
        s1 = self.ship(o, (10, 4))
        return o, s1

    def test_reservation_mode(self):
        self.mode("creation", reservation=True)
        o, s1 = self.run_flow()
        self.assertEqual((self.nums(self.p), self.nums(self.q)), ((20, 10, 10), (20, 6, 14)))
        self.move(s1, "in_transit")
        self.assertEqual((self.nums(self.p), self.nums(self.q)), ((10, 0, 10), (16, 2, 14)))
        self.assertClean(o)
        s2 = Shipment.objects.create(order=o, carrier=self.carrier)
        self.move(s2, "in_transit")
        self.assertEqual(self.status(o), "shipped")
        self.assertEqual((self.nums(self.p), self.nums(self.q)), ((10, 0, 10), (14, 0, 14)))
        self.assertClean(o)

    def test_shipped_mode(self):
        self.mode("shipped")
        o, s1 = self.run_flow()
        self.assertEqual(self.nums(self.q), (20, 6, 14))
        self.move(s1, "in_transit")
        self.assertEqual((self.nums(self.p), self.nums(self.q)), ((10, 0, 10), (16, 2, 14)))
        self.assertClean(o)
        self.move(Shipment.objects.create(order=o, carrier=self.carrier), "in_transit")
        self.assertEqual((self.nums(self.p), self.nums(self.q)), ((10, 0, 10), (14, 0, 14)))
        self.assertClean(o)

    def test_delivered_mode_counts_delivered_parcels_only(self):
        self.mode("delivered")
        o, s1 = self.run_flow()
        self.move(s1, "in_transit")
        self.assertEqual(self.nums(self.q), (20, 6, 14))
        self.move(s1, "delivered")
        self.assertEqual((self.nums(self.p), self.nums(self.q)), ((10, 0, 10), (16, 2, 14)))
        self.assertClean(o)

    def test_creation_mode_untouched(self):
        o, s1 = self.run_flow()
        self.assertEqual(self.nums(self.q), (14, 0, 14))
        self.move(s1, "in_transit")
        self.assertEqual(self.nums(self.q), (14, 0, 14))
        self.assertFalse(InventoryTransaction.objects.filter(external_key__contains=":sent:").exists())
        self.assertClean(o)

    def test_cancelled_parcel_returns_stock(self):
        self.mode("shipped")
        o, s1 = self.run_flow()
        self.move(s1, "in_transit")
        self.move(s1, "cancelled")
        self.assertEqual((self.nums(self.p), self.nums(self.q)), ((20, 10, 10), (20, 6, 14)))
        self.assertClean(o)

    def test_cancelled_order_returns_everything(self):
        self.mode("creation", reservation=True)
        o, s1 = self.run_flow()
        self.move(s1, "in_transit")
        self.set_status(o, "cancelled")
        self.assertEqual((self.nums(self.p), self.nums(self.q)), ((20, 0, 20), (20, 0, 20)))

    def test_reopen_returns_unsent_part(self):
        """Старе замовлення: усе списано при «Відправлено», а поїхала лише частина — після вмісту посилки залишок
        повертається на полицю й бронюється."""
        self.mode("shipped")
        o = self.make_order()
        s = self.ship(o, status="in_transit")
        self.set_status(o, "shipped")
        self.assertEqual(self.nums(self.q), (14, 0, 14))
        parcels.set_contents(Shipment.objects.get(pk=s.pk), {self.lp.pk: 10, self.lq.pk: 4})
        self.assertEqual((self.nums(self.p), self.nums(self.q)), ((10, 0, 10), (16, 2, 14)))
        self.assertClean(o)

    def test_open_order_lines_show_remainder(self):
        self.mode("shipped")
        o, s1 = self.run_flow()
        self.move(s1, "in_transit")
        rows = [(l.order_id, state, l._open_qty) for l, state in stock.open_order_lines([self.q.pk])]
        self.assertEqual(rows, [(o.pk, "reserved", 2)])


class DigiKeyShipTests(ParcelBase):
    """PUT /orders/{id}/ship: лише позиції посилки з їхньою кількістю."""

    ORDER = {"orderState": "ShippingInProgress", "orderDetails": [
        {"orderDetailId": "d1", "supplierSku": "FT220407", "quantity": 10},
        {"orderDetailId": "d2", "supplierSku": "AN220213", "quantity": 6},
    ], "shippingTrackingInfoList": []}

    def call(self, **kw):
        from bots.services import digikey
        sent = {}

        class Resp:
            ok, status_code, text = True, 200, "{}"

            def json(self):
                return {}

        def put(url, headers=None, json=None, timeout=None):
            sent["payload"] = json
            return Resp()

        with mock.patch.object(digikey, "get_marketplace_token", return_value="t"), \
                mock.patch.object(digikey, "_fetch_marketplace_order", return_value=self.ORDER), \
                mock.patch("requests.put", side_effect=put):
            res = digikey.ship_marketplace_order(mock.Mock(use_sandbox=True), "102", tracking_number="1Z", **kw)
        return res, (sent.get("payload") or {}).get("shippingTracking", [{}])[0].get("shippedParts")

    def test_parcel_by_sku(self):
        res, parts = self.call(parts_by_sku={"FT220407": D(10), "AN220213": D(4)})
        self.assertTrue(res["ok"])
        self.assertEqual([(p["orderDetailId"], p["quantity"]) for p in parts], [("d1", 10), ("d2", 4)])
        self.assertEqual(res["remaining"], 2)

    def test_zero_lines_skipped(self):
        res, parts = self.call(shipped_quantities={"d1": 0, "d2": 2})
        self.assertEqual([(p["orderDetailId"], p["quantity"]) for p in parts], [("d2", 2)])

    def test_unknown_sku_not_sent(self):
        res, parts = self.call(parts_by_sku={"XX-1": D(1)})
        self.assertFalse(res["ok"])
        self.assertIsNone(parts)

    def test_form_rows_prefill_parcel_and_digikey_remaining(self):
        from bots.views import _ship_rows
        o = self.make_order()
        s = self.ship(o, (10, 4))
        rows = _ship_rows(self.ORDER["orderDetails"], self.ORDER, s)
        self.assertEqual([(r["detail_id"], r["value"], r["left"]) for r in rows], [("d1", 10, 10), ("d2", 4, 6)])
        shipped = dict(self.ORDER, shippingTrackingInfoList=[
            {"shippedParts": [{"orderDetailId": "d1", "quantity": 10}, {"orderDetailId": "d2", "quantity": 4}]}])
        rows = _ship_rows(self.ORDER["orderDetails"], shipped, None)
        self.assertEqual([(r["detail_id"], r["already"], r["value"]) for r in rows], [("d1", 10, 0), ("d2", 4, 2)])

    def test_already_shipped_from_tracking_list(self):
        from bots.services import digikey
        order = {"orderDetails": [{"orderDetailId": "d1", "quantity": 10}],
                 "shippingTrackingInfoList": [{"shippedParts": [{"orderDetailId": "d1", "quantity": 7}]}]}
        self.assertEqual(digikey.marketplace_already_shipped(order), {"d1": 7})


class PagesTests(ParcelBase):
    """Форма відправлення, сторінка посилки, підтвердження в магазині, картка замовлення, документи."""

    def setUp(self):
        super().setUp()
        self.admin_client()
        self.o = self.make_order()

    def test_create_form_first_parcel_whole_then_remainder(self):
        r = self.client.get(f"/admin/shipping/shipment/create/{self.o.pk}/")
        self.assertContains(r, "📦 Що в цій посилці")
        self.assertContains(r, f'name="parcel_qty_{self.lq.pk}" value="6"')
        self.ship(self.o, (10, 4))
        r = self.client.get(f"/admin/shipping/shipment/create/{self.o.pk}/")
        self.assertContains(r, f'name="parcel_qty_{self.lq.pk}" value="2"')
        self.assertContains(r, f'name="parcel_qty_{self.lp.pk}" value="0"')

    def test_create_post_saves_contents(self):
        r = self.client.post(f"/admin/shipping/shipment/create/{self.o.pk}/", {
            "carrier": self.carrier.pk, "weight_kg": "1", "action_btn": "save",
            f"parcel_qty_{self.lp.pk}": "10", f"parcel_qty_{self.lq.pk}": "4"})
        self.assertEqual(r.status_code, 302)
        s = Shipment.objects.get(order=self.o)
        self.assertEqual(parcels.contents(s), {self.lp.pk: D(10), self.lq.pk: D(4)})

    def test_detail_card_and_parcel_post(self):
        s = self.ship(self.o, status="in_transit")
        url = f"/admin/shipping/shipment/{s.pk}/detail/"
        r = self.client.get(url)
        self.assertContains(r, "✂️ Відправили лише частину?")
        r = self.client.post(f"/admin/shipping/shipment/{s.pk}/parcel/",
                             {f"parcel_qty_{self.lp.pk}": "10", f"parcel_qty_{self.lq.pk}": "4"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(parcels.progress(self.o).remainder_text(), "AN220213 × 2")
        r = self.client.get(url)
        self.assertContains(r, "Посилка 1 з 1")
        self.assertContains(r, "🚚 Створити посилку на залишок")
        r = self.client.get(f"/admin/sales/salesorder/{self.o.pk}/change/")
        self.assertContains(r, "📦 Відправлено частинами: 14 з 16 шт.")
        self.assertContains(r, "🚚 Відправити залишок")

    def test_parcel_post_needs_change_right(self):
        from core.tests_hardening3 import person
        s = self.ship(self.o)
        self.client.force_login(person("vera", "readonly", allowed_modules=["shipping", "sales"]))
        self.client.post(f"/admin/shipping/shipment/{s.pk}/parcel/", {f"parcel_qty_{self.lp.pk}": "1"})
        self.assertFalse(ShipmentLine.objects.exists())

    def dk_setup(self):
        from shipping.models import ShippingSettings
        self.o.source = "digikey"
        self.o.save()
        cfg = ShippingSettings.get()
        cfg.shop_confirm_prompt, cfg.shop_confirm_sources = True, ["digikey"]
        cfg.save()

    def test_digikey_waits_for_all_parcels_then_confirms_them_together(self):
        """DigiKey приймає лише все замовлення разом: посилка 1 — «чекаємо», коли є трек-номер посилки 2 — одне
        підтвердження з обома трек-номерами."""
        self.dk_setup()
        s1 = self.ship(self.o, (10, 4))
        Shipment.objects.filter(pk=s1.pk).update(tracking_number="1Z1", status="label_ready")
        r = self.client.get(f"/admin/shipping/shipment/{s1.pk}/detail/")
        self.assertContains(r, "DigiKey: підтвердимо разом")
        self.assertContains(r, "🚚 Відправити залишок")
        calls = []

        def fake_ship(config, order_id, **kw):
            calls.append(kw)
            return {"ok": True, "message": "ok", "raw": {}, "remaining": 0, "parts": []}

        with mock.patch("bots.services.digikey.ship_marketplace_order", side_effect=fake_ship), \
                mock.patch("shipping.admin.ShipmentAdmin._dk_carrier_for", return_value=None):
            self.client.post(f"/admin/shipping/shipment/{s1.pk}/shop-confirm/", {"mode": "parcels"})
            self.assertEqual(calls, [])                          # ще не все в посилках — DigiKey не викликається
            s2 = Shipment.objects.create(order=self.o, carrier=self.carrier)
            Shipment.objects.filter(pk=s2.pk).update(tracking_number="1Z2", status="label_ready")
            r = self.client.get(f"/admin/shipping/shipment/{s2.pk}/detail/")
            self.assertContains(r, "усе замовлення одним підтвердженням")
            self.client.post(f"/admin/shipping/shipment/{s2.pk}/shop-confirm/", {"mode": "parcels"})
        self.assertEqual([(x["tracking"], x["parts_by_sku"]) for x in calls[0]["shipments"]],
                         [("1Z1", {"FT220407": D(10), "AN220213": D(4)}), ("1Z2", {"AN220213": D(2)})])
        self.assertTrue(all(s.shop_confirmed_at for s in Shipment.objects.filter(order=self.o)))

    def test_digikey_all_on_this_tracking_fallback(self):
        self.dk_setup()
        s1 = self.ship(self.o, (10, 4))
        Shipment.objects.filter(pk=s1.pk).update(tracking_number="1Z1", status="label_ready")
        calls = []

        def fake_ship(config, order_id, **kw):
            calls.append(kw)
            return {"ok": True, "message": "ok", "raw": {}, "remaining": 0, "parts": []}

        with mock.patch("bots.services.digikey.ship_marketplace_order", side_effect=fake_ship), \
                mock.patch("shipping.admin.ShipmentAdmin._dk_carrier_for", return_value=None):
            self.client.post(f"/admin/shipping/shipment/{s1.pk}/shop-confirm/", {"mode": "all_here"})
        self.assertEqual((calls[0]["tracking_number"], calls[0].get("parts_by_sku"), calls[0].get("shipments")),
                         ("1Z1", None, None))
        self.assertEqual(self.status(self.o), "processing")      # у Minerva ще 2 шт. не відправлено
        self.assertIsNotNone(Shipment.objects.get(pk=s1.pk).shop_confirmed_at)

    def test_manual_confirm_on_digikey_is_respected(self):
        """Підтвердили на supplier.digikey.com — синхронізація з DigiKey позначає посилки підтвердженими."""
        from bots.services.digikey import _parcels_dk_shipped
        s1 = self.ship(self.o, (10, 4))
        Shipment.objects.filter(pk=s1.pk).update(tracking_number="1Z1", status="in_transit")
        _parcels_dk_shipped(SalesOrder.objects.get(pk=self.o.pk), "ShippingInProgress")
        self.assertIsNone(Shipment.objects.get(pk=s1.pk).shop_confirmed_at)
        _parcels_dk_shipped(SalesOrder.objects.get(pk=self.o.pk), "Shipped")
        self.assertIsNotNone(Shipment.objects.get(pk=s1.pk).shop_confirmed_at)
        self.assertEqual(self.status(self.o), "processing")

    def test_documents_take_parcel_quantities(self):
        from shipping.services import commercial_invoice_service as ci, packing_list_service as pl
        s1 = self.ship(self.o, (10, 4))
        s2 = Shipment.objects.create(order=self.o, carrier=self.carrier)
        lines = ci.initial_data(self.o, s2.pk)["lines"]
        self.assertEqual([(l["part_no"], l["qty"]) for l in lines], [("AN220213", 2.0)])
        lines = pl.initial_data(self.o, s1.pk)["parcels"][0]["lines"]
        self.assertEqual([(l["part_no"], l["qty"]) for l in lines], [("FT220407", 10.0), ("AN220213", 4.0)])

    def test_admin_status_shipped_is_held_with_message(self):
        self.move(self.ship(self.o, (10, 4)), "in_transit")
        o = SalesOrder.objects.get(pk=self.o.pk)
        o.status = "shipped"
        o.save()
        self.assertEqual(getattr(o, "_parcels_held", None), "shipped")
        self.assertEqual(self.status(self.o), "processing")

    def test_order_card_remainder_button_and_list_badge(self):
        s = self.ship(self.o, (10, 4))
        self.move(s, "label_ready")
        r = self.client.get(f"/admin/sales/salesorder/{self.o.pk}/change/")
        self.assertContains(r, "🚚 Відправити залишок")
        r = self.client.get("/admin/sales/salesorder/")
        self.assertContains(r, "📦 14 / 16")

    def test_order_card_split_link(self):
        card = f"/admin/sales/salesorder/{self.o.pk}/change/"
        r = self.client.get(card)
        self.assertContains(r, f"/admin/shipping/shipment/create/{self.o.pk}/#parcel-card")
        self.assertContains(r, "✂️ Відправити лише частину")
        s = self.ship(self.o, status="in_transit")
        r = self.client.get(card)
        self.assertContains(r, f"/admin/shipping/shipment/{s.pk}/detail/#parcel")
        self.assertContains(r, "✂️ Відправили лише частину?")
        parcels.set_contents(Shipment.objects.get(pk=s.pk), {self.lp.pk: 10, self.lq.pk: 4})
        r = self.client.get(card)
        self.assertNotContains(r, "✂️ Відправили лише частину?")
        self.assertContains(r, "📦 Відправлено частинами")


class PartialMoreTests(ParcelBase):
    """Склад після етикетки, картки, лист клієнту, DigiKey кількома посилками."""

    def test_label_ready_parcel_leaves_the_shelf_order_stays_open(self):
        self.mode("shipped")
        o = self.make_order()
        s1 = self.ship(o, (10, 4))
        self.move(s1, "label_ready")
        self.assertEqual((self.nums(self.p), self.nums(self.q)), ((10, 0, 10), (16, 2, 14)))
        self.assertEqual(self.status(o), "processing")
        self.assertClean(o)

    def test_list_annotation(self):
        o = self.make_order()
        other = self.order((self.p, 1))
        self.move(self.ship(o, (10, 4)), "label_ready")
        orders = [SalesOrder.objects.get(pk=o.pk), SalesOrder.objects.get(pk=other.pk)]
        parcels.annotate_orders(orders)
        self.assertEqual((orders[0].parcel_info["sent"], orders[0].parcel_info["ordered"],
                          orders[0].parcel_info["unpacked"]), ("14", "16", "AN220213 × 2"))
        self.assertIsNone(orders[1].parcel_info)

    def test_mail_about_parcel(self):
        from dashboard.notifications import mark_ship_mail_sent, ship_mail_context
        o = self.make_order()
        s1 = self.ship(o, (10, 4))
        Shipment.objects.filter(pk=s1.pk).update(tracking_number="1Z1", status="label_ready")
        ctx, parcel, suffix = ship_mail_context(SalesOrder.objects.get(pk=o.pk))
        self.assertEqual((parcel.pk, ctx["tracking_number"]), (s1.pk, "1Z1"))
        self.assertIn("FT220407 — 10 pcs.", ctx["items"])
        self.assertIn("partial shipment (parcel 1 of 2)", ctx["items"])
        self.assertIn("AN220213 — 2 pcs.", ctx["items"].split("Still to follow")[1])
        self.assertEqual(suffix, " — partial shipment 1/2")
        mark_ship_mail_sent(o, parcel)
        s2 = Shipment.objects.create(order=o, carrier=self.carrier)
        Shipment.objects.filter(pk=s2.pk).update(tracking_number="1Z2", status="label_ready")
        ctx, parcel, suffix = ship_mail_context(SalesOrder.objects.get(pk=o.pk))
        self.assertEqual((parcel.pk, ctx["tracking_number"], suffix), (s2.pk, "1Z2", " — parcel 2/2"))
        self.assertIn("final parcel", ctx["items"])
        self.assertNotIn("FT220407", ctx["items"])

    def test_mail_without_parcels_unchanged(self):
        from dashboard.notifications import ship_mail_context
        o = self.make_order()
        o.tracking_number = "1Z9"
        o.save()
        ctx, parcel, suffix = ship_mail_context(o)
        self.assertEqual((parcel, suffix, ctx["tracking_number"]), (None, "", "1Z9"))
        self.assertIn("AN220213 — 6 pcs.", ctx["items"])

    def test_shop_plan(self):
        o = self.make_order()
        s1 = self.ship(o, (10, 4))
        Shipment.objects.filter(pk=s1.pk).update(tracking_number="1Z1", status="label_ready")
        plan = parcels.shop_plan(o)
        self.assertFalse(plan["ready"])
        self.assertIn("AN220213 × 2", plan["reason"])
        s2 = Shipment.objects.create(order=o, carrier=self.carrier)
        self.assertIn("чернетка", parcels.shop_plan(o)["reason"])
        Shipment.objects.filter(pk=s2.pk).update(status="label_ready")
        self.assertIn("трек-номер", parcels.shop_plan(o)["reason"])
        Shipment.objects.filter(pk=s2.pk).update(tracking_number="1Z2")
        self.assertTrue(parcels.shop_plan(o)["ready"])


class DigiKeyParcelsCallTests(ParcelBase):
    """ship_marketplace_order(shipments=…): одне підтвердження, кожна посилка — свій трек-номер."""

    ORDER = DigiKeyShipTests.ORDER

    def call_payload(self, order=None, **kw):
        from bots.services import digikey
        sent = {}

        class Resp:
            ok, status_code, text = True, 200, "{}"

            def json(self):
                return {}

        def put(url, headers=None, json=None, timeout=None):
            sent["payload"] = json
            return Resp()

        with mock.patch.object(digikey, "get_marketplace_token", return_value="t"), \
                mock.patch.object(digikey, "_fetch_marketplace_order", return_value=order or self.ORDER), \
                mock.patch("requests.put", side_effect=put):
            res = digikey.ship_marketplace_order(mock.Mock(use_sandbox=True), "102", tracking_number="1Z1", **kw)
        return res, sent.get("payload")

    def test_two_parcels_one_call(self):
        res, payload = self.call_payload(shipments=[
            {"tracking": "1Z1", "carrier_id": "c", "parts_by_sku": {"FT220407": D(10), "AN220213": D(4)}},
            {"tracking": "1Z2", "carrier_id": None, "parts_by_sku": {"AN220213": D(2)}}])
        self.assertTrue(res["ok"])
        self.assertEqual([(t["shippingTrackingNumber"], [(p["orderDetailId"], p["quantity"]) for p in t["shippedParts"]])
                          for t in payload["shippingTracking"]],
                         [("1Z1", [("d1", 10), ("d2", 4)]), ("1Z2", [("d2", 2)])])

    def test_short_parcels_not_sent(self):
        res, payload = self.call_payload(shipments=[
            {"tracking": "1Z1", "parts_by_sku": {"FT220407": D(10), "AN220213": D(4)}}])
        self.assertFalse(res["ok"])
        self.assertIn("AN220213 × 2", res["message"])
        self.assertIsNone(payload)

    def test_already_shipped_on_digikey(self):
        order = dict(self.ORDER, shippingTrackingInfoList=[
            {"shippedParts": [{"orderDetailId": "d1", "quantity": 10}, {"orderDetailId": "d2", "quantity": 6}]}])
        res, payload = self.call_payload(order=order)
        self.assertTrue(res.get("already"))
        self.assertIsNone(payload)

    def test_invalid_quantity_hint(self):
        from bots.services.digikey import _ship_hint
        self.assertIn("лише всього замовлення", _ship_hint("Invalid Shipped Quantity"))
        self.assertEqual(_ship_hint("Unauthorized"), "")


class StockCoverTests(ParcelBase):
    """Колонка «Склад» картки: відправлене посилками окремо, «потрібно» — лише залишок."""

    def test_cover_after_first_parcel(self):
        self.mode("shipped")
        o = self.make_order()
        self.move(self.ship(o, (10, 4)), "label_ready")
        cover = stock.order_lines_cover(SalesOrder.objects.get(pk=o.pk))
        p, q = cover[self.lp.pk], cover[self.lq.pk]
        self.assertEqual((p["sent"], p["need"], p["free"]), (10, 0, 10))     # на полиці 10, для замовлення — нічого
        self.assertEqual((q["sent"], q["need"], q["free"]), (4, 2, 16))      # на полиці 16, ще потрібно 2

    def test_cover_without_parcels_unchanged(self):
        self.mode("shipped")
        o = self.make_order()
        c = stock.order_lines_cover(SalesOrder.objects.get(pk=o.pk))[self.lq.pk]
        self.assertEqual((c["sent"], c["need"], c["free"]), (0, 6, 20))

    def test_list_card_json(self):
        import json
        self.mode("shipped")
        self.admin_client()
        o = self.make_order()
        self.move(self.ship(o, (10, 4)), "label_ready")
        r = self.client.get("/admin/sales/salesorder/")
        data = {l["pid"]: l for l in json.loads(r.context_data["sv_order_stock_json"])[str(o.pk)]}
        self.assertEqual((data[self.p.pk]["need"], data[self.p.pk]["sent"]), (0, 10))
        self.assertEqual((data[self.q.pk]["need"], data[self.q.pk]["sent"], data[self.q.pk]["stock"]), (2, 4, 16))


class IncomingOnCardTests(ParcelBase):
    """📥 Чекаємо поставку: позиції бракує товару, а його вже замовлено в постачальника."""

    def test_short_line_shows_incoming(self):
        import json
        from datetime import date
        from inventory.models import PurchaseOrder, PurchaseOrderLine
        self.mode("shipped")
        self.admin_client()
        o = self.order((self.q, 30))                          # на складі 20 — бракує 10
        po = PurchaseOrder.objects.create(status="ordered", code="PO-10", expected_date=date(2026, 11, 20))
        PurchaseOrderLine.objects.create(purchase_order=po, product=self.q, qty_ordered=300)
        PurchaseOrderLine.objects.create(purchase_order=po, product=self.p, qty_ordered=50)   # FT не бракує
        self.assertEqual(stock.incoming_info([self.q.pk])[self.q.pk]["codes"], ["PO-10"])
        r = self.client.get("/admin/sales/salesorder/")
        row = json.loads(r.context_data["sv_order_stock_json"])[str(o.pk)][0]
        self.assertEqual((row["inc"], row["inc_po"], row["inc_date"]), (300, "PO-10", "20.11.2026"))
        po_info = json.loads(r.context_data["sv_order_po_json"])[str(o.pk)]
        self.assertEqual((po_info["items"], po_info["expected_date"]), ("AN220213 +300 (PO-10)", "20.11.2026"))
        r = self.client.get(f"/admin/sales/salesorder/{o.pk}/change/")
        self.assertContains(r, "📥 +300 PO-10 · 20.11.2026")

    def test_enough_stock_no_incoming_note(self):
        import json
        from inventory.models import PurchaseOrder, PurchaseOrderLine
        self.admin_client()
        o = self.order((self.q, 5))
        po = PurchaseOrder.objects.create(status="ordered", code="PO-11")
        PurchaseOrderLine.objects.create(purchase_order=po, product=self.q, qty_ordered=300)
        r = self.client.get("/admin/sales/salesorder/")
        self.assertNotIn("inc", json.loads(r.context_data["sv_order_stock_json"])[str(o.pk)][0])
        self.assertNotIn(str(o.pk), json.loads(r.context_data["sv_order_po_json"]))


class StillToShipTests(ParcelBase):
    """«⏳ ще відправити N» — явно для частково відправленої позиції й замовлення."""

    def test_badge_and_inline(self):
        self.mode("shipped")
        self.admin_client()
        o = self.make_order()
        self.move(self.ship(o, (10, 4)), "label_ready")
        r = self.client.get("/admin/sales/salesorder/")
        self.assertContains(r, "📦 14 / 16 · ⏳ ще 2")
        r = self.client.get(f"/admin/sales/salesorder/{o.pk}/change/")
        self.assertContains(r, "⏳ ще відправити 2")
