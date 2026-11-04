"""📦 Відправлення частинами (посилки, Teillieferung) — уся логіка тут.

Замовлення можна відправити кількома посилками. У відправленні (Shipment) — рядки ShipmentLine «що з
замовлення лежить у цій посилці». Відправлення без рядків — уся кількість замовлення (так було завжди), тож
старі замовлення працюють як раніше. «Посилки» в замовленні (has_parcels) — коли хоч одне його відправлення
має рядки.

  жива посилка        — не скасована й не з помилкою (DEAD)
  відправлена посилка — «В дорозі» / «Доставлено» (SENT)
  залишок             — замовлено − відправлено, по кожній позиції

Поки є залишок, замовлення не стає «Відправлено» / «Доставлено» (hold_status — сигнал pre_save замовлення,
тож однаково для трекінгу, DigiKey, кнопок підтвердження, ручного статусу); «Доставлено» — коли доставлено все.
settle — статус замовлення за посилками (зміна посилки, її статусу, кількості позиції). Склад —
inventory.services.stock.sync_order: відправлене посилками списується (sent_by_line), залишок тримається
резервом.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

DEAD = ("cancelled", "error")
SENT = ("in_transit", "delivered")
OPEN_ORDER = ("received", "processing")
CLOSED_ORDER = ("shipped", "delivered")
ZERO = Decimal("0")


class ParcelError(Exception):
    """Вміст посилки не прийнято (повертається людині як повідомлення)."""


def _dec(value) -> Decimal:
    try:
        return Decimal(str(value).replace(",", ".").strip() or "0")
    except (InvalidOperation, ValueError, TypeError):
        raise ParcelError(f"Кількість «{value}» — не число.")


def qty_text(q) -> str:
    """250 / 2.5 — без зайвих нулів."""
    q = Decimal(str(q or 0))
    return str(int(q)) if q == q.to_integral() else f"{q.normalize():f}"


def order_lines(order) -> list:
    return list(order.lines.select_related("product").order_by("pk")) if order and order.pk else []


def line_sku(line) -> str:
    return (line.sku_raw or (line.product.sku if line.product_id else "") or "?").strip()


def _ordered(line) -> Decimal:
    return max(line.qty or ZERO, ZERO)


def has_parcels(order) -> bool:
    """Чи є в замовленні посилки (хоч одне відправлення з рядками, будь-якого стану)."""
    from shipping.models import ShipmentLine
    return bool(order and order.pk) and ShipmentLine.objects.filter(shipment__order_id=order.pk).exists()


def contents(shipment, lines=None) -> dict:
    """{pk позиції: кількість} у посилці. Відправлення без рядків — уся кількість замовлення."""
    rows = list(shipment.lines.all())
    if rows:
        return {r.order_line_id: r.qty for r in rows}
    if lines is None:
        lines = order_lines(shipment.order)
    return {l.pk: _ordered(l) for l in lines}


def is_whole(shipment) -> bool:
    return not shipment.lines.exists()


@dataclass
class Progress:
    lines: list = field(default_factory=list)      # [{line, pk, sku, ordered, packed, sent, delivered, to_pack, to_send}]
    parcels: list = field(default_factory=list)    # [{shipment, n, whole, contents, qty}]
    active: bool = False                           # у замовленні є посилки (has_parcels)
    ordered: Decimal = ZERO
    sent: Decimal = ZERO
    to_send: Decimal = ZERO
    to_pack: Decimal = ZERO
    all_delivered: bool = False

    @property
    def partial(self) -> bool:
        """Частину відправлено, частина ще ні."""
        return self.active and self.sent > 0 and self.to_send > 0

    def remainder(self) -> list:
        """[(артикул, скільки ще відправити)] — лише позиції з залишком."""
        return [(r["sku"], r["to_send"]) for r in self.lines if r["to_send"] > 0]

    def remainder_text(self) -> str:
        return ", ".join(f"{sku} × {qty_text(q)}" for sku, q in self.remainder())

    def unpacked_text(self) -> str:
        """Чого ще немає в жодній живій посилці (для нової посилки)."""
        return ", ".join(f"{r['sku']} × {qty_text(r['to_pack'])}" for r in self.lines if r["to_pack"] > 0)

    def number(self, shipment) -> int | None:
        return next((p["n"] for p in self.parcels if p["shipment"].pk == shipment.pk), None)


def progress(order, *, exclude=None) -> Progress:
    """Що відправлено й що лишилось. exclude — pk відправлення, яке не рахувати в «уже в посилках» (його ж
    редагують)."""
    p = Progress()
    if not order or not order.pk:
        return p
    lines = order_lines(order)
    p.active = has_parcels(order)
    ships = list(order.shipments.exclude(status__in=DEAD).exclude(direction="in")
                 .prefetch_related("lines").order_by("pk"))
    rows = {l.pk: {"line": l, "pk": l.pk, "sku": line_sku(l), "ordered": _ordered(l),
                   "packed": ZERO, "sent": ZERO, "delivered": ZERO} for l in lines}
    for n, s in enumerate(ships, 1):
        c = contents(s, lines)
        p.parcels.append({"shipment": s, "n": n, "whole": not s.lines.all(), "contents": c,
                          "qty": sum(c.values(), ZERO)})
        for pk, q in c.items():
            r = rows.get(pk)
            if r is None:
                continue
            if s.pk != exclude:
                r["packed"] += q
            if s.status in SENT:
                r["sent"] += q
            if s.status == "delivered":
                r["delivered"] += q
    all_delivered = True
    for r in rows.values():
        sent = min(r["sent"], r["ordered"])
        r["to_send"] = r["ordered"] - sent
        r["to_pack"] = max(r["ordered"] - r["packed"], ZERO)
        p.ordered += r["ordered"]
        p.sent += sent
        p.to_send += r["to_send"]
        p.to_pack += r["to_pack"]
        if r["ordered"] > 0 and r["delivered"] < r["ordered"]:
            all_delivered = False
        p.lines.append(r)
    p.all_delivered = all_delivered and p.ordered > 0
    return p


def default_contents(order, shipment=None) -> dict:
    """Вміст посилки для форми: наявне відправлення — що в ньому зараз (без рядків — усе замовлення); нове —
    усе, чого ще немає в інших живих посилках (перша посилка — уся кількість)."""
    if shipment is not None and shipment.pk:
        return contents(shipment)
    return {r["pk"]: r["to_pack"] for r in progress(order).lines}


def whole_parcels(order, exclude=None) -> list:
    """Живі відправлення замовлення, записані як «усе замовлення» (без рядків) — підказка, коли для нової
    посилки нічого не лишилось: можливо, в одному з них була лише частина."""
    if not order or not order.pk:
        return []
    qs = order.shipments.exclude(status__in=DEAD).exclude(direction="in").filter(lines__isnull=True).order_by("pk")
    if exclude:
        qs = qs.exclude(pk=exclude)
    return list(qs)


def sent_by_line(order, *, delivered_only=False) -> dict:
    """{pk позиції: відправлено посилками} (не більше замовленого) — для складу (stock.sync_order). Без посилок
    у замовленні — {} (склад як завжди, за статусом замовлення). delivered_only — режим складу «списувати при
    Доставлено»: рахуються лише доставлені посилки."""
    if not has_parcels(order):
        return {}
    p = progress(order)
    key = "delivered" if delivered_only else "sent"
    return {r["pk"]: min(r[key], r["ordered"]) for r in p.lines if r[key] > 0}


def hold_status(order, wanted, current=None) -> str:
    """Статус, який замовлення справді отримає, коли щось хоче поставити `wanted`: поки є залишок —
    не «Відправлено» / «Доставлено» (лишається «В обробці»; уже закрите раніше не відкривається тут — лише
    settle); «Доставлено» — коли доставлено всі посилки, інакше «Відправлено». Без посилок — як просили."""
    if wanted not in CLOSED_ORDER or not order or not order.pk or not has_parcels(order):
        return wanted
    cur = current if current is not None else order.status
    p = progress(order)
    if p.to_send > 0:
        return cur if cur in ("processing",) + CLOSED_ORDER else "processing"
    if wanted == "delivered" and not p.all_delivered:
        return cur if cur == "delivered" else "shipped"
    return wanted


def settle(order, *, force=False):
    """Статус замовлення за посилками (зміна вмісту посилки, її статусу, кількості позиції):
      є залишок, а замовлення «Відправлено» / «Доставлено» → «В обробці» (відкривається знову);
      «Отримано», а частина вже в дорозі → «В обробці»;
      усе відправлено → «Відправлено», усе доставлено → «Доставлено».
    force — і коли посилок уже немає (людина зробила посилку знову «усім замовленням»). Скасоване не чіпає.
    Повертає новий статус або None (не змінився)."""
    from sales.models import SalesOrder
    if not order or not order.pk:
        return None
    if not force and not has_parcels(order):
        return None
    row = SalesOrder.objects.filter(pk=order.pk).values("status", "shipped_at", "delivered_at").first()
    if not row or row["status"] == "cancelled":
        return None
    st = row["status"]
    p = progress(order)
    if not p.parcels:
        return None
    new = st
    if p.to_send > 0:
        if st in CLOSED_ORDER or (st == "received" and p.sent > 0):
            new = "processing"
    elif p.sent > 0:
        if st in OPEN_ORDER:
            new = "delivered" if p.all_delivered else "shipped"
        elif st == "shipped" and p.all_delivered:
            new = "delivered"
    if new == st:
        return None
    fresh = SalesOrder.objects.get(pk=order.pk)
    fresh.status = new
    fields = ["status", "status_source"]
    fresh.status_source = "Посилки"
    if new in CLOSED_ORDER and not row["shipped_at"]:
        fresh.shipped_at = timezone.localdate()
        fields.append("shipped_at")
    if new == "delivered" and not row["delivered_at"]:
        fresh.delivered_at = timezone.now()
        fields.append("delivered_at")
    fresh.save(update_fields=fields)
    order.status = new
    logger.info("Parcels: order %s:%s %s → %s", fresh.source, fresh.order_number, st, new)
    return new


def sync(order, *, force=False):
    """Після зміни посилок: статус (settle), а якщо він не змінився — склад (stock.sync_order); зміна статусу
    сама запускає склад через сигнал замовлення."""
    if settle(order, force=force) is None:
        from inventory.services import stock
        try:
            stock.sync_order(order)
        except Exception:
            logger.exception("Parcels: stock sync failed for order %s", order.pk)


def set_contents(shipment, quantities: dict) -> dict:
    """Записати, що лежить у посилці: {pk позиції: кількість} (рядок, якого немає, — 0). Уся кількість
    замовлення — рядки не зберігаються (звичайне відправлення). Кількість — від 0 до замовленої; хоч одна
    позиція має бути. Потім — статус замовлення й склад (sync). Повертає {whole, contents, progress}."""
    from shipping.models import ShipmentLine
    order = shipment.order
    if order is None:
        raise ParcelError("Відправлення не пов'язане із замовленням.")
    if shipment.status == "cancelled":
        raise ParcelError("Відправлення скасовано — його вміст не змінюється.")
    lines = order_lines(order)
    if not lines:
        raise ParcelError("У замовленні немає позицій.")
    by_pk = {str(l.pk): l for l in lines}
    clean = {}
    for key, raw in (quantities or {}).items():
        line = by_pk.get(str(key))
        if line is None:
            continue
        q = _dec(raw)
        if q < 0:
            raise ParcelError(f"{line_sku(line)}: кількість не може бути від'ємною.")
        if q > _ordered(line):
            raise ParcelError(f"{line_sku(line)}: у посилці {qty_text(q)}, а в замовленні лише "
                              f"{qty_text(_ordered(line))}.")
        clean[line.pk] = q
    if not any(q > 0 for q in clean.values()):
        raise ParcelError("У посилці немає жодної позиції — вкажіть, скільки чого в ній лежить.")
    whole = all(clean.get(l.pk, ZERO) == _ordered(l) for l in lines)
    with transaction.atomic():
        ShipmentLine.objects.filter(shipment=shipment).delete()
        if not whole:
            ShipmentLine.objects.bulk_create([
                ShipmentLine(shipment=shipment, order_line_id=pk, qty=q) for pk, q in clean.items() if q > 0])
    sync(order, force=True)
    return {"whole": whole, "contents": clean, "progress": progress(order)}


def parse_post(post, prefix="parcel_qty_") -> dict | None:
    """{pk позиції: кількість} з форми (поля parcel_qty_<pk>); None — у формі блоку посилки не було."""
    out = {}
    for key, value in post.items():
        if key.startswith(prefix):
            pk = key[len(prefix):]
            if pk.isdigit():
                out[pk] = value
    return out or None


def parcel_value(order, quantities: dict) -> Decimal:
    """Вартість вмісту посилки за цінами позицій (для «Оголошеної вартості» й митниці)."""
    total = ZERO
    for line in order_lines(order):
        q = Decimal(str(quantities.get(line.pk, quantities.get(str(line.pk), 0)) or 0))
        if q <= 0:
            continue
        unit = line.unit_price
        if unit is None and line.total_price is not None and line.qty:
            unit = Decimal(str(line.total_price)) / Decimal(str(line.qty))
        total += Decimal(str(unit or 0)) * q
    return total.quantize(Decimal("0.01"))


def parts_by_sku(shipment) -> dict | None:
    """{артикул: кількість} посилки — для підтвердження в магазині (DigiKey: shippedParts). None — у посилці все
    замовлення (магазину йде звичайне «відправлено все»)."""
    rows = list(shipment.lines.select_related("order_line__product"))
    if not rows:
        return None
    out = {}
    for r in rows:
        if r.qty > 0:
            sku = line_sku(r.order_line)
            out[sku] = out.get(sku, ZERO) + r.qty
    return out


def view_rows(order, shipment=None) -> list:
    """Рядки таблиці «📦 Що в цій посилці»: позиція, замовлено, уже в інших посилках, у цій, залишиться."""
    p = progress(order, exclude=shipment.pk if shipment is not None and shipment.pk else None)
    current = default_contents(order, shipment)
    out = []
    for r in p.lines:
        here = current.get(r["pk"], ZERO)
        line = r["line"]
        out.append({
            "pk": r["pk"], "sku": r["sku"],
            "name": ((line.product.name_export or line.product.name) if line.product_id else "") or "",
            "ordered": r["ordered"], "other": r["packed"], "here": here,
            "left": max(r["ordered"] - r["packed"] - here, ZERO),
            "unit_price": line.unit_price,
        })
    return out
