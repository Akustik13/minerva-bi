"""🛒 Замовлення JLCPCB ↔ закупівля Minerva (inventory.PurchaseOrder) — закупівлі в одному місці.

Кожне замовлення JLC (крім «Тільки трекінг» / «Ігнорувати») має свою закупівлю: постачальник JLCPCB,
external_source «jlcpcb» + external_id = ID замовлення JLC (повтор не створює дубль), позиції — файли замовлення
(JLCOrderLine; без них — саме замовлення), external_line_id «jlc:<pk позиції>». Так плати, що виготовляються,
видно в «Очікується» складу й «Що замовити», в історії постачальника, у проєктах. Список JLC лишається технічним
виглядом (Gerber, виробництво, трекінг) з посиланням на закупівлю.

  sync(order)                 — створити / оновити закупівлю (шапка, позиції, статус); вручну прив'язана закупівля
                                (не «jlcpcb») — лише прив'язка, позиції не чіпаємо; замовлення, доставлене ДО появи
                                закупівлі, — історія: позиції «отримано» без руху складу;
  receive(order, user, line)  — оприбуткувати доставлене одним шляхом — позиції закупівлі (залишок): «Додавати при
                                надходженні» (InventorySettings.add_on_po_receive) — сигнал складу, інакше рух
                                напряму, як раніше в JLC; позиції без товару лише закриваються; позначки отриманого —
                                і в позиціях JLC;
  on_delivered(order, user)   — замовлення вже «Доставлено»: закупівля «прибула», авто-оприбуткування (JLCConfig);
  deliver(order, source, user)— посилка доставлена (перевізник, DHL, вручну, ETA): статус, сповіщення, on_delivered;
  sync_all()                  — закупівлі для всіх замовлень без неї й відкритих (cron sync_jlc_orders і кнопка).
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

SOURCE = "jlcpcb"
ORDER_KEY = "jlc:order"
HISTORY_DAYS = 14        # доставлено давніше й закупівлі ще не було — історія: «отримано» без руху складу
ZERO = Decimal("0")


def _skip(order) -> bool:
    from jlcpcb.models import JLCOrder
    return order.mapping_status in (JLCOrder.MappingStatus.TRACK_ONLY, JLCOrder.MappingStatus.IGNORED)


def supplier():
    """Картка постачальника JLCPCB: наявна (назва / пошта / сайт / домени з «jlcpcb»), інакше нова."""
    from inventory.models import Supplier
    s = Supplier.objects.filter(name__iexact="JLCPCB").first() or Supplier.objects.filter(
        Q(name__icontains="jlcpcb") | Q(email__icontains="jlcpcb") | Q(website__icontains="jlcpcb")
        | Q(domains__icontains="jlcpcb")).order_by("pk").first()
    if s is None:
        s = Supplier.objects.create(name="JLCPCB", website="https://jlcpcb.com", currency="USD", addr_country="CN")
    return s


def _items(order) -> list:
    """Що замовлено: [{key, product, qty, total, name, sku, received, jlc_line}] — позиції JLC або саме замовлення."""
    from jlcpcb import contents
    lines = list(order.lines.select_related("product").all())
    out = []
    if lines:
        for ln in lines:
            out.append({"key": f"jlc:{ln.pk}", "product": ln.product, "qty": Decimal(ln.quantity or 0),
                        "total": ln.price, "name": ln.file_name, "sku": contents._guess_sku(ln.file_name),
                        "received": Decimal(ln.received_qty or 0), "jlc_line": ln})
        # колись оприбутковано все замовлення (order.product × order.quantity — перша позиція) — це та позиція
        got = Decimal(order.received_qty or 0)
        if got > 0 and order.product_id:
            for it in out:
                if it["product"] is not None and it["product"].pk == order.product_id and it["received"] < it["qty"]:
                    it["received"] = min(it["qty"], max(it["received"], got))
                    break
    else:
        total = order.total_price if order.total_price is not None else (
            order.unit_price * order.quantity if order.unit_price is not None else None)
        out.append({"key": ORDER_KEY, "product": order.product, "qty": Decimal(order.quantity or 0), "total": total,
                    "name": order.description or order.jlc_order_number or order.jlc_order_id,
                    "sku": contents._guess_sku(order.description), "received": Decimal(order.received_qty or 0),
                    "jlc_line": None})
    return out


def _unit(total, qty):
    if total is None or not qty:
        return None
    return (Decimal(total) / qty).quantize(Decimal("0.01"))


def sync(order, history=None):
    """Закупівля для замовлення JLC — створити або оновити; повертає PurchaseOrder або None."""
    from inventory.models import PurchaseOrder, PurchaseOrderLine
    from inventory.services import purchases
    from jlcpcb.models import JLCOrder
    with transaction.atomic():
        order = JLCOrder.objects.select_for_update().get(pk=order.pk)
        po = order.purchase_order or PurchaseOrder.objects.filter(
            external_source=SOURCE, external_id=order.jlc_order_id[:64]).first()
        if _skip(order):
            # «Тільки трекінг» / «Ігнорувати»: власна закупівля без отриманого — скасувати, щоб не «очікувалась»
            if po is not None and po.external_source == SOURCE and po.status not in (
                    PurchaseOrder.Status.CANCELLED, PurchaseOrder.Status.RECEIVED) and \
                    not po.lines.filter(qty_received__gt=0).exists():
                purchases.cancel(po, "у JLCPCB позначено «не відстежувати»")
            return po
        created = po is None
        if created:
            if order.local_status == JLCOrder.LocalStatus.CANCELLED:
                return None
            po = PurchaseOrder.objects.create(
                supplier=supplier(), status=PurchaseOrder.Status.ORDERED,
                order_date=order.order_date or timezone.localdate(), expected_date=order.expected_date,
                currency=(order.currency or "USD")[:8], external_source=SOURCE, external_id=order.jlc_order_id[:64],
                supplier_order_no=(order.jlc_order_number or order.jlc_order_id)[:64],
                notes=f"🔧 Замовлення JLCPCB {order.jlc_order_number or order.jlc_order_id} — Minerva веде цю "
                      f"закупівлю сама з JLCPCB (Склад → Замовлення JLCPCB).")
            if history is None:
                day = order.delivered_date or order.order_date
                history = order.local_status == JLCOrder.LocalStatus.DELIVERED and bool(day) and                     (timezone.localdate() - day).days > HISTORY_DAYS
        if po.external_source == SOURCE:
            _sync_lines(po, order, history=bool(created and history))
        if order.purchase_order_id != po.pk:
            JLCOrder.objects.filter(pk=order.pk).update(purchase_order=po)
            order.purchase_order = po
        return po


def _sync_lines(po, order, history=False):
    from inventory.models import PurchaseOrder, PurchaseOrderLine
    from inventory.services import purchases
    from jlcpcb.models import JLCOrder
    cur = (order.currency or "USD")[:8]
    items = _items(order)
    existing = {ln.external_line_id: ln for ln in po.lines.all()}
    for it in items:
        ln = existing.pop(it["key"], None)
        unit = _unit(it["total"], it["qty"])
        if ln is None:
            ln = PurchaseOrderLine.objects.create(
                purchase_order=po, product=it["product"], description=it["name"][:255], qty_ordered=it["qty"],
                unit_price=unit, currency=cur, external_line_id=it["key"], external_sku=(it["sku"] or "")[:100])
        else:
            upd = {"qty_ordered": it["qty"], "unit_price": unit, "description": it["name"][:255],
                   "external_sku": (it["sku"] or "")[:100], "currency": cur}
            if it["product"] is not None and (ln.product_id is None or ln.qty_received == 0):
                upd["product"] = it["product"]
            PurchaseOrderLine.objects.filter(pk=ln.pk).update(**upd)
        # оприбутковано раніше в JLC (рух складу вже є) — лише позначка в закупівлі, без нового руху (update)
        got = it["qty"] if history else min(it["received"], it["qty"])
        if got > ln.qty_received:
            PurchaseOrderLine.objects.filter(pk=ln.pk).update(qty_received=got)
    for ln in existing.values():          # позиції JLC більше немає — прибрати, якщо нічого не отримано
        if ln.external_line_id.startswith("jlc:") and ln.qty_received == 0:
            ln.delete()
    totals = [it["total"] for it in items]
    total = sum((Decimal(t) for t in totals), ZERO) if totals and all(t is not None for t in totals) else (
        order.total_price)
    skus = ", ".join(dict.fromkeys((it["product"].sku if it["product"] is not None else it["sku"])
                                   for it in items if (it["product"] is not None or it["sku"])))
    PurchaseOrder.objects.filter(pk=po.pk).update(
        expected_date=order.expected_date or po.expected_date,
        tracking_number=(order.tracking_number or po.tracking_number)[:255],
        shipping_service=(order.tracking_carrier or po.shipping_service)[:255],
        supplier_order_no=(order.jlc_order_number or order.jlc_order_id)[:64],
        order_date=order.order_date or po.order_date, currency=cur, total_price=total,
        ordered_skus=skus[:500], updated_at=timezone.now())
    po.refresh_from_db()
    if history:
        PurchaseOrder.objects.filter(pk=po.pk).update(
            notes=(po.notes + "\n" if po.notes else "") + "Доставлено до появи закупівлі — позиції позначено "
                  "отриманими без руху складу (що оприбутковували в JLC — уже на складі).",
            received_date=order.delivered_date or po.received_date)
        po.refresh_from_db()
    if order.local_status == JLCOrder.LocalStatus.CANCELLED and not po.lines.filter(qty_received__gt=0).exists():
        if po.status != PurchaseOrder.Status.CANCELLED:
            purchases.cancel(po, "скасовано в JLCPCB")
    else:
        purchases.refresh_status(po)


def has_open_stock_lines(order) -> bool:
    """Є що оприбуткувати: позиції з товаром, отримано менше, ніж замовлено (після sync)."""
    po = order.purchase_order
    if po is None:
        return False
    return any(ln.product_id and ln.qty_received < ln.qty_ordered for ln in po.lines.all())


def _locked_lines(po):
    """Позиції закупівлі під блокуванням (у transaction.atomic). of=("self",): товар позиції необов'язковий
    (LEFT JOIN), а PostgreSQL не блокує «nullable side of an outer join» — без of прийом падав з 500 (SQLite
    блокувань не робить, тож звичайні тести цього не бачили; перевірка — tests_purchase.PostgresLockTests)."""
    from inventory.models import PurchaseOrderLine
    return (PurchaseOrderLine.objects.select_for_update(of=("self",)).filter(purchase_order=po)
            .select_related("product"))


def receive(order, user=None, line=None) -> list:
    """Оприбуткувати доставлене — залишок позицій закупівлі (усіх або однієї позиції JLC line).
    Повертає [(артикул, к-сть)] того, що додано на склад."""
    from inventory.models import (InventorySettings, InventoryTransaction, Location, PurchaseOrder,
                                  PurchaseOrderLine)
    from inventory.services import purchases
    from jlcpcb.models import JLCConfig, JLCOrder, JLCOrderLine
    po = sync(order, history=False)
    if po is None or po.status == PurchaseOrder.Status.CANCELLED:
        return []
    direct = not InventorySettings.get().add_on_po_receive
    out = []
    with transaction.atomic():
        qs = _locked_lines(po)
        if line is not None:
            qs = qs.filter(external_line_id=f"jlc:{line.pk}")
        for pl in qs:
            rest = pl.qty_ordered - pl.qty_received
            if rest <= 0:
                continue
            if not pl.product_id:
                if line is None:      # без товару: на склад нічого, лише «прибуло», щоб закупівля закрилась
                    PurchaseOrderLine.objects.filter(pk=pl.pk).update(qty_received=pl.qty_ordered)
                continue
            if direct:                # «Додавати при надходженні» вимкнено — як раніше в JLC, рух напряму
                cfg = JLCConfig.get()
                loc_code = cfg.default_location or "MAIN"
                location, _ = Location.objects.get_or_create(code=loc_code, defaults={"name": loc_code})
                key = f"jlc-{order.jlc_order_id}-po-{pl.pk}"
                if not InventoryTransaction.objects.filter(external_key=key).exists():
                    InventoryTransaction.objects.create(
                        tx_type=InventoryTransaction.TxType.INCOMING, product=pl.product, location=location,
                        qty=rest, ref_doc=f"JLC-{order.jlc_order_id}", external_key=key, performed_by=user)
                PurchaseOrderLine.objects.filter(pk=pl.pk).update(qty_received=pl.qty_ordered)
            else:
                pl.qty_received = pl.qty_ordered
                pl.save()             # сигнал складу → Incoming (ref PO-…)
            out.append((pl.product.sku, rest))
            if pl.external_line_id.startswith("jlc:") and pl.external_line_id != ORDER_KEY:
                JLCOrderLine.objects.filter(pk=int(pl.external_line_id[4:])).update(received_qty=pl.qty_ordered)
        po.refresh_from_db()
        purchases.refresh_status(po)
        if po.status == PurchaseOrder.Status.RECEIVED or out and not has_open_stock_lines(order):
            JLCOrder.objects.filter(pk=order.pk).update(received_qty=Decimal(order.quantity or 0))
    return out


def on_delivered(order, user=None) -> list:
    """Замовлення JLC «Доставлено»: закупівля — «прибула», і якщо JLCConfig.auto_receive_on_delivered — на склад."""
    from inventory.models import PurchaseOrder
    from jlcpcb.models import JLCConfig
    po = sync(order, history=False)
    if po is None:
        return []
    if not po.delivered_reported_at:
        PurchaseOrder.objects.filter(pk=po.pk).update(delivered_reported_at=timezone.now())
    if JLCConfig.get().auto_receive_on_delivered:
        return receive(order, user)
    return []


def deliver(order, source="", user=None) -> list:
    """Посилка доставлена: «Доставлено» (дата — з посилки, інакше сьогодні), сповіщення, закупівля, на склад."""
    from jlcpcb.models import JLCOrder
    from jlcpcb.notifications import notify_jlc_status_change
    if order.local_status in (JLCOrder.LocalStatus.DELIVERED, JLCOrder.LocalStatus.CANCELLED):
        return []
    sync(order, history=False)                # закупівля — ще до «Доставлено» (не історія)
    order.refresh_from_db()
    old = order.local_status
    order.local_status = JLCOrder.LocalStatus.DELIVERED
    order.delivered_date = order.delivered_date or timezone.localdate()
    order.save(update_fields=["local_status", "delivered_date", "updated_at"])
    logger.info("JLC %s: %s → delivered (%s)", order.jlc_order_id, old, source)
    try:
        notify_jlc_status_change(order, old, "delivered")
    except Exception:  # noqa: BLE001 — без сповіщення доставка однаково записана
        logger.warning("JLC %s: notify failed", order.jlc_order_id, exc_info=True)
    return on_delivered(order, user)


def catch_up(days=HISTORY_DAYS) -> list:
    """Авто-прийом увімкнули вже після доставки (або посилку доставлено, поки він був вимкнений): замовлення JLC,
    доставлені за останні days днів, з позиціями, яких ще немає на складі, — на склад. Давніші — ні: їх могли
    оприбуткувати повз Minerva. Повертає [(замовлення, [(артикул, к-сть)])]; cron sync_jlc_orders — щопроходу."""
    from datetime import timedelta
    from jlcpcb.models import JLCConfig, JLCOrder
    if not JLCConfig.get().auto_receive_on_delivered:
        return []
    since = timezone.localdate() - timedelta(days=days)
    out = []
    for order in (JLCOrder.objects.filter(local_status=JLCOrder.LocalStatus.DELIVERED, delivered_date__gte=since,
                                          purchase_order__isnull=False)
                  .exclude(mapping_status__in=[JLCOrder.MappingStatus.TRACK_ONLY, JLCOrder.MappingStatus.IGNORED])
                  .select_related("purchase_order").order_by("pk")):
        if not has_open_stock_lines(order):
            continue
        try:
            got = receive(order)
        except Exception:  # noqa: BLE001 — одне замовлення не зупиняє решту
            logger.warning("JLC %s: catch-up receive failed", order.jlc_order_id, exc_info=True)
            continue
        if got:
            out.append((order, got))
    return out


def sync_all() -> dict:
    """Закупівлі для замовлень без неї (і доставлених раніше — історією) та оновлення відкритих."""
    from jlcpcb.models import JLCOrder
    stats = {"created": 0, "updated": 0, "errors": 0}
    qs = JLCOrder.objects.exclude(mapping_status__in=[JLCOrder.MappingStatus.TRACK_ONLY, JLCOrder.MappingStatus.IGNORED])
    todo = qs.filter(Q(purchase_order__isnull=True) | ~Q(local_status__in=[
        JLCOrder.LocalStatus.DELIVERED, JLCOrder.LocalStatus.CANCELLED]))
    for order in todo.order_by("pk"):
        had = order.purchase_order_id
        try:
            po = sync(order)
        except Exception:  # noqa: BLE001 — одне замовлення не зупиняє решту
            logger.warning("JLC %s: purchase sync failed", order.jlc_order_id, exc_info=True)
            stats["errors"] += 1
            continue
        if po is not None:
            stats["updated" if had else "created"] += 1
    return stats
