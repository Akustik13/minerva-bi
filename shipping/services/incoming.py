"""📥 Вхідні посилки на сторінці «Відправлення» (`Shipment.direction` = in): від постачальника (закупівля PO), JLCPCB.

Лише трекінг: нічого не оформлюється в перевізника, статус оновлює той самий трекінг, що й для наших відправлень
(`tracking_engine`, cron `track_shipments`: UPS / DHL / FedEx за номером). Що в посилці — позиції закупівлі;
прибула — людина оприбутковує закупівлю (сторінка PO).

`carrier_for(number, wanted)` — перевізник для трекінгу: вибраний → за номером (1Z… → UPS) → за замовчуванням.
`create(...)` — нова вхідна посилка; той самий трек-номер уже є — `Duplicate` з наявною.
`open_orders()` — закупівлі, до яких можна прив'язати посилку (ще не отримані й не скасовані).
`on_delivered(shipment)` — трекінг показав «Доставлено» (сигнал shipping/signals.py): за
`InventorySettings.incoming_delivered` — повідомити, позначити закупівлю «прибуло» або оприбуткувати.
"""
from django.utils import timezone

PREFIXES = (('1Z', 'ups'), ('JD', 'dhl'), ('00340', 'dhl'))


class Duplicate(Exception):
    def __init__(self, shipment):
        super().__init__(f'посилка з трек-номером {shipment.tracking_number} вже є (#{shipment.pk})')
        self.shipment = shipment


class NoCarrier(Exception):
    pass


def kind_of(number: str) -> str:
    n = (number or '').strip().upper()
    return next((kind for prefix, kind in PREFIXES if n.startswith(prefix)), '')


def carrier_for(number: str, wanted=''):
    """Carrier: id / назва / тип (ups, dhl, fedex) → за префіксом номера → за замовчуванням → перший активний."""
    from shipping.models import Carrier
    active = Carrier.objects.filter(is_active=True)
    wanted = str(wanted or '').strip()
    if wanted:
        if wanted.isdigit():
            found = active.filter(pk=int(wanted)).first()
            if found:
                return found
        found = (active.filter(name__iexact=wanted).first()
                 or active.filter(carrier_type__iexact=wanted.lower()).order_by('-is_default', 'pk').first()
                 or active.filter(name__icontains=wanted).order_by('-is_default', 'pk').first())
        if found:
            return found
    kind = kind_of(number)
    if kind:
        found = active.filter(carrier_type=kind).order_by('-is_default', 'pk').first()
        if found:
            return found
    found = active.order_by('-is_default', 'pk').first()
    if found is None:
        raise NoCarrier('у Minerva немає жодного активного перевізника — додайте його в «Перевізники»')
    return found


def find(number: str):
    from shipping.models import Shipment
    number = (number or '').strip()
    return Shipment.objects.filter(tracking_number__iexact=number).order_by('-pk').first() if number else None


def open_orders(limit=200):
    from inventory.models import PurchaseOrder
    return (PurchaseOrder.objects.exclude(status__in=(PurchaseOrder.Status.RECEIVED, PurchaseOrder.Status.CANCELLED))
            .select_related('supplier').order_by('-order_date', '-pk')[:limit])


def create(number: str, *, carrier=None, purchase_order=None, sender_company='', sender_country='',
           expected=None, notes='', reference=''):
    """Нова вхідна посилка «📥 До нас» у дорозі. Отримувач — ми (дані відправника з картки перевізника)."""
    from shipping.models import Shipment
    number = (number or '').strip()[:200]
    if not number:
        raise ValueError('потрібен трек-номер')
    existing = find(number)
    if existing is not None:
        raise Duplicate(existing)
    carrier = carrier or carrier_for(number)
    supplier = purchase_order.supplier if purchase_order is not None and purchase_order.supplier_id else None
    sender = (sender_company or (supplier.name if supplier else '')).strip()[:200]
    s = Shipment.objects.create(
        carrier=carrier, direction=Shipment.IN, purchase_order=purchase_order, tracking_number=number,
        status=Shipment.Status.IN_TRANSIT, submitted_at=timezone.now(),
        sender_company=sender, sender_country=(sender_country or (supplier.addr_country if supplier else '')
                                               or '').strip().upper()[:2],
        recipient_company=carrier.sender_company or '', recipient_name=carrier.sender_name or '',
        recipient_country=(carrier.sender_country or '').upper()[:2], recipient_city=carrier.sender_city or '',
        reference=(reference or (purchase_order.code if purchase_order is not None else ''))[:100],
        notes=notes or '',
    )
    if purchase_order is not None and not purchase_order.tracking_number:
        purchase_order.tracking_number = number
        purchase_order.shipping_service = purchase_order.shipping_service or carrier.name
        if expected and not purchase_order.expected_date:
            purchase_order.expected_date = expected
        purchase_order.save(update_fields=['tracking_number', 'shipping_service', 'expected_date', 'updated_at'])
    return s


# ── 📥 Посилку доставлено ─────────────────────────────────────────────────────

LIVE_EXCLUDE = ("cancelled", "error")


def on_delivered(shipment) -> dict:
    """Вхідна посилка стала «Доставлено» → за налаштуванням складу:
    notify — лише повідомлення; arrived — ще й доставка закупівлі «прибуло» (оприбутковує людина); receive — ще й
    оприбуткування всього, що не отримано за закупівлею (`purchases.deliver`, ключ `parcel:<pk>` — один раз). Кілька
    живих посилок на одну закупівлю — закупівля чекає, доки доставлять усі (що в якій — невідомо).
    → {mode, po, result, waiting, error} (mode off / не вхідна — {}). Повідомлення — dashboard.notifications."""
    from inventory.models import InventorySettings, PurchaseOrder
    from shipping.models import Shipment
    if shipment.direction != Shipment.IN:
        return {}
    s = InventorySettings.get()
    mode = s.incoming_delivered
    if mode == InventorySettings.INCOMING_OFF:
        return {}
    po = shipment.purchase_order if shipment.purchase_order_id else None
    out = {"mode": mode, "po": po, "result": None, "waiting": [], "error": ""}
    open_po = po is not None and po.status not in (PurchaseOrder.Status.RECEIVED, PurchaseOrder.Status.CANCELLED)
    if open_po and mode in (InventorySettings.INCOMING_ARRIVED, InventorySettings.INCOMING_RECEIVE):
        out["waiting"] = list(po.parcels.filter(direction=Shipment.IN).exclude(status__in=LIVE_EXCLUDE)
                              .exclude(status="delivered").exclude(pk=shipment.pk))
        if not out["waiting"]:
            from inventory.services import purchases
            try:
                out["result"] = purchases.deliver(
                    po, {"delivery_id": f"parcel:{shipment.pk}", "tracking_number": shipment.tracking_number or "",
                         "delivered_at": shipment.delivered_at or timezone.now()},
                    source="parcel", booked=mode == InventorySettings.INCOMING_RECEIVE)
                po.refresh_from_db()
            except purchases.PurchaseError as e:
                out["error"] = str(e)
    try:
        from dashboard.notifications import notify_incoming_delivered
        notify_incoming_delivered(shipment, out, email=s.incoming_notify_email, telegram=s.incoming_notify_telegram)
    except Exception as e:  # noqa: BLE001 — повідомлення не має ламати трекінг
        import logging
        logging.getLogger(__name__).warning("incoming delivered #%s: %s", shipment.pk, e)
    return out
