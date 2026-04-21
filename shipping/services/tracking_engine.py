"""
shipping/services/tracking_engine.py — Рушій трекінгу з fallback-ланцюгом.

Використання:
  from shipping.services.tracking_engine import track_with_fallback
  changed, logs = track_with_fallback(shipment)
"""
import time
import logging

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

class _FakeRule:
    """Спрощений об'єкт правила для fallback за замовчуванням."""
    def __init__(self, tracker: str, priority: int = 1):
        self.tracker = tracker
        self.priority = priority
        self.enabled = True
        self.interval_override = 0


def get_rules_for_carrier(carrier_type: str) -> list:
    """
    Повертає список увімкнених TrackingRule для типу перевізника, відсортованих
    за пріоритетом. Якщо правил немає — повертає Jumingo як безпечний fallback.
    """
    try:
        from shipping.models import TrackingRule
        rules = list(
            TrackingRule.objects.filter(
                carrier_type=carrier_type, enabled=True,
                tracking_number_prefix="",      # тільки правила без префіксу
            ).order_by("priority")
        )
        if rules:
            return rules
    except Exception as e:
        logger.warning("TrackingEngine: не вдалось отримати правила: %s", e)

    # Safe default: jumingo якщо доступний
    try:
        from shipping.models import Carrier
        if Carrier.objects.filter(carrier_type="jumingo", is_active=True).exists():
            return [_FakeRule("jumingo", 1)]
    except Exception:
        pass

    return [_FakeRule("jumingo", 1)]


def get_rules_for_shipment(shipment) -> list:
    """
    Визначає правила трекінгу для конкретного відправлення.

    Пріоритет пошуку:
    1. Правила з matching tracking_number_prefix (напр. 1Z → UPS, JD → DHL)
    2. Правила за carrier_type (стандартна логіка)
    3. Jumingo fallback

    Дозволяє системі автоматично вибирати правильний трекер по префіксу
    незалежно від налаштованого типу перевізника.
    """
    tn = (shipment.tracking_number or "").strip()

    # ── Крок 1: prefix-based matching ─────────────────────────────────────────
    if tn:
        try:
            from shipping.models import TrackingRule
            # Беремо всі правила з непорожнім префіксом
            prefix_rules = list(
                TrackingRule.objects.filter(enabled=True)
                                    .exclude(tracking_number_prefix="")
                                    .order_by("priority")
            )
            matched = [
                r for r in prefix_rules
                if tn.upper().startswith(r.tracking_number_prefix.upper())
            ]
            if matched:
                return matched
        except Exception as e:
            logger.warning("TrackingEngine: prefix lookup failed: %s", e)

    # ── Крок 2: carrier_type-based matching ────────────────────────────────────
    if shipment.carrier:
        return get_rules_for_carrier(shipment.carrier.carrier_type)

    return [_FakeRule("jumingo", 1)]


def call_tracker(tracker_name: str, shipment) -> dict:
    """
    Викликає конкретний трекер. НІКОЛИ не кидає виняток.
    Повертає {"error": "..."} при будь-якій невдачі.
    """
    try:
        if tracker_name == "jumingo":
            return _track_jumingo(shipment)
        elif tracker_name == "ups":
            return _track_ups(shipment)
        elif tracker_name == "dhl":
            return _track_dhl(shipment)
        elif tracker_name == "dhl_track":
            return _track_dhl_unified(shipment)
        elif tracker_name == "fedex":
            return _track_fedex(shipment)
        else:
            return {"error": f"Невідомий трекер: {tracker_name!r}"}
    except Exception as e:
        logger.exception("call_tracker(%s) unexpected error: %s", tracker_name, e)
        return {"error": str(e)}


def _track_jumingo(shipment) -> dict:
    if not shipment.carrier_shipment_id:
        return {"error": "carrier_shipment_id відсутній"}
    try:
        from shipping.services.registry import get_service
        svc = get_service(shipment.carrier)
        return svc.track(shipment.carrier_shipment_id) or {"error": "порожня відповідь"}
    except Exception as e:
        return {"error": str(e)}


def _track_ups(shipment) -> dict:
    if not shipment.tracking_number:
        return {"error": "tracking_number відсутній"}
    try:
        from shipping.models import Carrier
        from shipping.ups_client import UPSClient
        carrier = shipment.carrier
        if not carrier or carrier.carrier_type != "ups":
            # шукаємо будь-який активний UPS перевізник
            carrier = Carrier.objects.filter(carrier_type="ups", is_active=True).first()
        if not carrier:
            return {"error": "UPS перевізник не налаштований"}
        client = UPSClient(carrier)
        return client.track(shipment.tracking_number)
    except Exception as e:
        return {"error": str(e)}


def _track_dhl(shipment) -> dict:
    if not shipment.tracking_number:
        return {"error": "tracking_number відсутній"}
    try:
        from shipping.services.dhl import get_tracking
        carrier = shipment.carrier
        if not carrier:
            return {"error": "Carrier відсутній"}
        return get_tracking(carrier, shipment.tracking_number)
    except Exception as e:
        return {"error": str(e)}


def _track_dhl_unified(shipment) -> dict:
    if not shipment.tracking_number:
        return {"error": "tracking_number відсутній"}
    try:
        from shipping.models import Carrier
        from shipping.services.dhl_track import track as dhl_track
        carrier = shipment.carrier
        if not carrier:
            return {"error": "Carrier відсутній"}
        api_key = carrier.track_api_key
        if not api_key:
            # шукаємо будь-який DHL з track_api_key
            c = Carrier.objects.filter(carrier_type="dhl", is_active=True,
                                       track_api_key__gt="").first()
            if not c:
                return {"error": "DHL Tracking Unified API ключ не налаштований"}
            api_key = c.track_api_key
        return dhl_track(shipment.tracking_number, api_key)
    except Exception as e:
        return {"error": str(e)}


def _track_fedex(shipment) -> dict:
    if not shipment.tracking_number:
        return {"error": "tracking_number відсутній"}
    try:
        from shipping.models import Carrier
        from shipping.fedex_client import FedExClient
        carrier = shipment.carrier
        if not carrier or carrier.carrier_type != "fedex":
            carrier = Carrier.objects.filter(carrier_type="fedex", is_active=True).first()
        if not carrier:
            return {"error": "FedEx перевізник не налаштований"}
        client = FedExClient(carrier)
        return client.track(shipment.tracking_number)
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Нормалізація у формат Jumingo (для _apply_tracking_update)
# ─────────────────────────────────────────────────────────────────────────────

def normalize_to_jumingo_format(tracker_name: str, raw: dict) -> dict:
    """
    Конвертує відповідь UPS/FedEx/DHL у формат сумісний з Jumingo,
    щоб _apply_tracking_update() могла обробити без змін.
    """
    if tracker_name == "jumingo":
        return raw  # вже в правильному форматі

    if tracker_name in ("dhl", "dhl_track"):
        return _normalize_dhl(tracker_name, raw)
    elif tracker_name == "ups":
        return _normalize_ups(raw)
    elif tracker_name == "fedex":
        return _normalize_fedex(raw)

    return raw


def _dhl_status_to_class(status_code: str) -> str:
    """DHL status_code → Jumingo progress.class"""
    mapping = {
        "delivered":        "completed",
        "transit":          "in_transit",
        "out-for-delivery": "in_transit",
        "pre-transit":      "in_system",
        "failure":          "exception",
        "unknown":          "in_transit",
    }
    return mapping.get(status_code, "in_transit")


def _normalize_dhl(tracker_name: str, raw: dict) -> dict:
    """
    dhl.get_tracking() → {status, description, estimated_delivery, events, error}
    dhl_track.track()  → {status_code, status_label, estimated_delivery(?), events, error}
    """
    if tracker_name == "dhl_track":
        status_code = raw.get("status_code", "unknown")
        progress_class = _dhl_status_to_class(status_code)
        status_label = raw.get("status_label", raw.get("status_desc", ""))
    else:
        # dhl.get_tracking returns "status" = delivered/in_transit/etc
        status_code = raw.get("status", "unknown")
        progress_class = _dhl_status_to_class(status_code)
        status_label = raw.get("description", "")

    est_delivery = raw.get("estimated_delivery", "")

    return {
        "tracking": {
            "progress": {
                "class": progress_class,
                "label": status_label,
                "status_label": status_label,
            },
            "data": {
                "tracking_number": raw.get("tracking_number", ""),
                "estimated_delivery": est_delivery,
                "estimated_delivery_to": est_delivery,
            },
            "dates": {
                "eta_to": est_delivery,
            },
        },
    }


def _ups_status_to_class(status_type: str, delivered: bool) -> str:
    """UPS status type → Jumingo progress.class"""
    if delivered or status_type == "D":
        return "completed"
    if status_type in ("I", "O"):
        return "in_transit"
    if status_type in ("M", "P", "X"):
        return "in_system"
    return "in_transit"


def _normalize_ups(raw: dict) -> dict:
    status_type = raw.get("status", "")
    delivered   = bool(raw.get("delivered", False))
    progress_class = _ups_status_to_class(status_type, delivered)
    status_label = raw.get("status_description", "")
    est_delivery = raw.get("estimated_delivery", "")
    # UPS estimated_delivery format: "20260417" → "2026-04-17"
    if est_delivery and len(est_delivery) == 8 and est_delivery.isdigit():
        est_delivery = f"{est_delivery[:4]}-{est_delivery[4:6]}-{est_delivery[6:]}"

    return {
        "tracking": {
            "progress": {
                "class": progress_class,
                "label": status_label,
                "status_label": status_label,
            },
            "data": {
                "tracking_number": raw.get("tracking_number", ""),
                "estimated_delivery": est_delivery,
                "estimated_delivery_to": est_delivery,
                "actual_delivery": raw.get("actual_delivery", ""),
            },
            "dates": {
                "eta_to": est_delivery,
            },
        },
    }


def _fedex_status_to_class(status: str, delivered: bool) -> str:
    """FedEx status code → Jumingo progress.class"""
    if delivered or status == "DL":
        return "completed"
    if status in ("IT", "OD", "DP"):
        return "in_transit"
    return "in_system"


def _normalize_fedex(raw: dict) -> dict:
    status      = raw.get("status", "")
    delivered   = bool(raw.get("delivered", False))
    progress_class = _fedex_status_to_class(status, delivered)
    status_label = raw.get("status_description", "")
    est_delivery = raw.get("estimated_delivery", "")

    return {
        "tracking": {
            "progress": {
                "class": progress_class,
                "label": status_label,
                "status_label": status_label,
            },
            "data": {
                "tracking_number": raw.get("tracking_number", ""),
                "estimated_delivery": est_delivery,
                "estimated_delivery_to": est_delivery,
            },
            "dates": {
                "eta_to": est_delivery,
            },
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Логування спроб
# ─────────────────────────────────────────────────────────────────────────────

def _log_attempt(shipment, tracker_name: str, raw: dict, duration_ms: int,
                 dry_run: bool = False):
    """Записує TrackingAttemptLog. Обрізає старі записи. НІКОЛИ не кидає."""
    if dry_run:
        return
    try:
        from shipping.models import TrackingAttemptLog, ShippingSettings
        error = raw.get("error", "") or ""
        success = not bool(error)

        # Витягуємо статус з сирої відповіді (різні ключі в різних трекерів)
        status_found = (
            raw.get("status_code") or
            raw.get("status") or
            (raw.get("tracking", {}).get("progress", {}).get("class", "")) or
            ""
        )

        TrackingAttemptLog.objects.create(
            shipment=shipment,
            tracker=tracker_name,
            success=success,
            status_found=str(status_found)[:100],
            error=error[:1000],
            duration_ms=duration_ms,
        )

        # Обрізання старих записів
        try:
            cfg = ShippingSettings.get()
            max_entries = cfg.tracking_log_max_entries or 500
            excess_ids = list(
                TrackingAttemptLog.objects
                .order_by("-created_at")
                .values_list("id", flat=True)[max_entries:]
            )
            if excess_ids:
                TrackingAttemptLog.objects.filter(id__in=excess_ids).delete()
        except Exception:
            pass

    except Exception as e:
        logger.debug("_log_attempt failed (non-critical): %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Головна функція
# ─────────────────────────────────────────────────────────────────────────────

def track_with_fallback(shipment, dry_run: bool = False) -> tuple:
    """
    Намагається отримати трекінг для відправлення по всіх правилах у ланцюгу.
    Повертає (changed: bool, log_entries: list[dict]).

    Обробка:
    - Перебирає правила за пріоритетом
    - При помилці → пробує наступний трекер
    - Якщо всі провалились → повертає (False, logs)
    - НІКОЛИ не кидає виняток
    """
    from shipping.admin import _apply_tracking_update

    if not shipment.carrier and not shipment.tracking_number:
        return False, [{"tracker": "—", "error": "carrier і tracking_number відсутні", "success": False}]

    rules = get_rules_for_shipment(shipment)
    log_entries = []

    for rule in rules:
        t0 = time.monotonic()
        raw = call_tracker(rule.tracker, shipment)
        duration_ms = int((time.monotonic() - t0) * 1000)

        entry = {
            "tracker":     rule.tracker,
            "duration_ms": duration_ms,
            "success":     not bool(raw.get("error")),
            "error":       raw.get("error", ""),
            "status":      raw.get("status_code") or raw.get("status", ""),
        }
        log_entries.append(entry)

        _log_attempt(shipment, rule.tracker, raw, duration_ms, dry_run)

        if raw.get("error"):
            logger.debug(
                "track_with_fallback: tracker=%s failed (%s), trying next",
                rule.tracker, raw["error"]
            )
            continue

        # Успіх — нормалізуємо та застосовуємо
        normalized = normalize_to_jumingo_format(rule.tracker, raw)

        if dry_run:
            entry["normalized_class"] = (
                normalized.get("tracking", {})
                          .get("progress", {})
                          .get("class", "?")
            )
            return True, log_entries

        try:
            changed = _apply_tracking_update(shipment, normalized)
            return changed, log_entries
        except Exception as e:
            logger.exception("track_with_fallback: _apply_tracking_update error: %s", e)
            entry["error"] = f"apply error: {e}"
            entry["success"] = False

    # Всі трекери провалились
    return False, log_entries
