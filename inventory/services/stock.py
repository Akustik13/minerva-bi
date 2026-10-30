"""
Сервіс залишків складу — єдина точка для API, адмінки та інтеграцій.

Семантика (так рахують API, огляд складу, аналіз запасів, Angebote, сигнали):
  on_hand   = Σ qty усіх транзакцій, крім RESERVED (фізично на складі)
  reserved  = заброньовано під замовлення й ще НЕ списано зі складу:
              −Σ qty RESERVED-транзакцій
              + позиції відкритих замовлень (affects_stock, статус не «Скасовано» / «Доставлено»),
                під які рух складу ще не записано (списання при «Відправлено» / «Доставлено»)
              Замовлення, вже списані (Outgoing), сюди не входять — їх уже немає в on_hand.
              Відправлене замовлення без руху — не бронь: товар фізично пішов (історія до обліку складу в
              Minerva або пропущене списання); виняток — режим «списувати при Доставлено», де відправлене
              ще в on_hand, і то лише коли склад товару вже вели на дату відправки (pending_order_lines).
  available = on_hand − reserved (можна продати)
  incoming  = Σ max(0, qty_ordered − qty_received) у відкритих закупівлях (переотримана позиція — 0, не мінус)

Замовлення продажу ↔ склад (sync_order, release_order_stock, restore_order_stock):
  кожна позиція замовлення тримає не більше одного руху з ключем
      so:<джерело>:<номер>:line:<pk позиції>:<uuid>
  — Outgoing (списано) або Резерв, залежно від налаштувань складу і статусу (order_target).
  Позиція з виробом BOM («Ключові компоненти») ще й списує компоненти — коли сама позиція списується
  (order_target = Outgoing), по одному руху на компонент:
      so-bom:<pk замовлення>:line:<pk позиції>:<pk виробу>:<pk компонента>:<uuid>
  (старі bom_deduction_<виріб>_<замовлення>_<компонент> належать першій позиції з цим виробом).
  Скасування повертає товар: резерв знімається, списання компенсується коригуванням
  so-cancel:<pk руху>; зняття скасування прибирає ці компенсації — товар знову за замовленням.
  Скасування через API з restock=false лишає списане списаним — позначка so-writeoff:<pk руху> (0 шт.).
  Зміни замовлення йдуть під блокуванням рядка замовлення (select_for_update) — два одночасні збереження
  не спишуть товар двічі.
"""
from __future__ import annotations

import copy
import math
import re
import uuid
from decimal import Decimal

from django.db import transaction
from django.db.models import (
    CharField, DecimalField, Exists, F, Func, Max, Min, OuterRef, Q, Subquery, Sum, Value,
)
from django.db.models.functions import Cast, Coalesce, Greatest
from django.utils import timezone

from inventory.models import (
    InventorySettings, InventoryTransaction, Location, Product, ProductAlias, ProductComponent,
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


def _ships_late(settings=None):
    """Режим «списувати при Доставлено»: відправлене замовлення ще в on_hand, доки його не доставлено."""
    s = settings or InventorySettings.get()
    return not s.use_reservation and s.deduct_on == InventorySettings.DeductOn.DELIVERED


def pending_order_lines(settings=None):
    """Позиції відкритих замовлень (affects_stock, не скасовані й не доставлені), під які ще не записано
    жодного руху складу — товар обіцяно, але він ще в on_hand (режими «списувати при Відправлено /
    Доставлено» або замовлення, внесене до зміни налаштувань).

    Відправлені без руху — не бронь: товар уже пішов, а склад або ще не вели (замовлення 2025 року, залишок
    уведено пізніше інвентаризацією), або списання пропущене («🔄 Синхронізувати склад»). Лише в режимі
    «при Доставлено» відправлене ще рахується — якщо на дату відправки склад товару вже вели."""
    from sales.models import SalesOrderLine
    qs = (SalesOrderLine.objects
          .filter(order__affects_stock=True, qty__gt=0, product__isnull=False)
          .exclude(order__status__in=NOT_PENDING_STATUSES)
          .filter(~Exists(_line_held_txs())))
    shipped = Q(order__status__in=SHIPPED_STATUSES)
    if not _ships_late(settings):
        return qs.exclude(shipped)
    tracked = (InventoryTransaction.objects.filter(product_id=OuterRef("product_id"))
               .exclude(tx_type=TX.RESERVED)
               .filter(tx_date__date__lte=Coalesce(OuterRef("order__shipped_at"), OuterRef("order__order_date"))))
    return qs.filter(~shipped | Exists(tracked))


def shipped_before_tracking(order, product_id) -> bool:
    """Відправлено раніше, ніж склад цього товару почали вести в Minerva (перший рух — пізніше за відправку,
    напр. інвентаризація 2026 року, а замовлення — 2025): у залишку цього товару вже немає, списувати нічого."""
    if order.status not in SHIPPED_STATUSES or not product_id:
        return False
    when = order.shipped_at or order.order_date
    if not when:
        return False
    first = (InventoryTransaction.objects.filter(product_id=product_id).exclude(tx_type=TX.RESERVED)
             .exclude(external_key__startswith=order_prefix(order)).aggregate(t=Min("tx_date"))["t"])
    return first is not None and when < timezone.localdate(first)


def _default_location_code():
    return (InventorySettings.get().default_location or "MAIN").strip()


# ── Очікується (відкриті закупівлі) ───────────────────────────────────────────

def po_open_qty():
    """Ще не отримано за позицією закупівлі: max(0, замовлено − отримано). Переотримана позиція (прийшло
    більше, ніж замовили) дає 0, а не мінус — інакше «Очікується» товару зменшувалось би за рахунок інших
    відкритих позицій."""
    return Greatest(F("qty_ordered") - F("qty_received"), _ZERO, output_field=_DEC)


def incoming_subquery(statuses=OPEN_PO_STATUSES):
    """Subquery для annotate (OuterRef — Product): скільки ще прийде за закупівлями в цих статусах."""
    return (PurchaseOrderLine.objects
            .filter(product=OuterRef("pk"), purchase_order__status__in=statuses)
            .values("product").annotate(t=Sum(po_open_qty())).values("t"))


def incoming_qty(product, statuses=OPEN_PO_STATUSES):
    """Скільки ще прийде товару за закупівлями в цих статусах (Decimal, не менше 0)."""
    return (PurchaseOrderLine.objects
            .filter(product=product, purchase_order__status__in=statuses)
            .aggregate(t=Sum(po_open_qty()))["t"]) or Decimal("0")


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
    incoming = incoming_subquery(OPEN_PO_STATUSES)
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
    """Залишки товару по кожній локації: [{location, on_hand, reserved, available}]. Сума «reserved» усіх
    рядків = reserved товару: ще не записані замовлення бронюють товар на локації за замовчуванням — і тоді,
    коли там цього товару ще немає жодного руху (рядок 0 / резерв / мінус)."""
    rows = list(InventoryTransaction.objects.filter(product=product)
                .values("location__code", "location__name")
                .annotate(
                    on_hand=Coalesce(Sum("qty", filter=~Q(tx_type=TX.RESERVED)), _ZERO),
                    reserved_neg=Coalesce(Sum("qty", filter=Q(tx_type=TX.RESERVED)), _ZERO),
                )
                .order_by("location__code"))
    pending = (pending_order_lines().filter(product=product)
               .aggregate(t=Sum("qty"))["t"]) or Decimal("0")
    default = _default_location_code()
    if pending and not any(r["location__code"] == default for r in rows):
        loc = Location.objects.filter(code=default).values("name").first()
        rows.append({"location__code": default, "location__name": loc["name"] if loc else default,
                     "on_hand": Decimal("0"), "reserved_neg": Decimal("0")})
        rows.sort(key=lambda r: r["location__code"] or "")
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
    items: [{"sku": str, "qty": Decimal}] → [{"sku", "requested", "requested_total", "available", "ok", "found"}].
    Однакові SKU сумуються; різні артикули одного товару (артикул і його аліас) перевіряються разом:
    requested_total — скільки цього товару просять усього, ok — чи вистачає на всіх.
    """
    wanted = {}
    for it in items:
        sku = str(it["sku"]).strip()
        wanted[sku] = wanted.get(sku, Decimal("0")) + Decimal(str(it["qty"]))

    products = {sku: resolve_sku(sku) for sku in wanted}
    ids = [p.pk for p in products.values() if p]
    stock = {p.pk: p for p in annotate_stock(Product.objects.filter(pk__in=ids))}
    per_product = {}
    for sku, qty in wanted.items():
        if products[sku]:
            pk = products[sku].pk
            per_product[pk] = per_product.get(pk, Decimal("0")) + qty

    result = []
    for sku, qty in wanted.items():
        p = products[sku]
        if not p:
            result.append({"sku": sku, "found": False, "requested": qty,
                           "available": Decimal("0"), "ok": False})
            continue
        avail = stock[p.pk]._available
        total = per_product[p.pk]
        result.append({"sku": sku, "product_sku": p.sku, "found": True,
                       "requested": qty, "requested_total": total, "available": avail,
                       "ok": p.is_active and avail >= total})
    return result


# ── Що вже тримає замовлення (для «вистачає / бракує» у картці й списку замовлень) ──

def annotate_line_held(lines_qs):
    """Позиції замовлень + _held_tx: скільки під позицію вже списано / заброньовано рухом складу;
    _held_out — чи під позицію вже є списання (Outgoing)."""
    held = (_line_held_txs().values("product").annotate(t=Sum("qty")).values("t"))
    return lines_qs.annotate(_held_tx=-Coalesce(Subquery(held, output_field=_DEC), _ZERO),
                             _held_out=Exists(_line_held_txs().filter(tx_type=TX.OUTGOING)))


def line_held(line):
    """Скільки товару вже рахується за цією позицією (списано, резерв або бронь незаписаного замовлення).
    available + line_held(line) — скільки було б вільно без цього замовлення. Потрібен annotate_line_held."""
    held = getattr(line, "_held_tx", None) or Decimal("0")
    if held > 0:
        return held
    o = line.order
    if (o.affects_stock and o.status not in NOT_PENDING_STATUSES and line.product_id and (line.qty or 0) > 0
            and (o.status not in SHIPPED_STATUSES or _ships_late())):
        return line.qty
    return Decimal("0")


def line_cover_held(line):
    """Скільки додати до «Доступно», щоб позиція не показувала нестачу власного товару (картка й список
    замовлень). Відправлена позиція, товар якої вже списано (Outgoing), — 0: товар пішов зі складу. Відправлена,
    але ще не списана (списання «при Доставлено») — як незакрита: її кількість ще в «Резерв».
    Потрібен annotate_line_held."""
    if line.order.status in SHIPPED_STATUSES and getattr(line, "_held_out", False):
        return Decimal("0")
    return line_held(line)


def order_lines_cover(order):
    """{pk позиції: {available, held, free}} для картки замовлення: free = доступно + те, що вже взяло це
    замовлення (його списання / резерв / бронь не рахуються як нестача). Відправлене й списане — free = available."""
    lines = list(annotate_line_held(order.lines.filter(product__isnull=False)).select_related("order"))
    avail = {pid: p._available for pid, p in stock_map({l.product_id for l in lines}).items()}
    out = {}
    for line in lines:
        a = avail.get(line.product_id, Decimal("0"))
        h = line_cover_held(line)
        out[line.pk] = {"available": a, "held": h, "free": a + h}
    return out


def open_order_lines(product_ids):
    """Позиції незакритих замовлень товарів зі станом для огляду складу:
    reserved — 🔒 резерв, pending — ще не списано (рахується в «Резерв»), deducted — уже списано зі складу.
    Відправлені й уже списані замовлення не показуються, як і відправлені без руху, які не бронюють товар
    (pending_order_lines: історія до обліку складу)."""
    from sales.models import SalesOrderLine
    product_ids = list(product_ids)
    qs = (SalesOrderLine.objects
          .filter(product_id__in=product_ids, order__affects_stock=True, qty__gt=0)
          .exclude(order__status__in=NOT_PENDING_STATUSES)
          .annotate(_held_type=Subquery(_line_held_txs().values("tx_type")[:1]))
          .select_related("order")
          .order_by("product_id", "order__order_date", "pk"))
    pending = set(pending_order_lines().filter(product_id__in=product_ids).values_list("pk", flat=True))
    out = []
    for line in qs:
        state = {TX.RESERVED: "reserved", TX.OUTGOING: "deducted"}.get(line._held_type, "pending")
        if state == "deducted" and line.order.status in SHIPPED_STATUSES:
            continue
        if state == "pending" and line.pk not in pending:
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


def _writeoff_key(tx_pk):
    return f"so-writeoff:{tx_pk}"


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


_LOCK_FIELDS = ("status", "affects_stock", "source", "order_number")


def _locked(order):
    """Блокує рядок замовлення до кінця транзакції (виклик — усередині transaction.atomic) і повертає замовлення
    зі свіжим станом з бази: друге одночасне збереження чекає, а потім бачить рухи й статус, записані першим, —
    товар не списується двічі. Стан той самий — той самий об'єкт; інакше копія з полями з бази (об'єкт того, хто
    викликав, не змінюється). None — замовлення в базі немає. SQLite блокування не підтримує — лише свіжий стан."""
    from sales.models import SalesOrder
    if not order.pk:
        return None
    row = SalesOrder.objects.select_for_update().filter(pk=order.pk).values(*_LOCK_FIELDS).first()
    if row is None:
        return None
    if all(getattr(order, f) == row[f] for f in _LOCK_FIELDS):
        return order
    fresh = copy.copy(order)
    for f, v in row.items():
        setattr(fresh, f, v)
    return fresh


def _order_txs(order, line_pks=None):
    """{pk позиції: [рухи]} — рухи з ключем позицій цього замовлення. line_pks — лише цих позицій: ключ
    so:<джерело>:<номер>:… не знає самого замовлення, тож рух позиції, якої в замовленні немає (старе замовлення
    з тим самим джерелом і номером), — не його."""
    prefix = order_prefix(order)
    out = {}
    for tx in (InventoryTransaction.objects.filter(external_key__startswith=prefix)
               .select_related("product").order_by("pk")):
        head = tx.external_key[len(prefix):].split(":", 1)[0]
        if head.isdigit() and (line_pks is None or int(head) in line_pks):
            out.setdefault(int(head), []).append(tx)
    return out


def _cancel_comps(tx_pks):
    """{pk руху: компенсація скасування} для цих рухів."""
    keys = {_cancel_key(pk): pk for pk in tx_pks}
    if not keys:
        return {}
    return {keys[c.external_key]: c for c in InventoryTransaction.objects.filter(external_key__in=list(keys))}


def _settled(tx_pks):
    """pk рухів, уже закритих скасуванням: компенсація so-cancel (товар повернувся) або позначка so-writeoff
    (API: скасовано без повернення — товар фізично не повернувся)."""
    keys = {}
    for pk in tx_pks:
        keys[_cancel_key(pk)] = pk
        keys[_writeoff_key(pk)] = pk
    if not keys:
        return set()
    return {keys[k] for k in InventoryTransaction.objects.filter(external_key__in=list(keys))
            .values_list("external_key", flat=True)}


def _drop_writeoffs(txs):
    """Замовлення знову активне — позначки «списано без повернення» його рухів більше не діють."""
    keys = [_writeoff_key(tx.pk) for tx in txs]
    if keys:
        InventoryTransaction.objects.filter(external_key__in=keys).delete()


# ── Компоненти BOM ────────────────────────────────────────────────────────────

_BOM_LEGACY = re.compile(r"^bom_deduction_([0-9]+)_([0-9]+)_([0-9]+)$")


def _bom_prefix(order_pk, line_pk=None):
    base = f"so-bom:{order_pk}:line:"
    return base if line_pk is None else f"{base}{line_pk}:"


def _order_bom_txs(order, lines=None):
    """{pk позиції: {(pk виробу, pk компонента): [рухи]}} — списання компонентів BOM позицій замовлення.
    Старий ключ bom_deduction_<виріб>_<замовлення>_<компонент> (до 10.2026: один на замовлення × виріб) належить
    першій позиції з цим виробом; якщо виробу в замовленні вже немає — ключ None."""
    if not order.pk:
        return {}
    if lines is None:
        lines = list(order.lines.order_by("pk"))
    first_with = {}
    for line in sorted(lines, key=lambda l: l.pk):
        if line.product_id:
            first_with.setdefault(line.product_id, line.pk)
    prefix = _bom_prefix(order.pk)
    out = {}
    qs = (InventoryTransaction.objects
          .filter(Q(external_key__startswith=prefix)
                  | Q(external_key__regex=rf"^bom_deduction_[0-9]+_{order.pk}_[0-9]+$"),
                  tx_type=TX.OUTGOING)
          .select_related("product").order_by("pk"))
    for tx in qs:
        if tx.external_key.startswith(prefix):
            parts = tx.external_key[len(prefix):].split(":")
            if len(parts) < 3 or not all(p.isdigit() for p in parts[:3]):
                continue
            line_pk, kit, comp = (int(p) for p in parts[:3])
        else:
            m = _BOM_LEGACY.match(tx.external_key)
            if not m:
                continue
            kit, comp = int(m.group(1)), int(m.group(3))
            line_pk = first_with.get(kit)
        out.setdefault(line_pk, {}).setdefault((kit, comp), []).append(tx)
    return out


def _bom_flat(bom_txs, line_pks=None):
    """Усі рухи з _order_bom_txs (лише цих позицій, якщо задано line_pks)."""
    return [tx for lpk, group in bom_txs.items() if line_pks is None or lpk in line_pks
            for txs in group.values() for tx in txs]


def _kit_boms(product_ids):
    """{pk виробу: {pk компонента: (кількість на 1 виріб, артикул компонента)}} — вироби з BOM «Ключові
    компоненти», без необов'язкових компонентів (так само рахує «Можна зібрати»)."""
    ids = [pk for pk in product_ids if pk]
    out = {}
    if not ids:
        return out
    for pc in (ProductComponent.objects
               .filter(parent_id__in=ids, parent__bom_type=Product.BomType.KEY, optional=False)
               .select_related("component")):
        out.setdefault(pc.parent_id, {})[pc.component_id] = (Decimal(str(pc.qty_per)), pc.component.sku)
    return out


def _component_location():
    """Звідки списуються компоненти: активний склад компонентів, інакше будь-яка активна локація."""
    return (Location.objects.filter(location_type=Location.LocationType.COMPONENTS, is_active=True).first()
            or Location.objects.filter(is_active=True).first())


def _bom_plan(order, lines, target):
    """Що має статися з компонентами BOM позицій (спільне для sync_order і order_stock_issues).

    Компоненти списуються разом із позицією — коли вона списується (target Outgoing): кількість на 1 виріб ×
    кількість позиції, по одному руху на компонент. Кількість позиції змінилась — рух виправляється на різницю;
    позицію змінили на інший товар чи поставили 0 — компоненти повертаються; компонент прибрали з BOM після
    списання — списане лишається (як і зміна налаштувань). Повертає (кроки, компенсації скасування), крок —
    {"do": drop | fix | create, line, kit, comp, sku, txs, need}."""
    bom_txs = _order_bom_txs(order, lines)
    comps = _cancel_comps([tx.pk for tx in _bom_flat(bom_txs)])
    kits = _kit_boms({l.product_id for l in lines})
    steps = []
    for line in lines:
        want = max(line.qty or Decimal("0"), Decimal("0"))
        bom = kits.get(line.product_id, {}) if line.product_id and want > 0 else {}
        held = {}
        for key, txs in bom_txs.get(line.pk, {}).items():
            live = [tx for tx in txs if tx.pk not in comps]
            if live:
                held[key] = live
        for (kit, comp), txs in held.items():
            step = {"line": line, "kit": kit, "comp": comp, "sku": txs[0].product.sku, "txs": txs}
            if want <= 0 or kit != line.product_id:
                steps.append({**step, "do": "drop", "need": Decimal("0")})
                continue
            per = bom.get(comp)
            need = per[0] * want if per else -txs[0].qty
            if len(txs) > 1 or txs[0].qty != -need:
                steps.append({**step, "do": "fix", "need": need})
        if target == TX.OUTGOING:
            for comp, (per, sku) in bom.items():
                if (line.product_id, comp) not in held:
                    steps.append({"do": "create", "line": line, "kit": line.product_id, "comp": comp, "sku": sku,
                                  "txs": [], "need": per * want})
    return steps, comps


def _note(order, text, once=None):
    """Дописати рядок у «Нотатку» замовлення (до 500 символів) запитом UPDATE, без save(): save() усередині
    сигналу post_save перезапустив би сигнали замовлення й «загубив» би зміну статусу. once — не дописувати,
    якщо в нотатці вже є такий текст."""
    from sales.models import SalesOrder
    current = SalesOrder.objects.filter(pk=order.pk).values_list("internal_note", flat=True).first() or ""
    if (once or text) in current:
        return
    note = (f"{current}\n{text}" if current else text)[:500]
    SalesOrder.objects.filter(pk=order.pk).update(internal_note=note)
    order.internal_note = note


def _apply_bom_plan(order, steps, comps, performer):
    """Виконати кроки _bom_plan. Нові списання — усі компоненти позиції разом і лише коли їх вистачає на складі
    (як і раніше); не вистачає — нічого не списується, у нотатці замовлення — попередження, наступна подія
    (статус, позиція, «🔄 Синхронізувати склад») спробує знову. Повертає список змін."""
    changes = []
    creating = {}
    for st in steps:
        line_sku = st["line"].product.sku if st["line"].product_id else (st["line"].sku_raw or "?")
        if st["do"] == "drop":
            back = sum((abs(tx.qty) for tx in st["txs"]), Decimal("0"))
            for tx in st["txs"]:
                _drop(tx, comps)
            changes.append(f"{line_sku}: компонент BOM {st['sku']} повернуто ({_num(back)})")
        elif st["do"] == "fix":
            tx = st["txs"][0]
            for extra in st["txs"][1:]:
                _drop(extra, comps)
            if tx.qty != -st["need"]:
                changes.append(f"{line_sku}: компонент BOM {st['sku']} {_num(abs(tx.qty))} → {_num(st['need'])}")
                tx.qty = -st["need"]
                tx.save(update_fields=["qty"])
        else:
            creating.setdefault(st["line"].pk, []).append(st)
    location = None
    for group in creating.values():
        line = group[0]["line"]
        kit_sku = line.product.sku
        on_hand = {p.pk: p._on_hand for p in annotate_stock(Product.objects.filter(pk__in=[st["comp"] for st in group]))}
        short = [st for st in group if on_hand.get(st["comp"], Decimal("0")) < st["need"]]
        if short:
            head = f"⚠️ БОМ {kit_sku}: компоненти не списано"
            text = head + " — бракує " + ", ".join(
                f"{st['sku']} (є {_num(on_hand.get(st['comp'], Decimal('0')))}, треба {_num(st['need'])})" for st in short)
            _note(order, text, once=head)
            changes.append(text)
            continue
        location = location or _component_location()
        if location is None:
            text = f"⚠️ БОМ {kit_sku}: компоненти не списано — немає активної локації складу"
            _note(order, text)
            changes.append(text)
            continue
        for st in group:
            InventoryTransaction.objects.create(
                external_key=f"{_bom_prefix(order.pk, line.pk)}{line.product_id}:{st['comp']}:{uuid.uuid4()}",
                tx_type=TX.OUTGOING, qty=-st["need"], product_id=st["comp"], location=location,
                ref_doc=_order_ref(order), tx_date=timezone.now(), performed_by=performer)
        summary = ", ".join(f"{st['sku']}: {_num(on_hand.get(st['comp'], Decimal('0')))}→"
                            f"{_num(on_hand.get(st['comp'], Decimal('0')) - st['need'])}" for st in group)
        _note(order, f"✅ БОМ: вилучено компоненти для {kit_sku} ({summary})")
        changes.append(f"{kit_sku}: компоненти BOM списано ({summary})")
    return changes


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


def _release_reservations(order):
    """«Впливає на склад» вимкнено: резерви позицій знімаються (бронь — лише обіцянка, товар фізично не рухався),
    тож вони не рахуються в «Резерв» і не «зависають» поза оглядом складу. Уже списане лишається — його повертає
    скасування. Викликати під _locked. Повертає список змін."""
    line_pks = set(order.lines.values_list("pk", flat=True))
    txs = [tx for group in _order_txs(order, line_pks).values() for tx in group if tx.tx_type == TX.RESERVED]
    comps = _cancel_comps([tx.pk for tx in txs])
    changes = []
    for tx in txs:
        changes.append(f"{tx.product.sku}: резерв {_num(abs(tx.qty))} знято (замовлення не впливає на склад)")
        _drop(tx, comps)
    return changes


def sync_order(order, *, settings=None, performed_by=None, shipping_now=False):
    """Позиції замовлення → рухи складу (подія: позицію додано / змінено, статус змінився, «Синхронізувати»).
    Відправлене без руху, яке відправили раніше, ніж почали вести склад товару (shipped_before_tracking), —
    історія: не списується; shipping_now — статус щойно змінився (відправляють зараз), тоді списується.

    Позиція без руху отримує рух за order_target; рух із іншим товаром чи кількістю виправляється на місці
    (повернення старого й списання нового — один рух, без подвійного повернення); резерв стає списанням,
    коли замовлення відправлено. Уже списане назад не повертається (лише скасування, видалення позиції або
    кількість 0). Компоненти BOM — за тими самими правилами (_bom_plan). Скасовані замовлення не змінюються;
    «не впливає на склад» — лише знімаються резерви. Працює під блокуванням рядка замовлення й зі свіжим станом
    з бази. Повертає список змін."""
    if not order.pk:
        return []
    performer = _performer(performed_by)
    changes = []
    with transaction.atomic():
        order = _locked(order)
        if order is None or order.status == CANCELLED:
            return []
        if not order.affects_stock:
            return _release_reservations(order)
        s = settings or InventorySettings.get()
        target = order_target(order, s)
        lines = list(order.lines.select_related("product").order_by("pk"))
        by_line = _order_txs(order, {l.pk for l in lines})
        comps = _cancel_comps([tx.pk for txs in by_line.values() for tx in txs])
        location = None
        for line in lines:
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
                if target is None or (not shipping_now and shipped_before_tracking(order, line.product_id)):
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
        steps, bom_comps = _bom_plan(order, lines, target)
        changes += _apply_bom_plan(order, steps, bom_comps, performer)
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
      компоненти BOM позицій — так само
    Лише позиції, які є в замовленні: рух позиції, якої немає, — сирота іншого (старого) замовлення з тим самим
    джерелом і номером. Компоненти BOM — усі рухи цього замовлення (їхній ключ — pk замовлення).
    restock=False — лише зняти резерв: товар фізично не повернувся, списане позначається so-writeoff:<pk> і більше
    не пропонується «повернути на склад» (order_stock_issues, видалення замовлення).
    Повертає кількість оброблених транзакцій (знятих резервів і компенсацій).
    """
    performer = _performer(performed_by)
    count = 0
    with transaction.atomic():
        order = _locked(order) or order
        line_pks = set(order.lines.values_list("pk", flat=True))
        txs = [tx for group in _order_txs(order, line_pks).values() for tx in group]
        txs += _bom_flat(_order_bom_txs(order))
        settled = _settled([tx.pk for tx in txs])
        for tx in txs:
            if tx.tx_type == TX.RESERVED:
                tx.delete()
                count += 1
            elif tx.tx_type == TX.OUTGOING and tx.qty < 0 and tx.pk not in settled:
                _, created = InventoryTransaction.objects.get_or_create(
                    external_key=_cancel_key(tx.pk) if restock else _writeoff_key(tx.pk),
                    defaults=dict(
                        tx_type=TX.ADJUSTMENT,
                        qty=abs(tx.qty) if restock else Decimal("0"),
                        product_id=tx.product_id,
                        location_id=tx.location_id,
                        ref_doc=(f"{reason} {_order_ref(order)}" if restock
                                 else f"{reason} без повернення на склад {_order_ref(order)}"),
                        tx_date=timezone.now(),
                        performed_by=performer,
                    ),
                )
                if restock:
                    count += int(created)
    return count


def restore_order_stock(order, performed_by=None):
    """Замовлення знову активне (знято «Скасовано»): позиції, які є в замовленні, знову тримають товар за
    поточним статусом і налаштуваннями — компенсація скасування прибирається, резерв відновлюється, а
    якщо за статусом ще нічого не списується — прибираються і рух, і компенсація (компоненти BOM — так само).
    Позначки «списано без повернення» знімаються. Повертає список змін."""
    if not order.pk:
        return []
    s = InventorySettings.get()
    changes = []
    with transaction.atomic():
        order = _locked(order)
        if order is None or order.status == CANCELLED or not order.affects_stock:
            return []
        target = order_target(order, s)
        line_pks = set(order.lines.values_list("pk", flat=True))
        txs = [tx for group in _order_txs(order, line_pks).values() for tx in group if tx.tx_type == TX.OUTGOING]
        comps = _cancel_comps([tx.pk for tx in txs])
        _drop_writeoffs(txs)
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
        # Компоненти BOM списуються разом із позицією: позиція знову списана — компоненти теж, інакше — прибираються
        bom = _bom_flat(_order_bom_txs(order), line_pks)
        bom_comps = _cancel_comps([tx.pk for tx in bom])
        _drop_writeoffs(bom)
        for tx in bom:
            comp = bom_comps.get(tx.pk)
            if comp is None:
                continue
            comp.delete()
            if target != TX.OUTGOING:
                tx.delete()
            changes.append(f"{tx.product.sku}: скасування знято (компонент BOM)")
        changes += sync_order(order, settings=s, performed_by=performed_by)
    return changes


def release_line_stock(line, performed_by=None):
    """Позицію видаляють (у картці, повторним імпортом або разом із замовленням) — її товар повертається на
    склад, як при скасуванні: резерв знімається, списання компенсується (компоненти BOM позиції — теж).
    Повертає кількість рухів."""
    order = line.order
    performer = _performer(performed_by)
    count = 0
    with transaction.atomic():
        order = _locked(order) or order
        if order.status == CANCELLED:
            return 0
        txs = _order_txs(order, {line.pk}).get(line.pk, [])
        txs += _bom_flat(_order_bom_txs(order), {line.pk})
        settled = _settled([tx.pk for tx in txs])
        for tx in txs:
            if tx.tx_type == TX.RESERVED:
                tx.delete()
                count += 1
            elif tx.tx_type == TX.OUTGOING and tx.qty < 0 and tx.pk not in settled:
                _, created = InventoryTransaction.objects.get_or_create(
                    external_key=_cancel_key(tx.pk),
                    defaults=dict(tx_type=TX.ADJUSTMENT, qty=abs(tx.qty), product_id=tx.product_id,
                                  location_id=tx.location_id, ref_doc=f"Видалено позицію {_order_ref(order)}",
                                  tx_date=timezone.now(), performed_by=performer))
                count += int(created)
    return count


def rekey_order(order, old_source, old_number):
    """Змінились джерело чи номер замовлення — рухи його позицій переходять на новий ключ (інакше
    замовлення «загубить» свої списання: скасування нічого не повернуло б). Компоненти BOM не переходять —
    їхній ключ за pk замовлення. Повертає кількість рухів."""
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
        InventoryTransaction.objects.filter(external_key__startswith=_bom_prefix(order.pk),
                                            ref_doc=old_ref).update(ref_doc=new_ref)
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
    return sync_order(order, performed_by=performed_by, shipping_now=old_status is not None)


def order_saved(order, old, *, performed_by=None):
    """Замовлення збережено (inventory/signals): old — {status, affects_stock, source, order_number} до
    збереження, None — нове замовлення. Склад за правилами: новий ключ (джерело / номер), статус
    (order_status_changed), перемикач «Впливає на склад» (вимкнули — резерви знімаються, уже списане лишається;
    увімкнули — за статусом і налаштуваннями). Повертає список змін."""
    if old is None:
        return order_status_changed(order, None, performed_by=performed_by)
    if (old["source"], old["order_number"]) != (order.source, order.order_number):
        rekey_order(order, old["source"], old["order_number"])
    if old["status"] != order.status:
        return order_status_changed(order, old["status"], performed_by=performed_by)
    if old["affects_stock"] != order.affects_stock:
        return sync_order(order, performed_by=performed_by)
    return []


def _issue_text_bom(st, line_sku):
    if st["do"] == "drop":
        back = sum((abs(tx.qty) for tx in st["txs"]), Decimal("0"))
        return f"{line_sku}: компонент BOM {st['sku']} повернути на склад ({_num(back)})"
    if st["do"] == "fix":
        parts = []
        if st["txs"][0].qty != -st["need"]:
            parts.append(f"{_num(abs(st['txs'][0].qty))} → {_num(st['need'])}")
        if len(st["txs"]) > 1:
            parts.append(f"зайвих рухів: {len(st['txs']) - 1}")
        return f"{line_sku}: компонент BOM {st['sku']} " + ", ".join(parts)
    return f"{line_sku}: компонент BOM {st['sku']} не списано ({_num(st['need'])})"


def order_stock_issues(order, settings=None):
    """Що «🔄 Синхронізувати склад» виправить у замовленні: [{line_pk, line_sku, line_qty, tx_sku, tx_qty,
    tx_type, product_diff, qty_diff, text}]. Нічого не змінює.
    Скасоване замовлення, яке ще тримає товар (скасоване до жовтня 2026, коли скасування товар не повертало), —
    кожен такий рух позицій замовлення і його компонентів BOM: «Синхронізувати» поверне його (release_order_stock).
    Списане без повернення (API restock=false) не пропонується. «Не впливає на склад» — резерви, які ще тримаються."""
    if not order.pk:
        return []
    line_pks = set(order.lines.values_list("pk", flat=True))
    if order.status == CANCELLED:
        txs = [tx for group in _order_txs(order, line_pks).values() for tx in group
               if tx.tx_type in (TX.OUTGOING, TX.RESERVED) and tx.qty < 0]
        txs += [tx for tx in _bom_flat(_order_bom_txs(order)) if tx.qty < 0]
        settled = _settled([tx.pk for tx in txs])
        return [{"line_pk": None, "line_sku": tx.product.sku, "line_qty": 0, "tx_sku": tx.product.sku,
                 "tx_qty": float(abs(tx.qty)), "tx_type": tx.tx_type, "tx_pk": tx.pk, "product_diff": False,
                 "qty_diff": True,
                 "text": f"{tx.product.sku}: скасоване замовлення ще тримає {_num(abs(tx.qty))} шт. "
                         f"({_label(tx.tx_type)}) — повернути на склад"}
                for tx in txs if tx.pk not in settled]
    if not order.affects_stock:
        return [{"line_pk": None, "line_sku": tx.product.sku, "line_qty": 0, "tx_sku": tx.product.sku,
                 "tx_qty": float(abs(tx.qty)), "tx_type": tx.tx_type, "tx_pk": tx.pk, "product_diff": False,
                 "qty_diff": True,
                 "text": f"{tx.product.sku}: замовлення не впливає на склад, а резерв {_num(abs(tx.qty))} шт. "
                         f"ще тримається — зняти"}
                for group in _order_txs(order, line_pks).values() for tx in group if tx.tx_type == TX.RESERVED]
    s = settings or InventorySettings.get()
    target = order_target(order, s)
    lines = list(order.lines.select_related("product").order_by("pk"))
    by_line = _order_txs(order, line_pks)
    comps = _cancel_comps([tx.pk for txs in by_line.values() for tx in txs])
    issues = []
    for line in lines:
        txs = [tx for tx in by_line.get(line.pk, [])
               if tx.tx_type in (TX.OUTGOING, TX.RESERVED) and tx.pk not in comps]
        want = max(line.qty or Decimal("0"), Decimal("0"))
        sku = line.product.sku if line.product_id else (line.sku_raw or "?")
        base = {"line_pk": line.pk, "line_sku": sku, "line_qty": float(want)}
        if not txs:
            if (target is not None and line.product_id and want > 0
                    and not shipped_before_tracking(order, line.product_id)):
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
    steps, _ = _bom_plan(order, lines, target)
    for st in steps:
        line = st["line"]
        sku = line.product.sku if line.product_id else (line.sku_raw or "?")
        issues.append({"line_pk": line.pk, "line_sku": sku, "line_qty": float(max(line.qty or 0, 0)),
                       "tx_sku": st["sku"], "tx_qty": float(abs(st["txs"][0].qty)) if st["txs"] else 0,
                       "tx_type": TX.OUTGOING if st["txs"] else "", "product_diff": False, "qty_diff": True,
                       "text": _issue_text_bom(st, sku)})
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

    confirmed = incoming_subquery(CONFIRMED_PO_STATUSES)
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
