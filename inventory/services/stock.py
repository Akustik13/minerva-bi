"""
Сервіс залишків складу — єдина точка для API, адмінки та інтеграцій.

Семантика (так рахують API, огляд складу, аналіз запасів, Angebote, сигнали):
  on_hand   = Σ qty усіх транзакцій, крім RESERVED (фізично на складі)
  reserved  = заброньовано під замовлення й ще НЕ списано зі складу:
              −Σ qty RESERVED-транзакцій
              + позиції відкритих замовлень (affects_stock, статус не «Скасовано» / «Доставлено»),
                під які рух складу ще не записано (списання при «Відправлено» / «Доставлено»)
              Замовлення, вже списані (Outgoing), сюди не входять — їх уже немає в on_hand.
  available = on_hand − reserved (можна продати)
  incoming  = Σ (qty_ordered − qty_received) у відкритих закупівлях

Замовлення продажу ↔ склад (sync_order, release_order_stock, restore_order_stock):
  кожна позиція замовлення тримає не більше одного руху з ключем
      so:<джерело>:<номер>:line:<pk позиції>:<uuid>
  — Outgoing (списано) або Резерв, залежно від налаштувань складу і статусу (order_target).
  Скасування повертає товар: резерв знімається, списання компенсується коригуванням
  so-cancel:<pk руху>; зняття скасування прибирає ці компенсації — товар знову за замовленням.
"""
from __future__ import annotations

import math
import uuid
from decimal import Decimal

from django.db import transaction
from django.db.models import (
    CharField, DecimalField, Exists, F, Func, Max, OuterRef, Q, Subquery, Sum, Value,
)
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone

from inventory.models import (
    InventorySettings, InventoryTransaction, Location, Product, ProductAlias,
    PurchaseOrderLine,
)

TX = InventoryTransaction.TxType
_DEC = DecimalField(max_digits=18, decimal_places=3)
_ZERO = Value(Decimal("0"), output_field=_DEC)
OPEN_PO_STATUSES = ("draft", "ordered", "partial")
CONFIRMED_PO_STATUSES = ("ordered", "partial")      # замовлено в постачальника (без чернеток-кошика)

CANCELLED = "cancelled"
SHIPPED_STATUSES = ("shipped", "delivered")
# Позиції замовлень у цих статусах не бронюють товар, навіть якщо рух складу не записано
NOT_PENDING_STATUSES = (CANCELLED, "delivered")


class StockError(Exception):
    """Помилка бізнес-логіки складу (повертається клієнту як 400/409)."""

    def __init__(self, message, code="invalid", details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


# ── Позиції замовлень, які ще бронюють товар ──────────────────────────────────

class _FlatConcat(Func):
    """'a' || 'b' || … без COALESCE-обгорток Concat (частини не NULL): глибоко вкладений SQL переповнює
    стек парсера SQLite. «||» однаково працює в SQLite і PostgreSQL."""
    template = "(%(expressions)s)"
    arg_joiner = " || "
    output_field = CharField()


def _line_held_txs():
    """Рухи складу позиції замовлення (OuterRef — SalesOrderLine): списання або резерв під неї."""
    key = _FlatConcat(Value("so:"), OuterRef("order__source"), Value(":"), OuterRef("order__order_number"),
                      Value(":line:"), Cast(OuterRef("pk"), output_field=CharField()), Value(":"))
    return InventoryTransaction.objects.filter(
        product_id=OuterRef("product_id"),
        tx_type__in=(TX.OUTGOING, TX.RESERVED),
        external_key__startswith=key,
    )


def pending_order_lines():
    """Позиції відкритих замовлень (affects_stock, не скасовані й не доставлені), під які ще не записано
    жодного руху складу — товар обіцяно, але він ще в on_hand (режими «списувати при Відправлено /
    Доставлено» або замовлення, внесене до зміни налаштувань)."""
    from sales.models import SalesOrderLine
    return (SalesOrderLine.objects
            .filter(order__affects_stock=True, qty__gt=0, product__isnull=False)
            .exclude(order__status__in=NOT_PENDING_STATUSES)
            .filter(~Exists(_line_held_txs())))


def _default_location_code():
    return (InventorySettings.get().default_location or "MAIN").strip()


# ── Читання залишків ──────────────────────────────────────────────────────────

def annotate_stock(qs, location=None):
    """Додає до queryset Product: _on_hand, _reserved, _available, _incoming, _last_movement."""
    tx = InventoryTransaction.objects.filter(product=OuterRef("pk"))
    if location is not None:
        tx = tx.filter(location=location)

    on_hand = (tx.exclude(tx_type=TX.RESERVED)
               .values("product").annotate(t=Sum("qty")).values("t"))
    reserved = (tx.filter(tx_type=TX.RESERVED)
                .values("product").annotate(t=Sum("qty")).values("t"))
    last_move = tx.values("product").annotate(t=Max("created_at")).values("t")
    incoming = (PurchaseOrderLine.objects
                .filter(product=OuterRef("pk"), purchase_order__status__in=OPEN_PO_STATUSES)
                .values("product")
                .annotate(t=Sum(F("qty_ordered") - F("qty_received")))
                .values("t"))
    reserved_expr = -Coalesce(Subquery(reserved, output_field=_DEC), _ZERO)
    # Незаписані замовлення спишуться з локації за замовчуванням — там вони й бронюють товар
    if location is None or location.code == _default_location_code():
        pending = (pending_order_lines().filter(product=OuterRef("pk"))
                   .values("product").annotate(t=Sum("qty")).values("t"))
        reserved_expr = reserved_expr + Coalesce(Subquery(pending, output_field=_DEC), _ZERO)

    return qs.annotate(
        _on_hand=Coalesce(Subquery(on_hand, output_field=_DEC), _ZERO),
        _reserved=reserved_expr,
        _incoming=Coalesce(Subquery(incoming, output_field=_DEC), _ZERO),
        _last_movement=Subquery(last_move),
    ).annotate(_available=F("_on_hand") - F("_reserved"))


def stock_map(product_ids, location=None):
    """{pk товару: товар з _on_hand, _reserved, _available, _incoming} — для сторінок зі списками."""
    return {p.pk: p for p in annotate_stock(Product.objects.filter(pk__in=list(product_ids)), location=location)}


def location_breakdown(product):
    """Залишки товару по кожній локації: [{location, on_hand, reserved, available}]."""
    rows = (InventoryTransaction.objects.filter(product=product)
            .values("location__code", "location__name")
            .annotate(
                on_hand=Coalesce(Sum("qty", filter=~Q(tx_type=TX.RESERVED)), _ZERO),
                reserved_neg=Coalesce(Sum("qty", filter=Q(tx_type=TX.RESERVED)), _ZERO),
            )
            .order_by("location__code"))
    pending = (pending_order_lines().filter(product=product)
               .aggregate(t=Sum("qty"))["t"]) or Decimal("0")
    default = _default_location_code()
    out = []
    for r in rows:
        reserved = -r["reserved_neg"] + (pending if r["location__code"] == default else Decimal("0"))
        out.append({
            "location": r["location__code"],
            "location_name": r["location__name"],
            "on_hand": r["on_hand"],
            "reserved": reserved,
            "available": r["on_hand"] - reserved,
        })
    return out


def get_available(product):
    p = annotate_stock(Product.objects.filter(pk=product.pk)).get()
    return p._available


def resolve_sku(sku):
    """SKU або аліас → Product (None якщо не знайдено)."""
    sku = (sku or "").strip()
    if not sku:
        return None
    p = Product.objects.filter(sku=sku).first()
    if p:
        return p
    alias = ProductAlias.objects.filter(alias=sku).select_related("product").first()
    if alias:
        return alias.product
    return Product.objects.filter(sku__iexact=sku).first()


def check_availability(items):
    """
    items: [{"sku": str, "qty": Decimal}] → [{"sku", "requested", "available", "ok", "found"}].
    Однакові SKU сумуються.
    """
    wanted = {}
    for it in items:
        sku = str(it["sku"]).strip()
        wanted[sku] = wanted.get(sku, Decimal("0")) + Decimal(str(it["qty"]))

    products = {sku: resolve_sku(sku) for sku in wanted}
    ids = [p.pk for p in products.values() if p]
    stock = {p.pk: p for p in annotate_stock(Product.objects.filter(pk__in=ids))}

    result = []
    for sku, qty in wanted.items():
        p = products[sku]
        if not p:
            result.append({"sku": sku, "found": False, "requested": qty,
                           "available": Decimal("0"), "ok": False})
            continue
        avail = stock[p.pk]._available
        result.append({"sku": sku, "product_sku": p.sku, "found": True,
                       "requested": qty, "available": avail,
                       "ok": p.is_active and avail >= qty})
    return result


# ── Що вже тримає замовлення (для «вистачає / бракує» у картці й списку замовлень) ──

def annotate_line_held(lines_qs):
    """Позиції замовлень + _held_tx: скільки під позицію вже списано / заброньовано рухом складу."""
    held = (_line_held_txs().values("product").annotate(t=Sum("qty")).values("t"))
    return lines_qs.annotate(_held_tx=-Coalesce(Subquery(held, output_field=_DEC), _ZERO))


def line_held(line):
    """Скільки товару вже рахується за цією позицією (списано, резерв або бронь незаписаного замовлення).
    available + line_held(line) — скільки було б вільно без цього замовлення. Потрібен annotate_line_held."""
    held = getattr(line, "_held_tx", None) or Decimal("0")
    if held > 0:
        return held
    o = line.order
    if o.affects_stock and o.status not in NOT_PENDING_STATUSES and line.product_id and (line.qty or 0) > 0:
        return line.qty
    return Decimal("0")


def order_lines_cover(order):
    """{pk позиції: {available, held, free}} для картки замовлення: free = доступно + те, що вже взяло це
    замовлення (його списання / резерв / бронь не рахуються як нестача). Відправлене — free = available."""
    lines = list(annotate_line_held(order.lines.filter(product__isnull=False)).select_related("order"))
    avail = {pid: p._available for pid, p in stock_map({l.product_id for l in lines}).items()}
    out = {}
    for line in lines:
        a = avail.get(line.product_id, Decimal("0"))
        h = Decimal("0") if line.order.status in SHIPPED_STATUSES else line_held(line)
        out[line.pk] = {"available": a, "held": h, "free": a + h}
    return out


def open_order_lines(product_ids):
    """Позиції незакритих замовлень товарів зі станом для огляду складу:
    reserved — 🔒 резерв, pending — ще не списано (рахується в «Резерв»), deducted — уже списано зі складу.
    Відправлені й уже списані замовлення не показуються."""
    from sales.models import SalesOrderLine
    qs = (SalesOrderLine.objects
          .filter(product_id__in=list(product_ids), order__affects_stock=True, qty__gt=0)
          .exclude(order__status__in=NOT_PENDING_STATUSES)
          .annotate(_held_type=Subquery(_line_held_txs().values("tx_type")[:1]))
          .select_related("order")
          .order_by("product_id", "order__order_date", "pk"))
    out = []
    for line in qs:
        state = {TX.RESERVED: "reserved", TX.OUTGOING: "deducted"}.get(line._held_type, "pending")
        if state == "deducted" and line.order.status in SHIPPED_STATUSES:
            continue
        out.append((line, state))
    return out


# ── Запис рухів ───────────────────────────────────────────────────────────────

def get_location(code=None):
    """Активна локація за кодом; без коду — default_location з налаштувань складу."""
    code = (code or InventorySettings.get().default_location or "MAIN").strip()
    loc = Location.objects.filter(code=code).first()
    if loc is None:
        raise StockError(f"Локацію «{code}» не знайдено.", code="location_not_found")
    if not loc.is_active:
        raise StockError(f"Локація «{code}» неактивна.", code="location_inactive")
    return loc


def _validate_qty(product, qty):
    if qty == 0:
        raise StockError("Кількість не може бути 0.", code="zero_qty")
    if not product.is_fractional_unit() and qty != int(qty):
        raise StockError(
            f"Товар «{product.sku}» вимірюється в штуках — кількість має бути цілою.",
            code="fractional_qty",
        )


def _check_location_kind(product, location):
    if product.kind == Product.Kind.COMPONENT and location.location_type == Location.LocationType.FINISHED:
        raise StockError(f"Компонент «{product.sku}» не можна розміщувати на складі готової продукції "
                         f"«{location.code}».", code="wrong_location_type")
    if product.kind == Product.Kind.FINISHED and location.location_type == Location.LocationType.COMPONENTS:
        raise StockError(f"Готовий товар «{product.sku}» не можна розміщувати на складі компонентів "
                         f"«{location.code}».", code="wrong_location_type")


def signed_qty(tx_type, qty):
    """Знак кількості за типом руху: прихід — плюс, витрата й резерв — мінус, коригування — як введено."""
    qty = Decimal(str(qty))
    if tx_type == TX.INCOMING:
        return abs(qty)
    if tx_type in (TX.OUTGOING, TX.RESERVED):
        return -abs(qty)
    return qty


def create_movement(*, product, tx_type, qty, location=None, ref_doc="",
                    external_key="", tx_date=None, performed_by=None):
    """
    Створює рух складу.
      Incoming   — qty > 0 (знак ставиться автоматично)
      Outgoing   — qty > 0 у запиті, зберігається як від'ємне
      Adjustment — qty зі знаком (+ додати / − списати)

    external_key — ключ ідемпотентності: повторний запит з тим самим ключем
    повертає існуючу транзакцію і created=False.
    Повертає (tx, created).
    """
    qty = Decimal(str(qty))
    if external_key:
        existing = InventoryTransaction.objects.filter(external_key=external_key).first()
        if existing:
            return existing, False

    if tx_type not in (TX.INCOMING, TX.OUTGOING, TX.ADJUSTMENT):
        raise StockError("tx_type має бути Incoming, Outgoing або Adjustment.", code="bad_tx_type")
    qty = signed_qty(tx_type, qty)

    _validate_qty(product, qty)
    location = location or get_location()
    _check_location_kind(product, location)

    if qty < 0 and not InventorySettings.get().allow_negative_stock:
        on_loc = annotate_stock(Product.objects.filter(pk=product.pk), location=location).get()._on_hand
        if on_loc + qty < 0:
            raise StockError(
                f"Недостатньо «{product.sku}» на «{location.code}»: є {on_loc}, списується {-qty}.",
                code="insufficient_stock",
                details={"sku": product.sku, "on_hand": str(on_loc), "requested": str(-qty)},
            )

    tx = InventoryTransaction.objects.create(
        tx_type=tx_type,
        qty=qty,
        product=product,
        location=location,
        ref_doc=ref_doc or "",
        external_key=external_key or f"api:{tx_type.lower()}:{uuid.uuid4()}",
        tx_date=tx_date or timezone.now(),
        performed_by=performed_by,
    )
    return tx, True


def set_stock_level(*, product, qty, location=None, ref_doc="", performed_by=None):
    """
    Інвентаризація: встановлює фактичний залишок на локації — створює Adjustment
    на різницю. Повертає (tx | None, previous_on_hand).
    """
    qty = Decimal(str(qty))
    if qty < 0:
        raise StockError("Фактичний залишок не може бути від'ємним.", code="negative_count")
    if not product.is_fractional_unit() and qty != int(qty):
        raise StockError(f"Товар «{product.sku}» вимірюється в штуках.", code="fractional_qty")
    location = location or get_location()
    _check_location_kind(product, location)

    with transaction.atomic():
        current = annotate_stock(Product.objects.filter(pk=product.pk), location=location).get()._on_hand
        delta = qty - current
        if delta == 0:
            return None, current
        tx = InventoryTransaction.objects.create(
            tx_type=TX.ADJUSTMENT,
            qty=delta,
            product=product,
            location=location,
            ref_doc=ref_doc or f"Інвентаризація: {current} → {qty}",
            external_key=f"api:count:{uuid.uuid4()}",
            tx_date=timezone.now(),
            performed_by=performed_by,
        )
    return tx, current


# ── Замовлення продажу ↔ склад ────────────────────────────────────────────────

def order_prefix(order, line_pk=None, *, source=None, number=None):
    base = f"so:{order.source if source is None else source}:" \
           f"{order.order_number if number is None else number}:line:"
    return base if line_pk is None else f"{base}{line_pk}:"


def _order_ref(order):
    return f"SO-{order.source}:{order.order_number}"


def _cancel_key(tx_pk):
    return f"so-cancel:{tx_pk}"


def order_target(order, settings=None):
    """Який рух має тримати позиція замовлення зараз: TX.OUTGOING (списано), TX.RESERVED або None (нічого).

      скасовано / не впливає на склад                → None
      «Бронювати замовлення»                         → Резерв, у «Відправлено» / «Доставлено» — Outgoing
      «Списувати при створенні» (за замовчуванням)   → Outgoing
      «… при «Відправлено»»                          → Outgoing у «Відправлено» / «Доставлено», раніше — None
      «… при «Доставлено»»                           → Outgoing у «Доставлено», раніше — None
    """
    if not order.affects_stock or order.status == CANCELLED:
        return None
    s = settings or InventorySettings.get()
    shipped = order.status in SHIPPED_STATUSES
    if s.use_reservation:
        return TX.OUTGOING if shipped else TX.RESERVED
    if s.deduct_on == InventorySettings.DeductOn.SHIPPED:
        return TX.OUTGOING if shipped else None
    if s.deduct_on == InventorySettings.DeductOn.DELIVERED:
        return TX.OUTGOING if order.status == "delivered" else None
    return TX.OUTGOING


def _performer(performed_by=None):
    if performed_by is not None:
        return performed_by
    try:
        from core.utils import get_current_user
        user = get_current_user()
    except Exception:
        return None
    return user if getattr(user, "pk", None) and getattr(user, "is_authenticated", False) else None


def order_location(settings=None):
    """Локація для рухів замовлень продажу (готова продукція): «Локація за замовчуванням», створюється сама."""
    s = settings or InventorySettings.get()
    loc_code = s.default_location or "MAIN"
    location, created = Location.objects.get_or_create(
        code=loc_code,
        defaults={"name": "Основний склад", "location_type": Location.LocationType.FINISHED, "is_active": True},
    )
    if not created and location.location_type != Location.LocationType.FINISHED:
        location.location_type = Location.LocationType.FINISHED
        location.save(update_fields=["location_type"])
    return location


def _order_txs(order):
    """{pk позиції: [рухи]} — усі рухи з ключем позицій цього замовлення (і позицій, яких уже немає)."""
    prefix = order_prefix(order)
    out = {}
    for tx in InventoryTransaction.objects.filter(external_key__startswith=prefix).order_by("pk"):
        head = tx.external_key[len(prefix):].split(":", 1)[0]
        if head.isdigit():
            out.setdefault(int(head), []).append(tx)
    return out


def _cancel_comps(tx_pks):
    """{pk руху: компенсація скасування} для цих рухів."""
    keys = {_cancel_key(pk): pk for pk in tx_pks}
    if not keys:
        return {}
    return {keys[c.external_key]: c for c in InventoryTransaction.objects.filter(external_key__in=list(keys))}


def _bom_txs(order):
    """Списання компонентів BOM за замовленням (inventory/utils.deduct_components_for_assembly)."""
    if not order.pk:
        return []
    return list(InventoryTransaction.objects.filter(
        external_key__regex=rf"^bom_deduction_[0-9]+_{order.pk}_[0-9]+$", tx_type=TX.OUTGOING))


def _num(q):
    q = Decimal(str(q))
    return f"{q.normalize():f}" if q != q.to_integral() else str(int(q))


def _label(tx_type):
    return "резерв" if tx_type == TX.RESERVED else "списано"


def _drop(tx, comps):
    """Прибрати рух позиції (і його компенсацію скасування, якщо була) — разом вони нічого не змінюють."""
    comp = comps.get(tx.pk)
    if comp is not None:
        comp.delete()
    tx.delete()


def sync_order(order, *, settings=None, performed_by=None):
    """Позиції замовлення → рухи складу (подія: позицію додано / змінено, статус змінився, «Синхронізувати»).

    Позиція без руху отримує рух за order_target; рух із іншим товаром чи кількістю виправляється на місці
    (повернення старого й списання нового — один рух, без подвійного повернення); резерв стає списанням,
    коли замовлення відправлено. Уже списане назад не повертається (лише скасування, видалення позиції або
    кількість 0). Скасовані й «не впливає на склад» замовлення не змінюються. Повертає список змін."""
    if not order.pk or order.status == CANCELLED or not order.affects_stock:
        return []
    s = settings or InventorySettings.get()
    target = order_target(order, s)
    performer = _performer(performed_by)
    changes = []
    with transaction.atomic():
        by_line = _order_txs(order)
        comps = _cancel_comps([tx.pk for txs in by_line.values() for tx in txs])
        location = None
        for line in order.lines.select_related("product").order_by("pk"):
            txs = [tx for tx in by_line.get(line.pk, [])
                   if tx.tx_type in (TX.OUTGOING, TX.RESERVED) and tx.pk not in comps]
            want = max(line.qty or Decimal("0"), Decimal("0"))   # від'ємна кількість (повернення) нічого не тримає
            sku = line.product.sku if line.product_id else (line.sku_raw or "?")
            if not line.product_id or want <= 0:
                for tx in txs:
                    _drop(tx, comps)
                    changes.append(f"{sku}: рух прибрано (кількість 0)")
                continue
            if not txs:
                if target is None:
                    continue
                location = location or order_location(s)
                InventoryTransaction.objects.create(
                    external_key=f"{order_prefix(order, line.pk)}{uuid.uuid4()}",
                    tx_type=target, qty=-want, product_id=line.product_id, location=location,
                    ref_doc=_order_ref(order), tx_date=_order_tx_date(order), performed_by=performer)
                changes.append(f"{sku}: {_label(target)} {_num(want)}")
                continue
            tx = txs[0]
            for extra in txs[1:]:     # старі дублі — одна позиція тримає один рух
                _drop(extra, comps)
                changes.append(f"{sku}: зайвий рух прибрано")
            fields = []
            if tx.product_id != line.product_id:
                old_sku = tx.product.sku
                tx.product_id = line.product_id
                fields.append("product")
                changes.append(f"{old_sku} → {sku}")
            if tx.qty != -want:
                changes.append(f"{sku}: {_num(abs(tx.qty))} → {_num(want)}")
                tx.qty = -want
                fields.append("qty")
            if target == TX.OUTGOING and tx.tx_type == TX.RESERVED:
                tx.tx_type = TX.OUTGOING
                fields.append("tx_type")
                changes.append(f"{sku}: резерв → списано")
            if fields:
                tx.save(update_fields=fields)
    return changes


def _order_tx_date(order):
    """Дата руху замовлення — дата замовлення (як і раніше), без «naive datetime»."""
    d = order.order_date
    if not d:
        return timezone.now()
    if hasattr(d, "hour"):
        return d
    from datetime import datetime, time
    return timezone.make_aware(datetime.combine(d, time.min))


def release_order_stock(order, performed_by=None, *, restock=True, reason="Скасування"):
    """
    Повертає товар на склад (скасування, видалення):
      RESERVED → видаляються (бронь знімається)
      OUTGOING → компенсуються Adjustment (+qty, ключ so-cancel:<pk>), ідемпотентно
      компоненти BOM замовлення — так само
    restock=False — лише зняти резерв (товар фізично не повернувся).
    Повертає кількість оброблених транзакцій.
    """
    performer = _performer(performed_by)
    count = 0
    with transaction.atomic():
        txs = [tx for group in _order_txs(order).values() for tx in group]
        if restock:
            txs += _bom_txs(order)
        comps = _cancel_comps([tx.pk for tx in txs])
        for tx in txs:
            if tx.tx_type == TX.RESERVED:
                tx.delete()
                count += 1
            elif restock and tx.tx_type == TX.OUTGOING and tx.qty < 0 and tx.pk not in comps:
                _, created = InventoryTransaction.objects.get_or_create(
                    external_key=_cancel_key(tx.pk),
                    defaults=dict(
                        tx_type=TX.ADJUSTMENT,
                        qty=abs(tx.qty),
                        product_id=tx.product_id,
                        location_id=tx.location_id,
                        ref_doc=f"{reason} {_order_ref(order)}",
                        tx_date=timezone.now(),
                        performed_by=performer,
                    ),
                )
                count += int(created)
    return count


def restore_order_stock(order, performed_by=None):
    """Замовлення знову активне (знято «Скасовано»): позиції, які є в замовленні, знову тримають товар за
    поточним статусом і налаштуваннями — компенсація скасування прибирається, резерв відновлюється, а
    якщо за статусом ще нічого не списується — прибираються і рух, і компенсація. Повертає список змін."""
    if not order.pk or order.status == CANCELLED or not order.affects_stock:
        return []
    s = InventorySettings.get()
    target = order_target(order, s)
    changes = []
    with transaction.atomic():
        line_pks = set(order.lines.values_list("pk", flat=True))
        by_line = _order_txs(order)
        txs = [tx for lpk, group in by_line.items() if lpk in line_pks for tx in group
               if tx.tx_type == TX.OUTGOING]
        comps = _cancel_comps([tx.pk for tx in txs])
        for tx in txs:
            comp = comps.get(tx.pk)
            if comp is None:
                continue
            comp.delete()
            if target is None:
                tx.delete()
            elif target == TX.RESERVED:
                tx.tx_type = TX.RESERVED
                tx.save(update_fields=["tx_type"])
            changes.append(f"{tx.product.sku}: скасування знято")
        # Компоненти BOM: відправлене замовлення знову тримає їх; ще не відправлене — спишуться при «Відправлено»
        bom = _bom_txs(order)
        bom_comps = _cancel_comps([tx.pk for tx in bom])
        for tx in bom:
            comp = bom_comps.get(tx.pk)
            if comp is None:
                continue
            comp.delete()
            if order.status not in SHIPPED_STATUSES:
                tx.delete()
        changes += sync_order(order, settings=s, performed_by=performed_by)
    return changes


def release_line_stock(line, performed_by=None):
    """Позицію видаляють (у картці, повторним імпортом або разом із замовленням) — її товар повертається на
    склад, як при скасуванні: резерв знімається, списання компенсується. Повертає кількість рухів."""
    order = line.order
    if order.status == CANCELLED:
        return 0
    performer = _performer(performed_by)
    count = 0
    with transaction.atomic():
        txs = _order_txs(order).get(line.pk, [])
        comps = _cancel_comps([tx.pk for tx in txs])
        for tx in txs:
            if tx.tx_type == TX.RESERVED:
                tx.delete()
                count += 1
            elif tx.tx_type == TX.OUTGOING and tx.qty < 0 and tx.pk not in comps:
                InventoryTransaction.objects.create(
                    external_key=_cancel_key(tx.pk), tx_type=TX.ADJUSTMENT, qty=abs(tx.qty),
                    product_id=tx.product_id, location_id=tx.location_id,
                    ref_doc=f"Видалено позицію {_order_ref(order)}", tx_date=timezone.now(),
                    performed_by=performer)
                count += 1
    return count


def rekey_order(order, old_source, old_number):
    """Змінились джерело чи номер замовлення — рухи його позицій переходять на новий ключ (інакше
    замовлення «загубить» свої списання: скасування нічого не повернуло б). Повертає кількість рухів."""
    old_prefix = order_prefix(order, source=old_source, number=old_number)
    new_prefix = order_prefix(order)
    if old_prefix == new_prefix:
        return 0
    old_ref, new_ref = f"SO-{old_source}:{old_number}", _order_ref(order)
    count = 0
    with transaction.atomic():
        for tx in InventoryTransaction.objects.filter(external_key__startswith=old_prefix):
            tx.external_key = new_prefix + tx.external_key[len(old_prefix):]
            fields = ["external_key"]
            if tx.ref_doc == old_ref:
                tx.ref_doc = new_ref
                fields.append("ref_doc")
            tx.save(update_fields=fields)
            count += 1
    return count


def order_status_changed(order, old_status, *, performed_by=None):
    """Статус замовлення змінився (картка, масова дія, DigiKey, API) — склад за правилами:
    → «Скасовано» — товар повертається; зі «Скасовано» — знову за замовленням; інакше — sync_order."""
    if old_status == order.status:
        return []
    if order.status == CANCELLED:
        n = release_order_stock(order, performed_by)
        return [f"повернуто на склад: {n}"] if n else []
    if old_status == CANCELLED:
        return restore_order_stock(order, performed_by)
    return sync_order(order, performed_by=performed_by)


def order_stock_issues(order, settings=None):
    """Що «🔄 Синхронізувати склад» виправить у замовленні: [{line_pk, line_sku, line_qty, tx_sku, tx_qty,
    tx_type, product_diff, qty_diff, text}]. Нічого не змінює.
    Скасоване замовлення, яке ще тримає товар (скасоване до жовтня 2026, коли скасування товар не повертало), —
    кожен такий рух: «Синхронізувати» поверне його (release_order_stock)."""
    if not order.pk:
        return []
    if order.status == CANCELLED:
        txs = [tx for group in _order_txs(order).values() for tx in group
               if tx.tx_type in (TX.OUTGOING, TX.RESERVED) and tx.qty < 0]
        comps = _cancel_comps([tx.pk for tx in txs])
        return [{"line_pk": None, "line_sku": tx.product.sku, "line_qty": 0, "tx_sku": tx.product.sku,
                 "tx_qty": float(abs(tx.qty)), "tx_type": tx.tx_type, "tx_pk": tx.pk, "product_diff": False,
                 "qty_diff": True,
                 "text": f"{tx.product.sku}: скасоване замовлення ще тримає {_num(abs(tx.qty))} шт. "
                         f"({_label(tx.tx_type)}) — повернути на склад"}
                for tx in txs if tx.pk not in comps]
    if not order.affects_stock:
        return []
    s = settings or InventorySettings.get()
    target = order_target(order, s)
    by_line = _order_txs(order)
    comps = _cancel_comps([tx.pk for txs in by_line.values() for tx in txs])
    issues = []
    for line in order.lines.select_related("product").order_by("pk"):
        txs = [tx for tx in by_line.get(line.pk, [])
               if tx.tx_type in (TX.OUTGOING, TX.RESERVED) and tx.pk not in comps]
        want = max(line.qty or Decimal("0"), Decimal("0"))
        sku = line.product.sku if line.product_id else (line.sku_raw or "?")
        base = {"line_pk": line.pk, "line_sku": sku, "line_qty": float(want)}
        if not txs:
            if target is not None and line.product_id and want > 0:
                issues.append({**base, "tx_sku": "—", "tx_qty": 0, "tx_type": "", "product_diff": False,
                               "qty_diff": True, "text": f"{sku}: не записано на складі ({_label(target)} {_num(want)})"})
            continue
        tx = txs[0]
        product_diff = tx.product_id != line.product_id
        qty_diff = abs(tx.qty) != want or not line.product_id
        parts = []
        if product_diff:
            parts.append(f"SKU {tx.product.sku} → {sku}")
        if qty_diff:
            parts.append(f"к-сть {_num(abs(tx.qty))} → {_num(want if line.product_id else 0)}")
        if target == TX.OUTGOING and tx.tx_type == TX.RESERVED:
            parts.append("резерв → списано")
        if len(txs) > 1:
            parts.append(f"зайвих рухів: {len(txs) - 1}")
        if parts:
            issues.append({**base, "tx_sku": tx.product.sku, "tx_qty": float(abs(tx.qty)), "tx_type": tx.tx_type,
                           "tx_pk": tx.pk, "product_diff": product_diff, "qty_diff": qty_diff,
                           "text": f"{sku}: " + ", ".join(parts)})
    return issues


# ── Що замовити (аналіз запасів, сигнали) ─────────────────────────────────────

REORDER_COVER_MONTHS = 3      # запас на 3 місяці продажів
REORDER_SAFETY = 1.2          # + 20 %


def monthly_sales_qs(since):
    """Продані позиції для середніх продажів: замовлення, що впливають на склад, без скасованих."""
    from sales.models import SalesOrderLine
    return (SalesOrderLine.objects
            .filter(order__order_date__gte=since, order__affects_stock=True)
            .exclude(order__status=CANCELLED))


def low_stock_items(days=90):
    """Товари, які пора дозамовити, для попереджень в інтерфейсі (сторінка сигналів, дашборд):
    статус 🔥 / ⚠️ за reorder_advice (зокрема доступно ≤ точки дозамовлення). Порожньо, якщо в налаштуваннях
    складу вимкнено «Попередження про низький залишок». Сортування: 🔥 спершу, далі — менше місяців запасу."""
    if not InventorySettings.get().low_stock_alert_enabled:
        return []
    from datetime import timedelta
    today = timezone.now().date()

    def sold_since(since):
        return (monthly_sales_qs(since).filter(product=OuterRef("pk"))
                .values("product").annotate(t=Sum("qty")).values("t"))

    confirmed = (PurchaseOrderLine.objects
                 .filter(product=OuterRef("pk"), purchase_order__status__in=CONFIRMED_PO_STATUSES)
                 .values("product").annotate(t=Sum(F("qty_ordered") - F("qty_received"))).values("t"))
    qs = annotate_stock(Product.objects.filter(is_active=True)).annotate(
        _sold=Coalesce(Subquery(sold_since(today - timedelta(days=days)), output_field=_DEC), _ZERO),
        _sold_2y=Coalesce(Subquery(sold_since(today - timedelta(days=730)), output_field=_DEC), _ZERO),
        _confirmed=Coalesce(Subquery(confirmed, output_field=_DEC), _ZERO),
    ).order_by("sku")
    items = []
    for p in qs:
        monthly = float(p._sold) / (max(1, days) / 30.0)
        if monthly <= 0:
            monthly = float(p._sold_2y) / 24 / 6      # як «Що замовити»: давні продажі з меншою вагою
        d = reorder_advice(available=p._available, monthly=monthly, reorder_point=p.reorder_point,
                           incoming=p._confirmed)
        if d["status"] not in ("critical", "warning"):
            continue
        d.update(product=p, pk=p.pk, sku=p.sku, name=p.name, on_hand=float(p._on_hand),
                 reserved=float(p._reserved), available=float(p._available), incoming=float(p._confirmed),
                 reorder_point=p.reorder_point)
        items.append(d)
    order = {"critical": 0, "warning": 1}
    items.sort(key=lambda d: (order[d["status"]], d["months_left"] if d["months_left"] is not None else 99, d["sku"]))
    return items


def reorder_advice(*, available, monthly, reorder_point=0, incoming=0):
    """Скільки дозамовити й наскільки терміново (числа — float, як в адмінці).

      Вистачить на = Доступно ÷ продажі за місяць
      Ціль         = max(продажі за 3 міс + 20 %, Точка дозамовлення)
      Замовити     = Ціль − Доступно − Уже замовлено (відкриті закупівлі Ordered / Partial, ще не отримане);
                     понад 10 шт. — до десятків, але не менше, ніж треба до Точки дозамовлення
      Статус       = за «Вистачить на» (🔥 ≤ 0,5 міс або немає товару · ⚠️ ≤ 1,5 · 📉 ≤ 3 · ✅);
                     Доступно ≤ Точки дозамовлення — щонайменше ⚠️ Мало.
                     Без продажів і без точки дозамовлення — 💤 (рекомендації немає).
    """
    avail = float(available or 0)
    monthly = float(monthly or 0)
    rp = float(reorder_point or 0)
    inc = max(0.0, float(incoming or 0))
    shelf = max(0.0, avail)          # від'ємний залишок = борг, для розрахунку — 0
    below_rp = rp > 0 and avail <= rp
    if monthly <= 0 and rp <= 0:
        return {"stock": avail, "monthly": 0, "months_left": None, "reorder_qty": 0,
                "status": "no_sales", "below_reorder_point": False}

    target = max(monthly * REORDER_COVER_MONTHS * REORDER_SAFETY, rp)
    need = max(0.0, target - shelf - inc)
    qty = round(need / 10) * 10 if need > 10 else round(need)
    if rp > 0:
        qty = max(qty, math.ceil(max(0.0, rp - shelf - inc)))

    months_left = shelf / monthly if monthly > 0 else None
    if monthly > 0:
        if avail <= 0 or months_left <= 0.5:
            status = "critical"
        elif months_left <= 1.5:
            status = "warning"
        elif months_left <= 3:
            status = "low"
        else:
            status = "ok"
    else:
        status = "critical" if avail <= 0 and below_rp else "no_sales"
    if below_rp and status in ("ok", "low", "no_sales"):
        status = "warning"

    return {
        "stock": avail,
        "monthly": round(monthly, 1),
        "months_left": (round(months_left, 1) if avail >= 0 else 0.0) if months_left is not None else None,
        "reorder_qty": int(qty),
        "status": status,
        "below_reorder_point": below_rp,
    }
