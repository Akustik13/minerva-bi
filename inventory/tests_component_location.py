"""Компонент рухається там, де лежить (stock.home_location): продаж, списання BOM, ⚖️, прихід закупівлі.

    python manage.py test inventory.tests_component_location --settings=tabele.settings_test

Випадок з робочої бази (11.2026): кабель CA-SMAM-RG316-1000-SMAM (компонент, 0,5 на антену AN220208-01C) лежить на
MAIN (⚖️ пише туди), а продаж антени списував кабель зі «Складу компонентів» — там мінус, на MAIN плюс.
"""
import uuid
from decimal import Decimal as D

from inventory.models import (
    InventoryTransaction, Location, Product, ProductComponent, PurchaseOrder, PurchaseOrderLine, Supplier,
)
from inventory.services import stock
from inventory.tests_stock import StockBase

TX = InventoryTransaction.TxType


class ComponentBase(StockBase):
    def setUp(self):
        super().setUp()
        self.comp_loc = Location.objects.create(code="COMPONENTS", location_type=Location.LocationType.COMPONENTS)
        self.cable = Product.objects.create(sku="CA-SMAM-RG316-1000-SMAM", kind=Product.Kind.COMPONENT)
        self.antenna = Product.objects.create(sku="AN220208-01C", bom_type=Product.BomType.KEY)
        ProductComponent.objects.create(parent=self.antenna, component=self.cable, qty_per=D("0.5"))
        self.receive(self.antenna, 14)

    def put(self, product, qty, location):
        InventoryTransaction.objects.create(tx_type=TX.INCOMING, qty=qty, product=product, location=location,
                                            external_key=f"test:in:{uuid.uuid4()}")

    def by_location(self, product):
        return {r["location"]: r["on_hand"] for r in stock.location_breakdown(product)}


class HomeLocationTests(ComponentBase):
    def test_most_stock_wins(self):
        self.put(self.cable, 3, self.loc)
        self.put(self.cable, 10, self.comp_loc)
        self.assertEqual(stock.home_location(self.cable.pk), self.comp_loc)

    def test_reservations_do_not_count(self):
        self.put(self.cable, 3, self.loc)
        InventoryTransaction.objects.create(tx_type=TX.RESERVED, qty=-5, product=self.cable, location=self.comp_loc,
                                            external_key="test:hold")
        self.assertEqual(stock.home_location(self.cable.pk), self.loc)

    def test_nowhere_is_default_location(self):
        self.assertEqual(stock.home_location(self.cable.pk).code, "MAIN")


class BomTakesFromWhereItLiesTests(ComponentBase):
    def test_cable_on_main_is_taken_from_main(self):
        self.put(self.cable, 14, self.loc)
        self.order((self.antenna, 8))                         # «при створенні»: антена й кабель — одразу
        move = InventoryTransaction.objects.get(product=self.cable, tx_type=TX.OUTGOING)
        self.assertEqual((move.location, move.qty), (self.loc, -4))
        self.assertEqual(self.by_location(self.cable), {"MAIN": 10})      # раніше MAIN 14 і COMPONENTS −4

    def test_cable_on_components_is_taken_from_there(self):
        self.put(self.cable, 14, self.comp_loc)
        self.order((self.antenna, 3))
        move = InventoryTransaction.objects.get(product=self.cable, tx_type=TX.OUTGOING)
        self.assertEqual((move.location, move.qty), (self.comp_loc, D("-1.5")))

    def test_antenna_itself_still_from_order_location(self):
        self.put(self.cable, 14, self.comp_loc)
        self.order((self.antenna, 2))
        move = InventoryTransaction.objects.get(product=self.antenna, tx_type=TX.OUTGOING)
        self.assertEqual(move.location.code, "MAIN")


class ComponentSoldAloneTests(ComponentBase):
    def test_sold_cable_comes_from_where_it_lies(self):
        self.put(self.cable, 10, self.comp_loc)
        self.order((self.cable, 2))
        move = InventoryTransaction.objects.get(product=self.cable, tx_type=TX.OUTGOING)
        self.assertEqual(move.location, self.comp_loc)
        self.assertEqual(self.by_location(self.cable), {"COMPONENTS": 8})

    def test_reservation_mode_reserves_there_too(self):
        self.mode("shipped", reservation=True)
        self.put(self.cable, 10, self.comp_loc)
        self.order((self.cable, 2))
        move = InventoryTransaction.objects.get(product=self.cable, tx_type=TX.RESERVED)
        self.assertEqual(move.location, self.comp_loc)


class SetStockAndReceiptTests(ComponentBase):
    def test_set_stock_of_component_goes_where_it_lies(self):
        self.put(self.cable, 10, self.comp_loc)
        c = self.admin_client()
        r = c.post(f"/admin/inventory/product/{self.cable.pk}/set-stock/", {"target_stock": "12"})
        self.assertEqual(r.status_code, 302)
        adj = InventoryTransaction.objects.get(product=self.cable, tx_type=TX.ADJUSTMENT)
        self.assertEqual((adj.location, adj.qty), (self.comp_loc, 2))

    def test_set_stock_of_finished_product_still_on_main(self):
        self.put(self.antenna, 5, self.loc)
        c = self.admin_client()
        c.post(f"/admin/inventory/product/{self.antenna.pk}/set-stock/", {"target_stock": "20"})
        adj = InventoryTransaction.objects.get(product=self.antenna, tx_type=TX.ADJUSTMENT)
        self.assertEqual(adj.location.code, "MAIN")

    def receive_po(self, product, qty):
        self.mode("creation", add_on_po_receive=True)
        po = PurchaseOrder.objects.create(supplier=Supplier.objects.create(name="Annxin"), status="ordered")
        line = PurchaseOrderLine.objects.create(purchase_order=po, product=product, qty_ordered=qty)
        line.qty_received = qty
        line.save()
        return InventoryTransaction.objects.get(product=product, external_key__startswith="po:")

    def test_purchase_receipt_of_component_goes_where_it_lies(self):
        self.put(self.cable, 4, self.comp_loc)
        tx = self.receive_po(self.cable, 10)
        self.assertEqual(tx.location, self.comp_loc)

    def test_purchase_receipt_does_not_flip_main_location_type(self):
        tx = self.receive_po(self.cable, 10)                  # кабелю ще ніде немає — на основний склад
        self.assertEqual(tx.location.code, "MAIN")
        self.loc.refresh_from_db()
        self.assertEqual(self.loc.location_type, Location.LocationType.FINISHED)   # раніше ставав «Компоненти»
