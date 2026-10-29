"""Закупівлі, які зробила зовнішня система (CRM), — REST API /purchase-orders/ (api/purchases.py).

CRM сама замовляє в постачальника й передає Minerva: у кого, що, скільки, коли, номер постачальника, трек-номер,
орієнтовну доставку. Minerva веде це як закупівлю (PurchaseOrder) — на складі товар видно в «Очікується»
(incoming = замовлено − отримано у відкритих закупівлях). Коли CRM повідомляє «доставлено», Minerva оприбутковує
(InventorySettings.api_delivery_mode = receive; той самий шлях, що «Отримано» в огляді складу — сигнал
qty_received → InventoryTransaction) або лише позначає «прибуло» (arrived).

Без дублікатів:
- замовлення — одне на (source, external_id): повтор POST оновлює те саме (UniqueConstraint);
- позиція — за line_id зовнішньої системи, інакше за артикулом;
- трек-номер — одне вхідне відправлення (IncomingShipment) на (замовлення, номер);
- доставка — одна на (замовлення, ключ): delivery_id, інакше tracking:<номер>, інакше all (уся решта)
  (PurchaseReceipt) — повтор нічого не додає на склад;
- більше, ніж замовлено, — лише з allow_over (інакше over_receipt: схоже на ту саму доставку під іншим id).
"""
from __future__ import annotations

from datetime import datetime, time as dtime
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from ..models import (IncomingShipment, InventorySettings, PurchaseOrder, PurchaseOrderLine, PurchaseReceipt,
                      Supplier)
from .stock import resolve_sku

ZERO = Decimal("0")
API_STATUSES = {PurchaseOrder.Status.ORDERED, PurchaseOrder.Status.CANCELLED}   # partial / received — лише Minerva
SHIPMENT_STATUSES = {c for c, _ in IncomingShipment.Status.choices}
LOCKED = {PurchaseOrder.Status.RECEIVED, PurchaseOrder.Status.CANCELLED}


class PurchaseError(Exception):
    """Помилка для відповіді API: code — машинний, status — HTTP."""

    def __init__(self, message, code="invalid", status=400, details=None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details or {}


# ── Пошук ─────────────────────────────────────────────────────────────────────

def find(source: str, ref: str) -> PurchaseOrder | None:
    """Код Minerva (PO-12) або «ext:<номер у зовнішній системі>» цієї системи."""
    ref = (ref or "").strip()
    qs = PurchaseOrder.objects.select_related("supplier")
    if ref.startswith("ext:"):
        return qs.filter(external_source=source, external_id=ref[4:]).first()
    return qs.filter(code=ref).first()


def find_supplier(value) -> Supplier:
    """Рядок (назва) або {name, email, contact_person, phone, website, country, currency}: назва без регістру →
    email → нова картка постачальника. Порожні поля наявної картки доповнюються, заповнені не перезаписуються."""
    data = {"name": value} if isinstance(value, str) else dict(value or {})
    name = str(data.get("name") or "").strip()[:255]
    email = str(data.get("email") or "").strip()[:254]
    if not name and not email:
        raise PurchaseError("Не вказано постачальника (supplier.name).", code="supplier_required")
    supplier = (Supplier.objects.filter(name__iexact=name).first() if name else None) or \
               (Supplier.objects.filter(email__iexact=email).first() if email else None)
    fields = {"email": email, "contact_person": str(data.get("contact_person") or "").strip()[:255],
              "phone": str(data.get("phone") or "").strip()[:50], "website": str(data.get("website") or "").strip()[:200],
              "addr_country": str(data.get("country") or "").strip().upper()[:2],
              "currency": str(data.get("currency") or "").strip().upper()[:3]}
    if supplier is None:
        return Supplier.objects.create(name=name or email, **{k: v for k, v in fields.items() if v})
    changed = [k for k, v in fields.items() if v and not getattr(supplier, k)]
    for k in changed:
        setattr(supplier, k, fields[k])
    if changed:
        supplier.save(update_fields=changed)
    return supplier


def _line_for(po: PurchaseOrder, item: dict, lines=None) -> PurchaseOrderLine | None:
    """Позиція замовлення за line_id зовнішньої системи, інакше за артикулом (з рештою — першою)."""
    lines = list(po.lines.select_related("product")) if lines is None else lines
    line_id = str(item.get("line_id") or "").strip()
    if line_id:
        return next((l for l in lines if l.external_line_id == line_id), None)
    sku = str(item.get("sku") or "").strip()
    if not sku:
        return None
    product = resolve_sku(sku)
    same = [l for l in lines if (product and l.product_id == product.pk) or l.external_sku.lower() == sku.lower()]
    return next((l for l in same if l.qty_received < l.qty_ordered), same[0] if same else None)


def _date(value, field):
    if value in (None, ""):
        return None
    if hasattr(value, "year"):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        raise PurchaseError(f"{field}: очікується дата РРРР-ММ-ДД.", code="bad_date", details={"field": field})


def _dt(value):
    if value in (None, ""):
        return timezone.now()
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.combine(_date(value, "delivered_at"), dtime(12, 0))
        except PurchaseError:
            raise PurchaseError("delivered_at: очікується дата або дата й час ISO 8601.", code="bad_date")
    return dt if timezone.is_aware(dt) else timezone.make_aware(dt)


def _qty(value, field="qty") -> Decimal:
    try:
        q = Decimal(str(value))
    except Exception:  # noqa: BLE001
        raise PurchaseError(f"{field}: має бути числом.", code="bad_qty")
    if q < 0:
        raise PurchaseError(f"{field}: не може бути від'ємною.", code="bad_qty")
    return q


# ── Замовлення ────────────────────────────────────────────────────────────────

def upsert(data: dict, source: str, po: PurchaseOrder | None = None, strict: bool = False):
    """Створити або оновити закупівлю зовнішньої системи. Повертає (po, created, warnings).

    po — для PATCH (адресу вже знайдено); інакше шукається за (source, external_id)."""
    warnings = []
    with transaction.atomic():
        created = False
        if po is None:
            external_id = str(data.get("external_id") or "").strip()
            if not external_id:
                raise PurchaseError("external_id — номер замовлення у вашій системі — обов'язковий.",
                                    code="external_id_required")
            po = PurchaseOrder.objects.select_for_update().filter(external_source=source,
                                                                  external_id=external_id).first()
            if po is None:
                try:
                    with transaction.atomic():
                        po = PurchaseOrder.objects.create(external_source=source, external_id=external_id[:64],
                                                          status=PurchaseOrder.Status.ORDERED)
                    created = True
                except IntegrityError:   # паралельний запит з тим самим номером створив його першим
                    po = PurchaseOrder.objects.select_for_update().get(external_source=source,
                                                                       external_id=external_id)
        else:
            po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
        _apply_header(po, data, created)
        if "lines" in data:
            warnings += _apply_lines(po, data.get("lines") or [], strict)
        if data.get("tracking_number"):
            upsert_shipment(po, {k: data.get(k) for k in
                                 ("tracking_number", "carrier", "tracking_url", "shipment_status", "expected_date")
                                 if data.get(k) not in (None, "")}, source)
        _totals(po)
        refresh_status(po)
    return po, created, warnings


def _apply_header(po: PurchaseOrder, data: dict, created: bool):
    if "supplier" in data and data["supplier"]:
        po.supplier = find_supplier(data["supplier"])
    elif created and po.supplier_id is None:
        raise PurchaseError("Не вказано постачальника (supplier).", code="supplier_required")
    for field, key in (("order_date", "order_date"), ("expected_date", "expected_date")):
        if key in data:
            value = _date(data[key], key)
            if value or key == "expected_date":
                setattr(po, field, value)
    for field, key, size in (("supplier_order_no", "supplier_order_no", 64), ("currency", "currency", 8),
                             ("notes", "notes", 4000), ("shipping_service", "carrier", 255)):
        if key in data and data[key] is not None:
            setattr(po, field, str(data[key]).strip()[:size])
    if "tracking_number" in data and data["tracking_number"]:
        po.tracking_number = str(data["tracking_number"]).strip()[:255]
    status = data.get("status")
    if status:
        if status not in API_STATUSES:
            raise PurchaseError("status: зовнішня система ставить лише ordered або cancelled "
                                "(partial / received Minerva рахує сама з доставок).", code="bad_status")
        po.status = status
    po.save()


def _apply_lines(po: PurchaseOrder, items: list, strict: bool) -> list:
    """Позиції з запиту: оновити знайдені (line_id / артикул), додати нові; не згадані — не чіпаються,
    qty 0 — скасувати позицію. Кількість не може стати меншою за вже отриману."""
    if not isinstance(items, list):
        raise PurchaseError("lines: очікується список позицій.", code="bad_lines")
    warnings, lines, seen = [], list(po.lines.select_related("product")), set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise PurchaseError(f"lines[{i}]: очікується об'єкт.", code="bad_lines")
        sku = str(item.get("sku") or "").strip()[:100]
        line_id = str(item.get("line_id") or "").strip()[:64]
        if not sku and not line_id:
            raise PurchaseError(f"lines[{i}]: потрібен sku або line_id.", code="bad_lines")
        key = line_id or "sku:" + sku.lower()
        if key in seen:
            raise PurchaseError(f"lines[{i}]: позиція повторюється — однаковий артикул кількома рядками "
                                f"передавайте з різними line_id.", code="duplicate_line", details={"sku": sku})
        seen.add(key)
        qty = _qty(item.get("qty", 0), f"lines[{i}].qty")
        line = _line_for(po, item, lines)
        product = resolve_sku(sku) if sku else (line.product if line else None)
        if sku and product is None:
            if strict:
                raise PurchaseError(f"Товар «{sku}» не знайдено в Minerva.", code="sku_not_found",
                                    status=404, details={"sku": sku})
            warnings.append({"code": "sku_not_found", "sku": sku,
                             "message": f"Товару «{sku}» немає в Minerva — позицію збережено без товару, "
                                        f"на складі вона не рахується."})
        if line is None:
            line = PurchaseOrderLine(purchase_order=po, qty_received=ZERO)
            lines.append(line)
        elif qty < line.qty_received:
            raise PurchaseError(f"lines[{i}]: кількість {qty} менша за вже отриману {line.qty_received}.",
                                code="qty_below_received", status=409,
                                details={"sku": sku or line.external_sku, "qty_received": str(line.qty_received)})
        line.product = product
        line.qty_ordered = qty
        line.external_line_id = line_id or line.external_line_id
        line.external_sku = sku or line.external_sku
        if "description" in item or not line.description:
            line.description = str(item.get("description") or (product.name if product else sku) or "")[:255]
        if item.get("unit_price") not in (None, ""):
            line.unit_price = _qty(item["unit_price"], f"lines[{i}].unit_price")
        line.currency = str(item.get("currency") or po.currency or line.currency)[:8]
        if "notes" in item:
            line.notes = str(item.get("notes") or "")[:255]
        line.save()
    return warnings


def _totals(po: PurchaseOrder):
    lines = list(po.lines.all())
    priced = [l.qty_ordered * l.unit_price for l in lines if l.unit_price is not None]
    if priced:
        PurchaseOrder.objects.filter(pk=po.pk).update(total_price=sum(priced, ZERO))
        po.total_price = sum(priced, ZERO)


def refresh_status(po: PurchaseOrder):
    """Стан з позицій (як «Отримано» в огляді складу): усе отримано → received, частина → partial."""
    if po.status == PurchaseOrder.Status.CANCELLED:
        return
    lines = [l for l in po.lines.all() if l.qty_ordered > 0]
    ordered = sum((l.qty_ordered for l in lines), ZERO)
    received = sum((min(l.qty_received, l.qty_ordered) for l in lines), ZERO)
    status = po.status
    if lines and received >= ordered:
        status = PurchaseOrder.Status.RECEIVED
    elif received > 0:
        status = PurchaseOrder.Status.PARTIAL
    elif status in (PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.PARTIAL, PurchaseOrder.Status.RECEIVED):
        status = PurchaseOrder.Status.ORDERED
    fields = []
    if status != po.status:
        po.status = status
        fields.append("status")
    if status == PurchaseOrder.Status.RECEIVED and not po.received_date:
        po.received_date = timezone.localdate()
        fields.append("received_date")
    if fields:
        po.save(update_fields=fields + ["updated_at"])


def cancel(po: PurchaseOrder, reason: str = ""):
    """Скасувати: решта замовлення більше не очікується на складі (отримане лишається)."""
    with transaction.atomic():
        po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
        po.status = PurchaseOrder.Status.CANCELLED
        if reason:
            po.notes = (po.notes + "\n" if po.notes else "") + f"Скасовано: {reason}"[:500]
        po.save()
    return po


# ── Трекінг ───────────────────────────────────────────────────────────────────

def upsert_shipment(po: PurchaseOrder, data: dict, source: str = "") -> IncomingShipment:
    """Вхідне відправлення закупівлі: одне на (замовлення, трек-номер). Статус delivered → deliver().

    Без status наявне відправлення лишає свій стан (повтор замовлення з тим самим трек-номером не повертає
    «на митниці» в «в дорозі»); уже отримане в Minerva (delivered) статусу не змінює."""
    number = str(data.get("tracking_number") or "").strip()[:255]
    if not number:
        raise PurchaseError("tracking_number — обов'язковий.", code="tracking_required")
    status = data.get("status") or data.get("shipment_status") or ""
    if status and status not in SHIPMENT_STATUSES:
        raise PurchaseError(f"status: одне з {', '.join(sorted(SHIPMENT_STATUSES))}.", code="bad_status")
    with transaction.atomic():
        po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
        shipment = po.incoming_shipments.filter(tracking_number__iexact=number).first() or \
            IncomingShipment(purchase_order=po, tracking_number=number, status=IncomingShipment.Status.IN_TRANSIT)
        for field, key, size in (("carrier_name", "carrier", 100), ("tracking_url", "tracking_url", 200),
                                 ("carrier_status_label", "carrier_status", 255),
                                 ("origin_country", "origin_country", 3), ("origin_city", "origin_city", 100),
                                 ("notes", "notes", 4000)):
            if data.get(key) not in (None, ""):
                setattr(shipment, field, str(data[key]).strip()[:size])
        for field in ("expected_date", "shipped_date"):
            if field in data:
                setattr(shipment, field, _date(data[field], field))
        if isinstance(data.get("events"), list):
            shipment.tracking_events = data["events"][-100:]
            shipment.last_tracked_at = timezone.now()
        if status and status != IncomingShipment.Status.DELIVERED and \
                shipment.status != IncomingShipment.Status.DELIVERED:
            shipment.status = status
        shipment.save()
        fields = []
        if shipment.expected_date and po.expected_date != shipment.expected_date:
            po.expected_date = shipment.expected_date
            fields.append("expected_date")
        if not po.tracking_number:
            po.tracking_number = number
            po.shipping_service = po.shipping_service or shipment.carrier_name
            fields += ["tracking_number", "shipping_service"]
        if fields:
            po.save(update_fields=fields + ["updated_at"])
        if status == IncomingShipment.Status.DELIVERED:
            deliver(po, {"tracking_number": number, "delivered_at": data.get("delivered_at"),
                         "delivery_id": data.get("delivery_id"), "lines": data.get("lines"),
                         "allow_over": data.get("allow_over")}, source)
            shipment.refresh_from_db()
    return shipment


# ── Доставка ──────────────────────────────────────────────────────────────────

def deliver(po: PurchaseOrder, data: dict, source: str = "") -> dict:
    """Зовнішня система повідомила «доставлено». Повертає {duplicate, delivery_id, booked, received[], status,
    warnings[]}. Один ключ доставки — одне оприбуткування; повтор → duplicate без змін складу."""
    tracking = str(data.get("tracking_number") or "").strip()[:255]
    items = data.get("lines")
    delivery_id = str(data.get("delivery_id") or "").strip()[:100]
    if items is not None and not isinstance(items, list):
        raise PurchaseError("lines: очікується список позицій.", code="bad_lines")
    if items and not (delivery_id or tracking):
        raise PurchaseError("Для доставки з переліком позицій потрібен delivery_id (або tracking_number) — "
                            "так Minerva відрізняє повтор від нової доставки.", code="delivery_id_required")
    key = delivery_id or (f"tracking:{tracking.lower()}" if tracking else "all")
    delivered_at = _dt(data.get("delivered_at"))
    booked = InventorySettings.get().api_delivery_mode == InventorySettings.API_DELIVERY_RECEIVE
    warnings = []
    with transaction.atomic():
        po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
        existing = po.receipts.filter(delivery_key=key).first()
        if existing:
            return _delivery_result(po, existing, duplicate=True)
        if po.status == PurchaseOrder.Status.CANCELLED:
            raise PurchaseError("Закупівлю скасовано — доставку не прийнято.", code="cancelled", status=409)
        lines = list(po.lines.select_related("product"))
        plan = []
        if items:
            for i, item in enumerate(items):
                line = _line_for(po, item if isinstance(item, dict) else {}, lines)
                if line is None:
                    raise PurchaseError(f"lines[{i}]: позиції немає в замовленні.", code="line_not_found",
                                        status=404, details={"item": item})
                plan.append((line, _qty(item.get("qty"), f"lines[{i}].qty")))
        else:
            others = po.incoming_shipments.exclude(status=IncomingShipment.Status.DELIVERED)
            if tracking:
                others = others.exclude(tracking_number__iexact=tracking)
            if tracking and others.exists():
                warnings.append({"code": "lines_needed",
                                 "message": "У замовлення кілька відправлень, а позицій не передано — відправлення "
                                            "позначено доставленим, на склад нічого не додано. Передайте lines."})
            else:
                plan = [(l, l.qty_ordered - l.qty_received) for l in lines
                        if l.product_id and l.qty_ordered > l.qty_received]
        over = [{"sku": l.external_sku or (l.product.sku if l.product_id else ""), "qty": str(q),
                 "open": str(l.qty_ordered - l.qty_received)} for l, q in plan if l.qty_received + q > l.qty_ordered]
        if over and not data.get("allow_over"):
            raise PurchaseError("Отримано більше, ніж замовлено — схоже на ту саму доставку під іншим delivery_id. "
                                "Якщо це справді так, надішліть allow_over: true.", code="over_receipt", status=409,
                                details={"lines": over})
        unknown = [l for l, q in plan if not l.product_id and q > 0]
        if unknown:
            warnings.append({"code": "sku_not_found",
                             "message": "Позиції без товару Minerva на склад не додаються.",
                             "skus": [l.external_sku for l in unknown]})
        try:
            with transaction.atomic():
                receipt = PurchaseReceipt.objects.create(
                    purchase_order=po, delivery_key=key, delivered_at=delivered_at, tracking_number=tracking,
                    booked=booked, source=source[:32],
                    lines=[{"line": l.pk, "line_id": l.external_line_id,
                            "sku": l.product.sku if l.product_id else l.external_sku, "qty": format(q.normalize(), "f")}
                           for l, q in plan if q > 0])
        except IntegrityError:   # той самий ключ щойно прийшов паралельним запитом
            return _delivery_result(po, po.receipts.get(delivery_key=key), duplicate=True)
        if booked:
            for line, qty in plan:
                if qty > 0 and line.product_id:
                    line.qty_received = line.qty_received + qty
                    line.save()   # сигнал inventory → InventoryTransaction (якщо «Додавати при надходженні»)
            refresh_status(po)
            if po.status == PurchaseOrder.Status.RECEIVED:
                PurchaseOrder.objects.filter(pk=po.pk).update(received_date=timezone.localdate(delivered_at))
        po.delivered_reported_at = delivered_at
        po.save(update_fields=["delivered_reported_at", "updated_at"])
        shipments = po.incoming_shipments.filter(tracking_number__iexact=tracking) if tracking else \
            (po.incoming_shipments.all() if not items else po.incoming_shipments.none())
        shipments.update(status=IncomingShipment.Status.DELIVERED if booked else IncomingShipment.Status.ARRIVED,
                         received_date=timezone.localdate(delivered_at), updated_at=timezone.now())
        po.refresh_from_db()
        return _delivery_result(po, receipt, duplicate=False, warnings=warnings)


def _delivery_result(po, receipt, duplicate, warnings=None) -> dict:
    return {"duplicate": duplicate, "delivery_id": receipt.delivery_key, "booked": receipt.booked,
            "delivered_at": receipt.delivered_at, "lines": receipt.lines, "status": po.status,
            "stock_updated": receipt.booked and InventorySettings.get().add_on_po_receive,
            "warnings": warnings or []}
