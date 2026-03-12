"""
shipping/services/dhl_track.py — DHL Shipment Tracking (Unified API)

Endpoint: GET https://api-eu.dhl.com/track/shipments?trackingNumber=<number>
Auth:     Header  DHL-API-Key: <key>  (from developer.dhl.com → Shipment Tracking – Unified)

Free tier: 250 calls/day, 1 call/5 sec.
"""
import logging
import requests

logger = logging.getLogger(__name__)

DHL_TRACK_URL = "https://api-eu.dhl.com/track/shipments"

STATUS_CODE_DISPLAY = {
    "pre-transit":  ("🕐", "Очікує передачі",   "#607d8b"),
    "transit":      ("🚚", "В дорозі",           "#ff9800"),
    "delivered":    ("✅", "Доставлено",          "#4caf50"),
    "failure":      ("❌", "Невдача доставки",    "#f44336"),
    "unknown":      ("❓", "Невідомо",            "#607d8b"),
    "out-for-delivery": ("📦", "Виїхав кур'єр",  "#2196f3"),
}


def _addr(location_obj) -> str:
    """'{'address': {'addressLocality': 'Köln', 'countryCode': 'DE'}}' → 'Köln, DE'"""
    addr = (location_obj or {}).get("address") or {}
    parts = [addr.get("addressLocality", ""), addr.get("countryCode", "")]
    return ", ".join(p for p in parts if p)


def track(tracking_number: str, api_key: str) -> dict:
    """
    Повертає словник:
      {
        "tracking_number": str,
        "status_code":     str,          # 'transit', 'delivered', ...
        "status_icon":     str,
        "status_label":    str,
        "status_color":    str,
        "status_desc":     str,          # текст від DHL
        "status_ts":       str,          # ISO timestamp
        "status_loc":      str,
        "origin":          str,
        "destination":     str,
        "events":          list[dict],   # [{ts, desc, status_code, location}]
        "error":           str | None,
      }
    """
    try:
        resp = requests.get(
            DHL_TRACK_URL,
            params={"trackingNumber": tracking_number},
            headers={"DHL-API-Key": api_key},
            timeout=15,
        )
    except requests.Timeout:
        return {"error": "Timeout: DHL API не відповідає"}
    except requests.ConnectionError:
        return {"error": "Помилка з'єднання з DHL API"}

    if resp.status_code in (401, 403):
        return {"error": (
            f"HTTP {resp.status_code} — підписка 'Shipment Tracking – Unified' ще не активована DHL.\n"
            "На developer.dhl.com перевір статус API у своєму App: має бути Aktiviert, а не Pending.\n"
            "Зазвичай DHL активує протягом 1–2 робочих днів — просто почекай."
        )}
    if resp.status_code == 404:
        return {"error": f"Посилку {tracking_number!r} не знайдено в системі DHL"}
    if not resp.ok:
        try:
            detail = resp.json().get("detail", resp.text[:300])
        except Exception:
            detail = resp.text[:300]
        return {"error": f"HTTP {resp.status_code}: {detail}"}

    try:
        data = resp.json()
    except Exception:
        return {"error": "Не вдалось розібрати відповідь DHL API (не JSON)"}

    shipments = data.get("shipments") or []
    if not shipments:
        return {"error": f"Посилку {tracking_number!r} не знайдено"}

    s = shipments[0]
    status_obj = s.get("status") or {}
    status_code = status_obj.get("statusCode", "unknown")
    icon, label, color = STATUS_CODE_DISPLAY.get(
        status_code, ("📦", status_code, "#607d8b")
    )

    events = []
    for ev in s.get("events") or []:
        events.append({
            "ts":          ev.get("timestamp", ""),
            "desc":        ev.get("description", ""),
            "status_code": ev.get("statusCode", ""),
            "location":    _addr(ev.get("location")),
        })

    return {
        "tracking_number": s.get("id") or tracking_number,
        "status_code":  status_code,
        "status_icon":  icon,
        "status_label": label,
        "status_color": color,
        "status_desc":  status_obj.get("description", ""),
        "status_ts":    status_obj.get("timestamp", ""),
        "status_loc":   _addr(status_obj.get("location")),
        "origin":       _addr(s.get("origin")),
        "destination":  _addr(s.get("destination")),
        "events":       events,
        "error":        None,
    }
