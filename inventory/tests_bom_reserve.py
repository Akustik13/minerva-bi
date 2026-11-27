"""🔧 Компонент для ще не списаних виробів з BOM — у «Резерв», черзі й «Резерваціях» (stock._bom_demand_subqueries,
bom_open_lines, stock_queue, lines_cover).

    python manage.py test inventory.tests_bom_reserve --settings=tabele.settings_test

Випадок користувача (11.2026): склад «Кабелі», кабель продається окремо й іде в BOM антени (0,5 на антену). У режимі
«списувати при відправці» кабель для ще не відправлених антен ніде не бронювався: «Доступно» кабелю було більшим,
ніж насправді, і окремий продаж міг забрати кабель, потрібний для антен.
"""
import json
import uuid
from datetime import timedelta
from decimal import Decimal as D

from django.utils import timezone

from inventory.models import InventoryTransaction, Location, Product, ProductComponent
from inventory.services import stock
from inventory.tests_stock import StockBase
from sales.models import SalesOrder, SalesOrderLine

TX = InventoryTransaction.TxType


class BomReserveBase(StockBase):
    def setUp(self):
        super().setUp()
        self.mode("shipped")
        self.cables = Location.objects.create(code="CABLES", name="Кабелі", location_type=Location.LocationType.OTHER)
        self.cable = Product.objects.create(sku="CA-1", kind=Product.Kind.COMPONENT)
        self.antenna = Product.objects.create(sku="AN-1", bom_type=Product.BomType.KEY)
        self.pc = ProductComponent.objects.create(parent=self.antenna, component=self.cable, qty_per=D("0.5"))
        self.receive(self.antenna, 100)
        InventoryTransaction.objects.create(tx_type=TX.INCOMING, qty=20, product=self.cable, location=self.cables,
                                            external_key=f"t:{uuid.uuid4()}")

    def add(self, product, qty, deadline_days=None, number=None):
        self._n += 1
        today = timezone.localdate()
        o = SalesOrder.objects.create(source="manual", order_number=number or f"{500 + self._n}", status="processing",
                                      affects_stock=True, order_date=today,
                                      shipping_deadline=today + timedelta(days=deadline_days) if deadline_days else None)
        SalesOrderLine.objects.create(order=o, product=product, sku_raw=product.sku, qty=qty)
        return SalesOrder.objects.get(pk=o.pk)

    def cover(self, order):
        return next(iter(stock.order_lines_cover(SalesOrder.objects.get(pk=order.pk)).values()))

    def at_cables(self):
        return {r["location"]: (r["on_hand"], r["reserved"]) for r in stock.location_breakdown(self.cable)}["CABLES"]


class ReserveTests(BomReserveBase):
    def test_cable_for_unshipped_antennas_is_reserved(self):
        self.add(self.antenna, 4)
        self.add(self.cable, 2)
        self.assertEqual(self.nums(self.cable), (20, 4, 16))       # раніше (20, 2, 18)
        self.assertEqual(self.at_cables(), (20, 4))

    def test_shipping_the_antenna_turns_reserve_into_write_off(self):
        a = self.add(self.antenna, 4)
        self.set_status(a, "shipped")
        self.assertEqual(self.nums(self.cable), (18, 0, 18))

    def test_reservation_mode_counts_reserved_antennas(self):
        self.mode("shipped", reservation=True)
        a = self.add(self.antenna, 4)
        stock.sync_order(a)
        self.assertTrue(InventoryTransaction.objects.filter(product=self.antenna, tx_type=TX.RESERVED).exists())
        self.assertEqual(self.nums(self.cable), (20, 2, 18))

    def test_deducted_on_creation_is_not_reserved_twice(self):
        self.mode("creation")
        self.order((self.antenna, 4))
        self.assertEqual(self.nums(self.cable), (18, 0, 18))

    def test_optional_component_and_kit_without_bom_do_not_reserve(self):
        self.pc.optional = True
        self.pc.save()
        self.add(self.antenna, 4)
        self.assertEqual(self.nums(self.cable), (20, 0, 20))
        self.pc.optional = False
        self.pc.save()
        Product.objects.filter(pk=self.antenna.pk).update(bom_type=Product.BomType.NONE)
        self.assertEqual(self.nums(self.cable), (20, 0, 20))

    def test_cancelled_antenna_releases_cable(self):
        a = self.add(self.antenna, 4)
        self.set_status(a, "cancelled")
        self.assertEqual(self.nums(self.cable), (20, 0, 20))

    def test_sql_and_python_agree(self):
        self.add(self.antenna, 3)
        self.add(self.antenna, 5)
        self.mode("shipped", reservation=True)
        stock.sync_order(self.add(self.antenna, 2))
        need = sum(e["need"] for e in stock.bom_open_lines([self.cable.pk]))
        self.assertEqual(need, D("5"))
        self.assertEqual(self.nums(self.cable)[1], need)


class QueueWithBomTests(BomReserveBase):
    def setUp(self):
        super().setUp()
        self.kit_order = self.add(self.antenna, 30, deadline_days=5)        # 15 кабелів, дедлайн раніше
        self.cable_order = self.add(self.cable, 10)                          # прямий продаж без дедлайну

    def test_antennas_with_earlier_deadline_get_the_cable_first(self):
        c = self.cover(self.cable_order)
        self.assertEqual((c["free"], c["queue"]["rank"], c["queue"]["short"]), (5, 2, 5))
        self.assertEqual(self.cover(self.kit_order)["bom"], [])

    def test_pinned_cable_order_leaves_antennas_short(self):
        SalesOrder.objects.filter(pk=self.cable_order.pk).update(stock_first_at=timezone.now())
        self.assertEqual(self.cover(self.cable_order)["queue"]["short"], 0)
        bom = self.cover(self.kit_order)["bom"]
        self.assertEqual([(b["sku"], b["short"], b["rank"]) for b in bom], [("CA-1", 5, 2)])

    def test_ship_agent_warns_about_missing_component(self):
        from shipping.services import ship_advice
        SalesOrder.objects.filter(pk=self.cable_order.pk).update(stock_first_at=timezone.now())
        texts = " ".join(c["text"] for c in ship_advice.checks(SalesOrder.objects.get(pk=self.kit_order.pk)))
        self.assertIn("бракує компонента BOM CA-1 для AN-1: 5", texts)

    def test_pages(self):
        SalesOrder.objects.filter(pk=self.cable_order.pk).update(stock_first_at=timezone.now())
        c = self.admin_client()
        r = c.get(f"/admin/sales/salesorder/{self.kit_order.pk}/change/")
        self.assertContains(r, "🔧 CA-1: <b>бракує 5</b>")
        r = c.get("/admin/sales/salesorder/")
        data = json.loads(r.context_data["sv_order_stock_json"])
        self.assertEqual(data[str(self.kit_order.pk)][0]["bom"][0]["short"], "5")
        r = c.get("/admin/inventory/reorderproxy/supply/")
        row = next(x for x in r.context["rows"] if x["product"].pk == self.cable.pk)
        states = [(res["state"], res["qty"], res["queue"]["rank"]) for res in row["reservations"]]
        self.assertEqual(states, [("pending", 10.0, 1), ("bom", 15.0, 2)])
