"""
Сервіс залишків складу — єдина точка для API та інтеграцій.

Семантика (узгоджена з inventory/admin.py):
  on_hand   = Σ qty усіх транзакцій, крім RESERVED (фізично на складі)
  reserved  = −Σ qty RESERVED-транзакцій (заброньовано під замовлення)
  available = on_hand − reserved (можна продати)
  incoming  = Σ (qty_ordered − qty_received) у відкритих закупівлях
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import transaction
from django.db.models import (
    DecimalField, F, Max, OuterRef, Q, Subquery, Sum, Value,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from inventory.models import (
    InventorySettings, InventoryTransaction, Location, Product, ProductAlias,
    PurchaseOrderLine,
)

TX = InventoryTransaction.TxType
_DEC = DecimalField(max_digits=18, decimal_places=3)
_ZERO = Value(Decimal("0"), output_field=_DEC)
OPEN_PO_STATUSES = ("draft", "ordered", "partial")


class StockError(Exception):
    """Помилка бізнес-логіки складу (повертається клієнту як 400/409)."""

    def __init__(self, message, code="invalid", details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


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

    return qs.annotate(
        _on_hand=Coalesce(Subquery(on_hand, output_field=_DEC), _ZERO),
        _reserved=-Coalesce(Subquery(reserved, output_field=_DEC), _ZERO),
        _incoming=Coalesce(Subquery(incoming, output_field=_DEC), _ZERO),
        _last_movement=Subquery(last_move),
    ).annotate(_available=F("_on_hand") - F("_reserved"))


def location_breakdown(product):
    """Залишки товару по кожній локації: [{location, on_hand, reserved, available}]."""
    rows = (InventoryTransaction.objects.filter(product=product)
            .values("location__code", "location__name")
            .annotate(
                on_hand=Coalesce(Sum("qty", filter=~Q(tx_type=TX.RESERVED)), _ZERO),
                reserved_neg=Coalesce(Sum("qty", filter=Q(tx_type=TX.RESERVED)), _ZERO),
            )
            .order_by("location__code"))
    return [{
        "location": r["location__code"],
        "location_name": r["location__name"],
        "on_hand": r["on_hand"],
        "reserved": -r["reserved_neg"],
        "available": r["on_hand"] + r["reserved_neg"],
    } for r in rows]


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

    if tx_type == TX.INCOMING:
        qty = abs(qty)
    elif tx_type == TX.OUTGOING:
        qty = -abs(qty)
    elif tx_type != TX.ADJUSTMENT:
        raise StockError("tx_type має бути Incoming, Outgoing або Adjustment.", code="bad_tx_type")

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


def release_order_stock(order, performed_by=None):
    """
    Повертає товар на склад при скасуванні замовлення:
      RESERVED → видаляються (бронь знімається)
      OUTGOING → компенсуються Adjustment (+qty), ідемпотентно
    Повертає кількість оброблених транзакцій.
    """
    prefix = f"so:{order.source}:{order.order_number}:line:"
    count = 0
    with transaction.atomic():
        txs = list(InventoryTransaction.objects.filter(external_key__startswith=prefix))
        for tx in txs:
            if tx.tx_type == TX.RESERVED:
                tx.delete()
                count += 1
            elif tx.tx_type == TX.OUTGOING and tx.qty < 0:
                _, created = InventoryTransaction.objects.get_or_create(
                    external_key=f"so-cancel:{tx.pk}",
                    defaults=dict(
                        tx_type=TX.ADJUSTMENT,
                        qty=abs(tx.qty),
                        product_id=tx.product_id,
                        location_id=tx.location_id,
                        ref_doc=f"Скасування SO-{order.source}:{order.order_number}",
                        tx_date=timezone.now(),
                        performed_by=performed_by,
                    ),
                )
                count += int(created)
    return count
