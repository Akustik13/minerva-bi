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
# коробку запаковано й наклеєно етикетку — товару вже немає на полиці (склад списує з цього моменту;
# «Відправлено» для статусу замовлення — лише SENT)
OUT = ("label_ready", "in_transit", "delivered")
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
    out: Decimal = ZERO                            # уже не на полиці (етикетка готова, у дорозі, доставлено)
    to_send: Decimal = ZERO
    to_pack: Decimal = ZERO
    all_delivered: bool = False

    @property
    def partial(self) -> bool:
        """Частину відправлено, частина ще ні."""
        return self.active and self.sent > 0 and self.to_send > 0

    @property
    def started(self) -> bool:
        """Частина вже поїхала або запакована з етикеткою, а замовлення відправлене не все."""
        return self.active and self.out > 0 and self.to_send > 0

    def remainder(self) -> list:
        """[(артикул, скільки ще відправити)] — лише позиції з залишком."""
        return [(r["sku"], r["to_send"]) for r in self.lines if r["to_send"] > 0]

    def remainder_text(self) -> str:
        return ", ".join(f"{sku} × {qty_text(q)}" for sku, q in self.remainder())

    def left(self) -> list:
        """[(артикул, скільки ще не поїхало)] — ще не в посилці з етикеткою / у дорозі / доставленій (OUT): те, що
        людина вважає «ще відправити». Для статусу замовлення — remainder (лише SENT)."""
        out = []
        for r in self.lines:
            q = r["ordered"] - min(r["out"], r["ordered"])
            if q > 0:
                out.append((r["sku"], q))
        return out

    def left_text(self) -> str:
        return ", ".join(f"{sku} × {qty_text(q)}" for sku, q in self.left())

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
                   "packed": ZERO, "sent": ZERO, "out": ZERO, "delivered": ZERO} for l in lines}
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
            if s.status in OUT:
                r["out"] += q
            if s.status == "delivered":
                r["delivered"] += q
    all_delivered = True
    for r in rows.values():
        sent = min(r["sent"], r["ordered"])
        r["to_send"] = r["ordered"] - sent
        r["to_pack"] = max(r["ordered"] - r["packed"], ZERO)
        p.ordered += r["ordered"]
        p.sent += sent
        p.out += min(r["out"], r["ordered"])
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
    """{pk позиції: уже не на полиці} (не більше замовленого) — для складу (stock.sync_order): посилки з готовою
    етикеткою, у дорозі й доставлені (OUT). Без посилок у замовленні — {} (склад як завжди, за статусом
    замовлення). delivered_only — режим складу «списувати при Доставлено»: лише доставлені посилки."""
    if not has_parcels(order):
        return {}
    p = progress(order)
    key = "delivered" if delivered_only else "out"
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


# ── Список замовлень, магазин (DigiKey), лист клієнту ────────────────────────

def annotate_orders(orders) -> None:
    """Сторінка списку замовлень: order.parcel_info = {sent, ordered, remainder, to_pack, started} для замовлень
    із посилками (один запит, щоб знайти їх), інакше None."""
    from shipping.models import ShipmentLine
    orders = list(orders)
    ids = set(ShipmentLine.objects.filter(shipment__order_id__in=[o.pk for o in orders])
              .values_list("shipment__order_id", flat=True))
    for o in orders:
        o.parcel_info = None
        if o.pk in ids:
            p = progress(o)
            o.parcel_info = {"sent": qty_text(p.out), "ordered": qty_text(p.ordered),
                             "remainder": p.left_text(), "unpacked": p.unpacked_text(),
                             "to_pack": p.to_pack > 0, "started": p.started}


def live_parcels(order) -> list:
    """Оформлені посилки замовлення (не чернетки, не скасовані й не з помилкою, «від нас»), за порядком."""
    return list(order.shipments.exclude(status__in=DEAD + ("draft",)).exclude(direction="in")
                .prefetch_related("lines").order_by("pk"))


def mark_shop_confirmed(order) -> int:
    """Магазин уже показує замовлення відправленим (підтвердили на сайті DigiKey вручну чи раніше) — посилки з
    трек-номером позначаються підтвердженими: банер «Підтвердити відправлення» їх більше не пропонує."""
    from shipping.models import Shipment
    ids = [s.pk for s in live_parcels(order) if s.tracking_number and not s.shop_confirmed_at]
    if not ids:
        return 0
    return Shipment.objects.filter(pk__in=ids).update(shop_confirmed_at=timezone.now())


def shop_plan(order) -> dict:
    """Чи можна підтвердити відправлення в магазині, який приймає лише все замовлення разом (DigiKey: сума
    кількостей усіх посилок = замовлене): {ready, parcels: [{shipment, n, tracking, parts}], waiting, reason}.
    ready — усе замовлення розкладено по оформлених посилках і в кожної є трек-номер."""
    p = progress(order)
    parcels = []
    for x in p.parcels:
        s = x["shipment"]
        if s.status == "draft":
            continue
        parts = {}
        for line in (r["line"] for r in p.lines):
            q = x["contents"].get(line.pk, ZERO)
            if q > 0:
                sku = line_sku(line)
                parts[sku] = parts.get(sku, ZERO) + q
        parcels.append({"shipment": s, "n": x["n"], "tracking": (s.tracking_number or "").strip(),
                        "parts": parts, "parts_text": ", ".join(f"{k} × {qty_text(v)}" for k, v in parts.items())})
    out = {"ready": False, "parcels": parcels, "of": len(p.parcels), "unpacked": p.unpacked_text(), "reason": ""}
    if p.to_pack > 0:
        out["reason"] = f"ще не в жодній посилці: {p.unpacked_text()}"
    elif any(x["shipment"].status == "draft" for x in p.parcels):
        out["reason"] = "є посилка-чернетка — оформіть її в перевізника"
    elif not parcels or any(not x["tracking"] for x in parcels):
        out["reason"] = "не в усіх посилок є трек-номер"
    else:
        out["ready"] = True
    return out


def current_parcel(order, shipment=None):
    """Посилка, про яку лист клієнту: вибрана (лише цього замовлення), інакше остання оформлена з трек-номером,
    про яку ще не писали, інакше остання оформлена. None — посилок немає (лист — як завжди, про все замовлення)."""
    if not has_parcels(order):
        return None
    parcels = live_parcels(order)
    if shipment is not None:
        return next((s for s in parcels if s.pk == getattr(shipment, "pk", shipment)), None)
    fresh = [s for s in parcels if s.tracking_number and not s.customer_notified_at]
    return (fresh or parcels or [None])[-1]


def mail_parts(order, shipment) -> dict | None:
    """Лист клієнту про посилку частини замовлення (англійською, як шаблони листів): items — лише її вміст і
    примітка «partial shipment n of m / still to follow …» або «final parcel»; tracking / carrier — посилки;
    subject_suffix — до теми. None — посилок немає."""
    if shipment is None:
        return None
    p = progress(order)
    n = p.number(shipment)
    here = contents(shipment)
    items = "\n".join(f"• {r['sku']} — {qty_text(here[r['pk']])} pcs."
                      for r in p.lines if here.get(r["pk"], ZERO) > 0) or "—"
    total = len(p.parcels) + (1 if p.to_pack > 0 else 0)
    later = [(r["sku"], r["ordered"] - min(r["out"], r["ordered"])) for r in p.lines]
    later = [(sku, q) for sku, q in later if q > 0]
    if later:
        note = (f"This is a partial shipment (parcel {n} of {total}). Still to follow:\n"
                + "\n".join(f"• {sku} — {qty_text(q)} pcs." for sku, q in later)
                + "\nWe will send you the tracking number of the next parcel as soon as it ships.")
        suffix = f" — partial shipment {n}/{total}"
    elif total > 1:
        note = f"This is the final parcel of your order (parcel {n} of {total}) — your order is now complete."
        suffix = f" — parcel {n}/{total}"
    else:
        return None
    return {"items": f"{items}\n\n{note}", "tracking": (shipment.tracking_number or "").strip(),
            "carrier": shipment.carrier.get_carrier_type_display() if shipment.carrier_id else "",
            "subject_suffix": suffix, "n": n, "of": total}
