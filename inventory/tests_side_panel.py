"""📌 Панель праворуч у списку товарів (inventory/side_panel.py): розділи з тими самими числами, що картка й звіт,
права кожного розділу й дії, швидка зміна точки дозамовлення (мінімальна форма — базові ціни не зачіпаються),
«Історія» без «Уся історія», BOM лише для виробів з ключовими компонентами, без мережі.

    python manage.py test inventory.tests_side_panel --settings=tabele.settings_test
"""
import json
import logging
import re
import tempfile
import uuid
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from core import side_panel as sp
from core.models import AuditLog, UserProfile
from inventory.models import (
    IncomingShipment, InventorySettings, InventoryTransaction, Location, Product, ProductComponent,
    ProductPriceHistory, PurchaseOrder, PurchaseOrderLine, Supplier,
)
from inventory.services import product_report, stock
from sales.models import SalesOrder, SalesOrderLine

URL = "/core/panel/inventory.product/"
TX = InventoryTransaction.TxType
BLOCKS = ["stock", "demand", "incoming", "prices", "channels", "sales", "params", "bom", "media", "moves", "offers"]


def cfg_of(html):
    return json.loads(html.split('id="mv-panel-config" type="application/json">')[1].split("</script>")[0])


def section(html, key):
    at = html.index(f'data-sec="{key}"')
    start = html.rindex("<section", 0, at)
    return html[start:html.index("</section>", start)]


def kv_value(html, label):
    """Текст значення рядка «label» у dl.mvp-kv, без тегів (None — рядка немає)."""
    m = re.search(r"<dt>" + re.escape(label) + r"</dt><dd>(.*?)</dd>", html, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


class Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.default_currency = "EUR"
        ss.save()
        quiet = logging.getLogger("django.request")         # 403 / 404 / 409 тут — очікувані відповіді, не шум
        self.addCleanup(quiet.setLevel, quiet.level)
        quiet.setLevel(logging.ERROR)
        # будь-що, що впало в постачальнику (розділ, лічильник, підсумок, шапка, дія), каркас лише записує в журнал —
        # тут це помилка тесту
        self.failures = _Collect()
        log = logging.getLogger("core.side_panel")
        log.addHandler(self.failures)
        self.addCleanup(log.removeHandler, self.failures)
        self.addCleanup(log.setLevel, log.level)
        log.setLevel(logging.WARNING)
        self.mode("creation")
        self.loc = Location.objects.create(code="MAIN", name="Головний склад", location_type="other")
        self.root = User.objects.create_superuser("root", "root@example.com", "p")
        self.client.force_login(self.root)
        self.p = Product.objects.create(sku="FT220407", name="Filter 2.4 GHz", category="filter", reorder_point=5,
                                        sale_price=D("12.50"), purchase_price=D("5"))
        self.receive(self.p, 20)
        self.n = 0

    def mode(self, deduct_on, reservation=False):
        s = InventorySettings.get()
        s.deduct_on, s.use_reservation, s.default_location = deduct_on, reservation, "MAIN"
        s.add_on_po_receive = False
        s.save()

    def receive(self, product, qty, loc=None):
        InventoryTransaction.objects.create(tx_type=TX.INCOMING, qty=qty, product=product, location=loc or self.loc,
                                            external_key=f"test:in:{uuid.uuid4()}")

    def order(self, qty, status="received", product=None, total=None, **kw):
        self.n += 1
        product = product or self.p
        o = SalesOrder.objects.create(source="manual", order_number=f"5{self.n:04d}", status=status,
                                      order_date=timezone.localdate(), client=f"Kunde {self.n}", **kw)
        SalesOrderLine.objects.create(order=o, product=product, sku_raw=product.sku, qty=qty, total_price=total,
                                      currency="EUR")
        return o

    def user(self, name, role, modules=None):
        u = User.objects.create_user(name, f"{name}@example.com", "p", is_staff=True)
        UserProfile.objects.filter(user=u).update(role=role, allowed_modules=modules)
        return User.objects.get(pk=u.pk)

    def shell(self, pk=None):
        r = self.client.get(f"{URL}{pk or self.p.pk}/")
        self.assertEqual(r.status_code, 200, r.content[:300])
        html = r.content.decode()
        self.assertNotIn("data-error", html)
        self.assertEqual(self.failures.records, [])
        return html

    def block(self, key, pk=None):
        r = self.client.get(f"{URL}{pk or self.p.pk}/block/{key}/")
        self.assertEqual(r.status_code, 200, r.content[:300])
        html = r.content.decode()
        self.assertNotIn("data-error", html)
        self.assertEqual(self.failures.records, [])
        return html

    def edit(self, data, pk=None):
        return self.client.post(f"{URL}{pk or self.p.pk}/edit/", json.dumps(data), content_type="application/json")


class ConfigTests(Base):
    def test_product_list_has_panel_config(self):
        html = self.client.get("/admin/inventory/product/").content.decode()
        self.assertIn('id="mv-panel-config"', html)
        cfg = cfg_of(html)
        self.assertEqual((cfg["model"], cfg["url"]), ("inventory.product", URL))
        self.assertEqual([b["key"] for b in cfg["blocks"]], BLOCKS + ["history", "fields", "related"])
        self.assertEqual(cfg["hideDefault"], ["media_col", "shops_col", "kind", "bom_type", "reserved_qty",
                                              "incoming_qty", "buildable_qty", "set_stock_link"])
        self.assertEqual([e["key"] for e in cfg["edits"]], ["reorder_point", "lead_time_days"])
        prefs = cfg["prefs"]
        self.assertEqual(prefs["blocks"], [k for k in BLOCKS if k not in ("moves", "offers")] + ["history", "fields"])
        self.assertEqual(prefs["collapsed"], ["params", "history", "fields"])
        self.assertEqual(prefs["actions"], ["report", "label", "datasheet", "moves_page"])
        label = next(a for a in cfg["actions"] if a["key"] == "label")
        self.assertEqual((label["kind"], label["hotkey"]), ("svl", "KeyL"))
        self.assertNotIn("set_stock", json.dumps(cfg["actions"] + cfg["adminActions"]))

    def test_config_follows_roles(self):
        # складник: склад змінює, продажів / магазину / DigiKey / Angebote не бачить
        self.client.force_login(self.user("sklad", "warehouse"))
        cfg = cfg_of(self.client.get("/admin/inventory/product/").content.decode())
        keys = [b["key"] for b in cfg["blocks"]]
        for hidden in ("demand", "sales", "channels", "offers"):
            self.assertNotIn(hidden, keys)
        self.assertIn("stock", keys)
        self.assertTrue(cfg["canChange"])
        self.assertIn("label", [a["key"] for a in cfg["actions"]])
        # бухгалтер: склад лише переглядає — без швидких змін і без етикеток (модуля DYMO немає)
        self.client.force_login(self.user("buh", "accountant"))
        cfg = cfg_of(self.client.get("/admin/inventory/product/").content.decode())
        self.assertFalse(cfg["canChange"])
        self.assertEqual(cfg["edits"], [])
        self.assertIn("demand", [b["key"] for b in cfg["blocks"]])
        self.assertNotIn("label", [a["key"] for a in cfg["actions"]])


class ShellTests(Base):
    def test_title_subtitle_actions_and_escaping(self):
        self.p.name = "<script>alert(1)</script>"
        self.p.lifecycle_status = "nrnd"
        self.p.is_active = False
        self.p.save()
        html = self.shell()
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("<b>FT220407</b>", html)
        self.assertIn('title="Не рекомендовано для нових розробок (NRND)">NRND</span>', html)
        self.assertIn('<span class="mvp-badge mvp-t-off">неактивний</span>', html)
        self.assertIn("Доступно 20 шт. · filter · 12,50 EUR", html)
        self.assertNotIn("data-history-url", html)                 # history_view товару — рух складу
        self.assertNotIn("set-stock", html)
        with tempfile.TemporaryDirectory() as labels, override_settings(LABELS_DIR=Path(labels)):
            html = self.shell()
            self.assertIn('data-act="label" data-svl="filter" data-sku="FT220407" data-qty="1" data-file=""'
                          ' data-svl-act="print" data-fallback="/labels/product/FT220407/"', html)
            (Path(labels) / "FT220407 Alt.dymo").write_text("<x/>", encoding="utf-8")   # файл з бази етикеток
            self.assertIn('data-sku="FT220407" data-qty="1" data-file="1"', self.shell())
        self.assertIn(f'data-act="report" href="/admin/inventory/product/{self.p.pk}/report/"', html)
        self.assertNotIn('data-act="datasheet"', html)              # даташита немає — кнопки немає
        self.p.datasheet_url = "https://example.com/FT220407.pdf"
        self.p.save()
        html = self.shell()
        self.assertIn('data-act="datasheet" href="https://example.com/FT220407.pdf" target="_blank"', html)

    def test_lazy_and_collapsed_sections_are_deferred(self):
        html = self.shell()
        for key in ("sales", "params", "history", "fields"):
            self.assertIn("data-deferred", section(html, key), key)
        self.assertNotIn("data-deferred", section(html, "stock"))
        self.assertNotIn('data-sec="moves"', html)                  # вимкнено за замовчуванням

    def test_bom_is_not_applicable_without_key_components(self):
        html = self.shell()
        self.assertIn("data-na", section(html, "bom"))
        self.assertIn("Не стосується цього запису.", section(html, "bom"))
        kit = Product.objects.create(sku="KIT-1", name="Kit", bom_type="key")
        cable = Product.objects.create(sku="CA-X", kind="component")
        nut = Product.objects.create(sku="NUT-1", kind="component")
        self.receive(cable, 10)
        self.receive(nut, 3)
        ProductComponent.objects.create(parent=kit, component=cable, qty_per=2)
        ProductComponent.objects.create(parent=kit, component=nut, qty_per=1)
        html = self.shell(pk=kit.pk)
        bom = section(html, "bom")
        self.assertNotIn("data-na", bom)
        self.assertEqual(kv_value(bom, "Можна зібрати"), "3 шт.")
        self.assertEqual(kv_value(bom, "Вузьке місце"), "NUT-1")
        self.assertIn("CA-X", bom)
        self.assertIn('data-tone="ok"', bom)

    def test_roles_without_inventory_get_nothing(self):
        self.client.force_login(self.user("guest", "readonly"))
        self.assertEqual(self.client.get(f"{URL}{self.p.pk}/").status_code, 403)
        self.assertEqual(self.client.get(f"{URL}{self.p.pk}/block/stock/").status_code, 403)


class StockBlockTests(Base):
    def test_numbers_equal_stock_now(self):
        b2 = Location.objects.create(code="B2", name="Полиця B2")
        self.receive(self.p, 5, loc=b2)
        self.order(3)                                   # «при створенні» — списано
        self.mode("creation", reservation=True)
        self.order(4)                                   # резерв
        sup = Supplier.objects.create(name="Mini-Circuits")
        po = PurchaseOrder.objects.create(supplier=sup, status="ordered", code="PO-A")
        PurchaseOrderLine.objects.create(purchase_order=po, product=self.p, qty_ordered=10, qty_received=2)
        cart = PurchaseOrder.objects.create(supplier=sup, status="draft", code="PO-CART")
        PurchaseOrderLine.objects.create(purchase_order=cart, product=self.p, qty_ordered=6)
        s = product_report.stock_now(self.p, timezone.localdate())
        html = section(self.shell(), "stock")
        self.assertEqual(kv_value(html, "На складі"), f"{sp.qty(s['on_hand'])} шт.")
        self.assertEqual(kv_value(html, "Резерв"), f"{sp.qty(s['reserved'])} шт.")
        self.assertEqual(kv_value(html, "Доступно"), f"{sp.qty(s['available'])} шт.")
        self.assertEqual(kv_value(html, "Очікується"), f"{sp.qty(s['incoming'])} шт.")
        self.assertEqual(kv_value(html, "Продажі на міс"), f"{sp.qty(s['monthly'])} шт.")
        self.assertEqual(kv_value(html, "Вистачить на"), f"{sp.qty(s['months_left'])} міс")
        self.assertEqual(kv_value(html, "Стан"), s["status_text"])
        self.assertEqual(kv_value(html, "Що замовити"),
                         f"{sp.qty(s['reorder_qty'])} шт." if s["reorder_qty"] else "не потрібно")
        self.assertEqual(kv_value(html, "У кошику"), "6 шт.")
        self.assertEqual((s["on_hand"], s["reserved"], s["available"], s["incoming"]), (22, 4, 18, 8))
        self.assertIn("📍 По локаціях", html)
        self.assertIn("Полиця B2", html)
        self.assertIn('data-tone="ok"', html)
        self.assertIn('data-edit="reorder_point" data-kind="number" data-value="5"', html)
        self.assertIn('data-edit="lead_time_days" data-kind="number" data-value=""', html)

    def test_badge_tones_and_single_location(self):
        html = section(self.shell(), "stock")
        self.assertIn('data-tone="ok"', html)
        self.assertNotIn("По локаціях", html)          # одна локація й без резерву — таблиці немає
        self.mode("creation", reservation=True)
        self.order(20)                                  # усе зарезервовано
        html = section(self.shell(), "stock")
        self.assertIn('data-tone="err"', html)
        self.assertIn("По локаціях", html)
        InventoryTransaction.objects.filter(tx_type=TX.RESERVED).delete()
        SalesOrder.objects.all().delete()
        self.p.reorder_point = 25
        self.p.save()
        self.assertIn('data-tone="warn"', section(self.shell(), "stock"))

    def test_view_only_role_sees_values_not_controls(self):
        self.client.force_login(self.user("buh", "accountant"))
        html = section(self.shell(), "stock")
        self.assertNotIn("data-edit", html)
        self.assertEqual(kv_value(html, "Точка дозамовлення"), "5")
        self.assertIsNone(kv_value(html, "Термін поставки, днів"))      # порожньо й змінити не можна — рядка немає


class DemandTests(Base):
    def test_open_order_lines_with_their_state(self):
        deducted = self.order(2)                        # «при створенні» — уже списано, ще не відправлено
        self.mode("shipped")
        pending = self.order(3, shipping_deadline=timezone.localdate() - timedelta(days=1))
        self.mode("creation", reservation=True)
        reserved = self.order(4)
        self.mode("creation")
        cancelled = self.order(5, status="cancelled")
        shipped = self.order(1, status="shipped")       # відправлено й списано — уже не чекає
        expected = {line.order.order_number: state for line, state in stock.open_order_lines([self.p.pk])}
        self.assertEqual(expected, {deducted.order_number: "deducted", pending.order_number: "pending",
                                    reserved.order_number: "reserved"})
        html = section(self.shell(), "demand")
        labels = {"deducted": "✓ списано", "pending": "⏳ ще не списано", "reserved": "🔒 резерв"}
        for number, state in expected.items():
            at = html.index(f"№ {number}")
            line = html[at:html.index("</div></div>", at)]
            self.assertIn(labels[state], line, number)
        self.assertNotIn(f"№ {cancelled.order_number}", html)
        self.assertNotIn(f"№ {shipped.order_number}", html)
        self.assertIn('data-count="3"', html)
        self.assertIn("⚠ строк відправки минув", html)
        self.assertIn("Разом чекають: 9 шт.", html)
        self.assertIn(f'href="/admin/sales/salesorder/{pending.pk}/change/"', html)

    def test_role_without_sales_has_no_demand(self):
        self.order(2)
        self.client.force_login(self.user("sklad", "warehouse"))
        self.assertNotIn('data-sec="demand"', self.shell())
        self.assertEqual(self.client.get(f"{URL}{self.p.pk}/block/demand/").status_code, 404)


class IncomingTests(Base):
    def test_open_quantity_and_received_lines_excluded(self):
        sup = Supplier.objects.create(name="Mini-Circuits")
        week = timezone.localdate() + timedelta(days=7)
        open_po = PurchaseOrder.objects.create(supplier=sup, status="ordered", code="PO-OPEN", expected_date=week)
        PurchaseOrderLine.objects.create(purchase_order=open_po, product=self.p, qty_ordered=10, qty_received=4)
        done = PurchaseOrder.objects.create(supplier=sup, status="partial", code="PO-DONE")
        PurchaseOrderLine.objects.create(purchase_order=done, product=self.p, qty_ordered=5, qty_received=7)
        received = PurchaseOrder.objects.create(supplier=sup, status="received", code="PO-RECV")
        PurchaseOrderLine.objects.create(purchase_order=received, product=self.p, qty_ordered=7)
        cart = PurchaseOrder.objects.create(supplier=sup, status="draft", code="PO-CART")
        PurchaseOrderLine.objects.create(purchase_order=cart, product=self.p, qty_ordered=3)
        IncomingShipment.objects.create(purchase_order=open_po, carrier_name="DHL", tracking_number="1Z999",
                                        status="in_transit")
        IncomingShipment.objects.create(purchase_order=open_po, carrier_name="UPS", tracking_number="OLD-1",
                                        status="delivered")
        opened = {line.purchase_order.code: line.open_qty for line in
                  PurchaseOrderLine.objects.filter(product=self.p, purchase_order__status__in=stock.OPEN_PO_STATUSES)
                  .annotate(open_qty=stock.po_open_qty()).filter(open_qty__gt=0).select_related("purchase_order")}
        self.assertEqual(opened, {"PO-OPEN": 6, "PO-CART": 3})
        html = section(self.shell(), "incoming")
        self.assertIn("PO-OPEN", html)
        self.assertIn("6 шт. з 10", html)
        self.assertIn(f"очікується {sp.date(week)}", html)
        self.assertIn("1Z999", html)
        self.assertNotIn("OLD-1", html)                 # отримане відправлення не показується
        at = html.index("PO-CART")
        self.assertIn("у кошику", html[at:html.index("</div></div>", at)])
        self.assertIn("3 шт.", html[at:])
        for code in ("PO-DONE", "PO-RECV"):
            self.assertNotIn(code, html)
        self.assertIn('data-count="2"', html)
        self.assertIn("Mini-Circuits", html)

    def test_role_without_purchases(self):
        buh = self.user("buh", "accountant")
        UserProfile.objects.filter(user=buh).update(denied_models=["inventory:PurchaseOrder"])
        self.client.force_login(User.objects.get(pk=buh.pk))
        html = self.shell()
        self.assertNotIn('data-sec="incoming"', html)
        self.assertIn('data-sec="stock"', html)


class ChannelsTests(Base):
    def setUp(self):
        super().setUp()
        from bots.models import DigiKeyListing
        from shop.models import Shop, ShopListing
        shop = Shop.objects.create(name="Sevskiy Shop", slug="webshop", is_default=True)
        ShopListing.objects.create(shop=shop, product=self.p, price=D("14.90"))
        DigiKeyListing.objects.create(product=self.p, dk_title="DK FT220407", dk_description="Filter",
                                      sync_status="published", dk_prices=[{"qty": 1, "price": 15.5}],
                                      dk_quantity_available=12)

    def test_superuser_sees_shops_and_digikey(self):
        html = section(self.shell(), "channels")
        self.assertIn("🏪 Магазини", html)
        self.assertIn("Sevskiy Shop", html)
        self.assertIn("14,90 EUR", html)
        self.assertIn("🔷 DigiKey", html)
        self.assertEqual(kv_value(html, "Залишок на DigiKey"), "12 шт.")
        self.assertIn('data-count="2"', html)
        prices = section(self.shell(), "prices")
        self.assertIn("DigiKey, ", prices)              # ступені Minerva / DigiKey поруч

    def test_parts_hidden_without_shop_or_bots_permission(self):
        self.client.force_login(self.user("shopper", "warehouse", ["inventory", "shop"]))
        html = self.block("channels")
        body = html[html.index('class="mvp-sb"'):]
        self.assertIn("🏪 Магазини", body)
        self.assertIn("Sevskiy Shop", body)
        self.assertNotIn("DigiKey", body)                           # ні частини DigiKey, ні бейджа
        self.assertNotIn("digikeylisting", body)
        self.assertNotIn("DigiKey, ", section(self.shell(), "prices"))
        self.assertNotIn('data-act="dk"', self.shell())

        self.client.force_login(self.user("dk", "warehouse", ["inventory", "bots"]))
        html = self.block("channels")
        body = html[html.index('class="mvp-sb"'):]
        self.assertIn("🔷 DigiKey", body)
        self.assertNotIn("Магазини", body)
        self.assertNotIn("Sevskiy Shop", body)
        self.assertNotIn("shoplisting", body)

        self.client.force_login(self.user("sklad", "warehouse"))
        self.assertNotIn('data-sec="channels"', self.shell())
        self.assertEqual(self.client.get(f"{URL}{self.p.pk}/block/channels/").status_code, 404)


class PricesTests(Base):
    def test_foreign_base_currency_warns_and_links_only_for_editors(self):
        self.p.base_prices = [{"min_qty": 1, "unit_price": "4.89"}, {"min_qty": 10, "unit_price": "4.52"}]
        self.p.base_price_currency = "USD"
        self.p.save()
        html = section(self.shell(), "prices")
        self.assertIn('data-tone="warn"', html)
        self.assertIn("⚠ Базові ціни в USD, а валюта Minerva — EUR.", html)
        self.assertIn(f"/admin/inventory/product/base-currency/?ids={self.p.pk}", html)
        self.assertIn("Minerva, USD", html)
        self.assertIn("4,89", html)
        self.assertEqual(kv_value(html, "Ціна закупки"), "5,00 EUR маржа 60,0 %")
        self.client.force_login(self.user("buh", "accountant"))
        html = section(self.shell(), "prices")
        self.assertIn("⚠ Базові ціни в USD", html)
        self.assertNotIn("base-currency", html)


class EditTests(Base):
    def test_reorder_point_edit_keeps_base_prices_and_writes_audit(self):
        self.p.base_prices = [{"min_qty": 1, "unit_price": "4.8900"}, {"min_qty": 10, "unit_price": "4.5200"}]
        self.p.base_price_currency = "USD"
        self.p.save()
        before = Product.objects.get(pk=self.p.pk)
        history = ProductPriceHistory.objects.count()
        r = self.edit({"key": "reorder_point", "value": "15", "expect": "5"})
        self.assertEqual(r.status_code, 200, r.content[:300])
        j = r.json()
        self.assertTrue(j["ok"])
        self.assertEqual((j["value"], j["message"]), ("15", "Точка дозамовлення: 15"))
        self.assertEqual(j["undo"], {"key": "reorder_point", "value": "5"})
        self.assertEqual(list(j["blocks"]), ["stock"])
        self.assertIn('data-value="15"', j["blocks"]["stock"])
        self.assertEqual(set(j["cells"]), {"reorder_badge", "stock_badge"})
        p = Product.objects.get(pk=self.p.pk)
        self.assertEqual(p.reorder_point, 15)
        self.assertEqual(p.base_prices, before.base_prices)
        self.assertEqual(p.base_price_currency, "USD")
        self.assertEqual(ProductPriceHistory.objects.count(), history)
        audit = AuditLog.objects.filter(action=AuditLog.Action.UPDATE, object_id=str(p.pk))
        self.assertEqual(audit.count(), 1)
        self.assertEqual(audit.get().extra["changed_fields"], ["reorder_point"])
        self.assertEqual(LogEntry.objects.filter(object_id=str(p.pk)).count(), 1)

    def test_lead_time_stale_and_permissions(self):
        r = self.edit({"key": "lead_time_days", "value": "21", "expect": ""})
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(Product.objects.get(pk=self.p.pk).lead_time_days, 21)
        r = self.edit({"key": "lead_time_days", "value": "", "expect": "21"})
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertIsNone(Product.objects.get(pk=self.p.pk).lead_time_days)
        r = self.edit({"key": "reorder_point", "value": "9", "expect": "3"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(Product.objects.get(pk=self.p.pk).reorder_point, 5)
        self.assertEqual(self.edit({"key": "reorder_point", "value": "-1"}).status_code, 400)
        self.assertEqual(self.edit({"key": "base_price_currency", "value": "EUR"}).status_code, 404)
        self.client.force_login(self.user("buh", "accountant"))
        self.assertEqual(self.edit({"key": "reorder_point", "value": "9"}).status_code, 403)
        self.assertEqual(Product.objects.get(pk=self.p.pk).reorder_point, 5)


class HistoryTests(Base):
    def test_history_has_no_full_history_link(self):
        self.assertEqual(self.edit({"key": "reorder_point", "value": "8"}).status_code, 200)
        html = self.block("history")
        self.assertIn("root", html)
        self.assertNotIn("Уся історія", html)
        self.assertNotIn("/history/", html)


class LazyBlocksTests(Base):
    def test_sales_block_uses_kpis_and_draws_bars(self):
        self.order(2, total=D("25"))
        self.order(3, total=D("36"), status="cancelled")
        k = product_report.kpis(self.p, months=12, listing=None)
        html = self.block("sales")
        self.assertEqual(kv_value(html, "Продано").split()[0:2], [sp.qty(k["units_12"]), "шт."])
        self.assertTrue(kv_value(html, "Виручка").startswith(k["revenue_12"].full_text))
        self.assertEqual(kv_value(html, "Замовлень"), str(k["orders_12"]))
        self.assertIn('<svg class="mvp-spark"', html)
        self.assertIn(f"/admin/inventory/product/{self.p.pk}/report/", html)
        self.assertIn("Kunde 1", html)

    def test_sales_block_empty_without_sales(self):
        self.assertIn("data-empty", self.block("sales"))

    def test_moves_and_offers_blocks(self):
        self.order(2)
        html = self.block("moves")
        self.assertIn("➖ Витрата", html)
        self.assertIn("замовлення № 50001", html)
        self.assertIn(f"/admin/inventory/product/{self.p.pk}/history/", html)
        from offers.models import Offer, OfferLine
        offer = Offer.objects.create(contact_name="Max Muster", currency="EUR")
        OfferLine.objects.create(offer=offer, product=self.p, sku=self.p.sku, qty=35, unit_price=D("11.2"))
        html = self.block("offers")
        self.assertIn(offer.display_number, html)
        self.assertIn(f"/offers/{offer.pk}/quick/", html)
        self.assertIn("Max Muster", html)
        self.assertIn('data-count="1"', html)

    def test_every_block_renders_without_network(self):
        from bots.models import DigiKeyListing
        DigiKeyListing.objects.create(product=self.p, dk_title="DK", dk_description="Filter", sync_status="staged",
                                      dk_prices=[{"qty": 1, "price": 15.5}])
        self.p.tech_attributes = {f"Param {i}": f"v{i}" for i in range(14)} | {"rohsStatus": "ROHS3"}
        self.p.notes = "Нотатка"
        self.p.save()
        boom = AssertionError("мережа в панелі")
        with mock.patch("requests.Session.request", side_effect=boom) as net, \
                mock.patch("bots.services.dk_prices.row_for", side_effect=boom) as row_for, \
                mock.patch("bots.admin_prices.product_panel", side_effect=boom) as panel:
            self.shell()
            for key in BLOCKS + ["history", "fields", "related"]:
                if key == "bom":
                    continue
                self.block(key)
        net.assert_not_called()
        row_for.assert_not_called()
        panel.assert_not_called()
        params = self.block("params")
        self.assertIn("ще 4", params)
        self.assertNotIn("rohsStatus", params)
        self.assertIn("Нотатка", params)
