"""🔀 Переміщення між складами (stock.move_stock / move_all) і «📍 Звідки» в BOM — inventory/admin_locations.py.

    python manage.py test inventory.tests_stock_locations --settings=tabele.settings_test
"""
import uuid
from decimal import Decimal as D

from django.contrib.auth.models import User

from inventory.models import InventoryTransaction, Location, Product, ProductComponent
from inventory.services import stock
from inventory.tests_stock import StockBase

TX = InventoryTransaction.TxType


class LocationsBase(StockBase):
    def setUp(self):
        super().setUp()
        self.comp = Location.objects.create(code="COMPONENTS", name="Склад компонентів",
                                            location_type=Location.LocationType.COMPONENTS)
        self.boards = Location.objects.create(code="BOARDS", name="Плати", location_type=Location.LocationType.OTHER)
        self.cable = Product.objects.create(sku="CA-SMAM-RG316-1000-SMAM", kind=Product.Kind.COMPONENT)
        self.antenna = Product.objects.create(sku="AN220208-01C", bom_type=Product.BomType.KEY)
        ProductComponent.objects.create(parent=self.antenna, component=self.cable, qty_per=D("0.5"))
        self.receive(self.antenna, 14)
        self.receive(self.cable, 14)                              # кабель на MAIN — як на робочій базі

    def where(self, product):
        return {r["code"]: r["qty"] for r in stock.on_hand_by_location([product.pk]).get(product.pk, [])}


class MoveStockTests(LocationsBase):
    def test_moves_with_two_adjustments_and_keeps_total(self):
        out_tx, in_tx = stock.move_stock(product=self.cable, source=self.loc, target=self.comp, qty=14)
        self.assertEqual((out_tx.qty, out_tx.location, in_tx.qty, in_tx.location), (-14, self.loc, 14, self.comp))
        self.assertEqual((out_tx.tx_type, in_tx.tx_type), (TX.ADJUSTMENT, TX.ADJUSTMENT))
        self.assertTrue(out_tx.external_key.endswith(":out") and in_tx.external_key.endswith(":in"))
        self.assertEqual(out_tx.external_key[:-4], in_tx.external_key[:-3])
        self.assertEqual(self.where(self.cable), {"COMPONENTS": 14})
        self.assertEqual(self.nums(self.cable), (14, 0, 14))

    def test_part_and_where_most_lies_wins(self):
        stock.move_stock(product=self.cable, source=self.loc, target=self.comp, qty=10)
        self.assertEqual(self.where(self.cable), {"COMPONENTS": 10, "MAIN": 4})
        self.assertEqual(stock.home_location(self.cable.pk), self.comp)

    def test_bom_takes_from_new_place_after_move(self):
        stock.move_stock(product=self.cable, source=self.loc, target=self.comp, qty=14)
        self.order((self.antenna, 2))
        move = InventoryTransaction.objects.get(product=self.cable, tx_type=TX.OUTGOING)
        self.assertEqual(move.location, self.comp)

    def test_refusals(self):
        cases = [
            dict(source=self.loc, target=self.loc, qty=1),        # той самий склад
            dict(source=self.loc, target=self.comp, qty=15),      # більше, ніж лежить
            dict(source=self.loc, target=self.comp, qty=0),
            dict(source=self.loc, target=self.comp, qty=D("2.5")),  # частина штук — лише цілими
            dict(source=self.comp, target=self.loc, qty=1),       # на COMPONENTS кабелю немає
        ]
        for kw in cases:
            with self.subTest(**{k: str(v) for k, v in kw.items()}):
                with self.assertRaises(stock.StockError):
                    stock.move_stock(product=self.cable, **kw)
        self.assertEqual(self.where(self.cable), {"MAIN": 14})

    def test_component_not_to_finished_goods_but_to_other(self):
        products = Location.objects.create(code="PRODUCTS", location_type=Location.LocationType.FINISHED)
        stock.move_stock(product=self.cable, source=self.loc, target=self.comp, qty=14)
        with self.assertRaises(stock.StockError):
            stock.move_stock(product=self.cable, source=self.comp, target=products, qty=1)
        stock.move_stock(product=self.cable, source=self.comp, target=self.boards, qty=1)   # «Інше» — можна
        self.assertEqual(self.where(self.cable), {"COMPONENTS": 13, "BOARDS": 1})

    def test_inactive_target_refused(self):
        self.comp.is_active = False
        self.comp.save()
        with self.assertRaises(stock.StockError):
            stock.move_stock(product=self.cable, source=self.loc, target=self.comp, qty=1)

    def test_half_left_can_be_moved_as_whole(self):
        self.order((self.antenna, 1))                           # кабель 14 − 0,5 = 13,5
        stock.move_stock(product=self.cable, source=self.loc, target=self.comp, qty=D("13.5"))
        self.assertEqual(self.where(self.cable), {"COMPONENTS": D("13.5")})

    def test_reservations_follow_when_everything_moves(self):
        self.mode("shipped", reservation=True)
        self.order((self.cable, 2))                             # резерв кабелю на MAIN
        stock.move_stock(product=self.cable, source=self.loc, target=self.comp, qty=14)
        hold = InventoryTransaction.objects.get(product=self.cable, tx_type=TX.RESERVED)
        self.assertEqual(hold.location, self.comp)
        rows = {r["location"]: r for r in stock.location_breakdown(self.cable)}
        self.assertEqual((rows["COMPONENTS"]["on_hand"], rows["COMPONENTS"]["reserved"]), (14, 2))

    def test_pending_order_shows_where_component_lies(self):
        self.mode("shipped")
        stock.move_stock(product=self.cable, source=self.loc, target=self.comp, qty=14)
        self.order((self.cable, 3))                             # ще не списано — бронь там, звідки спишуть
        rows = {r["location"]: r for r in stock.location_breakdown(self.cable)}
        self.assertEqual(rows["COMPONENTS"]["reserved"], 3)
        self.assertEqual(rows["MAIN"]["reserved"], 0)

    def test_move_all(self):
        stock.move_stock(product=self.cable, source=self.loc, target=self.boards, qty=4)
        done = stock.move_all(self.cable, self.comp)
        self.assertEqual(sorted((s.code, q) for s, q in done), [("BOARDS", 4), ("MAIN", 10)])
        self.assertEqual(self.where(self.cable), {"COMPONENTS": 14})


class MovePagesTests(LocationsBase):
    def test_card_shows_locations_and_bom_where(self):
        c = self.admin_client()
        r = c.get(f"/admin/inventory/product/{self.antenna.pk}/change/")
        self.assertContains(r, "🔀 Перемістити між складами")
        self.assertContains(r, "📍 Звідки")
        self.assertContains(r, "📍 MAIN</b> 14")
        r = c.get("/admin/inventory/reorderproxy/supply/")
        row = next(x for x in r.context["rows"] if x["product"].pk == self.antenna.pk)
        self.assertEqual(row["bom_info"]["components"][0]["where"], "MAIN 14")

    def test_single_page_moves(self):
        c = self.admin_client()
        url = f"/admin/inventory/product/{self.cable.pk}/move-stock/"
        self.assertContains(c.get(url), "Де лежить")
        r = c.post(url, {"source": self.loc.pk, "target": self.comp.pk, "qty": ""})   # порожньо — усе
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.where(self.cable), {"COMPONENTS": 14})
        r = c.post(url, {"source": self.comp.pk, "target": self.boards.pk, "qty": "99"}, follow=True)
        self.assertContains(r, "перемістити 99 не можна")
        self.assertEqual(self.where(self.cable), {"COMPONENTS": 14})

    def test_bulk_action_moves_everything(self):
        c = self.admin_client()
        r = c.post("/admin/inventory/product/", {"action": "action_move_stock",
                                                 "_selected_action": [self.cable.pk, self.antenna.pk]})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/inventory/product/move-stock/?ids=", r["Location"])
        r = c.post("/admin/inventory/product/move-stock/",
                   {"ids": f"{self.cable.pk},{self.antenna.pk}", "target": self.comp.pk})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.where(self.cable), {"COMPONENTS": 14})
        self.assertEqual(self.where(self.antenna), {"MAIN": 14})      # готовий товар — не на склад компонентів
        bad = next(x for x in r.context["results"] if x["product"].pk == self.antenna.pk)
        self.assertFalse(bad["ok"])

    def test_viewer_cannot_move(self):
        from config.models import SystemSettings
        from core.models import UserProfile
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        u = User.objects.create_user("viewer", "v@example.com", "p", is_staff=True)
        prof, _ = UserProfile.objects.get_or_create(user=u)
        prof.role = "readonly"
        prof.save()
        self.client.force_login(u)
        r = self.client.post(f"/admin/inventory/product/{self.cable.pk}/move-stock/",
                             {"source": self.loc.pk, "target": self.comp.pk, "qty": "1"})
        self.assertIn(r.status_code, (302, 403))
        r = self.client.post("/admin/inventory/product/move-stock/", {"ids": str(self.cable.pk), "target": self.comp.pk})
        self.assertIn(r.status_code, (302, 403))
        self.assertEqual(self.where(self.cable), {"MAIN": 14})
