"""
inventory/services/incoming_tracking.py
Refresh tracking for IncomingShipment — reuses shipping app DHL tracker.
"""
import logging
from django.utils import timezone

logger = logging.getLogger(__name__)

# DHL status code → IncomingShipment.Status
_DHL_STATUS_MAP = {
    'pre-transit':      'pending',
    'transit':          'in_transit',
    'out-for-delivery': 'in_transit',
    'delivered':        'delivered',
    'failure':          'exception',
    'unknown':          'in_transit',
}

_STATUS_UPGRADE = {
    'pending': 0, 'in_transit': 1, 'customs': 2,
    'arrived': 3, 'delivered': 4, 'exception': 0,
}


def _get_dhl_api_key(shipment) -> str | None:
    """Return DHL track_api_key: from linked Carrier, or first active DHL carrier."""
    if shipment.carrier_id:
        c = shipment.carrier
        if c.carrier_type == 'dhl' and c.track_api_key:
            return c.track_api_key
    try:
        from shipping.models import Carrier
        c = Carrier.objects.filter(
            carrier_type='dhl', is_active=True, track_api_key__gt=''
        ).first()
        return c.track_api_key if c else None
    except Exception:
        return None


def _refresh_dhl(shipment, api_key: str) -> tuple[bool, str]:
    from shipping.services.dhl_track import track
    result = track(shipment.tracking_number, api_key)
    if result.get('error'):
        return False, f'DHL: {result["error"]}'

    changed = False
    raw_status = result.get('status_code', '')
    new_status  = _DHL_STATUS_MAP.get(raw_status, 'in_transit')

    # only upgrade status (don't go backwards)
    if (_STATUS_UPGRADE.get(new_status, 0) >
            _STATUS_UPGRADE.get(shipment.status, 0)):
        shipment.status = new_status
        changed = True

    new_label = result.get('status_label', '')
    if new_label and shipment.carrier_status_label != new_label:
        shipment.carrier_status_label = new_label
        changed = True

    new_events = result.get('events') or []
    if new_events:
        shipment.tracking_events = new_events
        changed = True

    origin = result.get('origin', '')
    if origin and not shipment.origin_city:
        shipment.origin_city = origin
        changed = True

    return changed, f'DHL: {new_label or new_status}'


def refresh_tracking(shipment) -> tuple[bool, str]:
    """
    Refresh one IncomingShipment.
    Returns (changed: bool, message: str).
    Saves the shipment if changed.
    """
    if not shipment.tracking_number:
        return False, 'Номер відстеження не вказано'

    # Detect carrier type
    c_type = ''
    if shipment.carrier_id:
        c_type = shipment.carrier.carrier_type

    # Fallback: detect from number prefix
    if not c_type:
        tn = shipment.tracking_number.upper()
        if tn.startswith(('JD', '00340')):
            c_type = 'dhl'
        elif tn.startswith('1Z'):
            c_type = 'ups'
        elif tn.startswith(('61', '62', '63', '64', '65', '66', '67', '68', '69', '70', '74', '75', '76', '77', '78')):
            c_type = 'fedex'

    try:
        if c_type == 'dhl':
            api_key = _get_dhl_api_key(shipment)
            if not api_key:
                return False, 'DHL: track_api_key не налаштовано в Carrier'
            changed, msg = _refresh_dhl(shipment, api_key)
        else:
            return False, f'Перевізник "{c_type or shipment.carrier_name}" — API не налаштовано'

        shipment.last_tracked_at = timezone.now()
        update_fields = ['status', 'carrier_status_label', 'tracking_events',
                         'origin_city', 'last_tracked_at']
        shipment.save(update_fields=update_fields)
        return changed, msg

    except Exception as exc:
        logger.exception('IncomingShipment #%s tracking error', shipment.pk)
        return False, f'Помилка: {exc}'
