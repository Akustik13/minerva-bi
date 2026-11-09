"""
shipping/services/import_tracking.py
Утиліта: знайти або створити Shipment для замовлення з трекінг-номером.
Використовується при імпорті трекінгу з DigiKey та інших маркетплейсів.
"""
from __future__ import annotations

import logging
from typing import Tuple

logger = logging.getLogger(__name__)


def _detect_carrier_type(tracking: str, carrier_name: str = "") -> Tuple[str, str]:
    """Повертає (carrier_type, display_name) на основі трекінг-номера."""
    from shipping.models import Carrier

    t = (tracking or "").upper().strip()
    n = (carrier_name or "").upper()

    if t.startswith("1Z") and len(t) >= 18:
        return Carrier.CarrierType.UPS, "UPS"
    if "UPS" in n:
        return Carrier.CarrierType.UPS, "UPS"

    if (
        t.startswith(("JD", "JJD", "00340", "00345", "00380"))
        or (t.isdigit() and len(t) in (20, 22))
        or "DHL" in n
    ):
        return Carrier.CarrierType.DHL, "DHL"

    if "FEDEX" in n or (len(t) in (12, 15, 20) and t.isdigit()):
        return Carrier.CarrierType.FEDEX, "FedEx"

    display = carrier_name or "Інший перевізник"
    return Carrier.CarrierType.OTHER, display


def tracking_numbers_in(value: str) -> list:
    """Окремі трек-номери з поля: «1Z…, 1Z…», рядками, «Part1: JD… Part2: JD…» (DigiKey)."""
    import re
    s = re.sub(r"\bPart\s*\d+\s*:", " ", value or "", flags=re.IGNORECASE)
    return [p for p in re.split(r"[,;\s]+", s) if p]


def ensure_shipment_for_order(
    order,
    tracking_number: str,
    carrier_name: str = "",
) -> Tuple[object, bool]:
    """
    Знаходить або створює Shipment для замовлення.

    Повертає (shipment, created: bool).
    Відправлення з цим трек-номером (або без трек-номера) вже є — дописує номер, якщо його не було, і підвищує
    статус до IN_TRANSIT; номер іншого відправлення не змінює — тоді створює нове.
    """
    from shipping.models import Carrier, Shipment

    tracking_number = (tracking_number or "").strip()
    if not tracking_number:
        raise ValueError("tracking_number обов'язковий")

    carrier_type, display_name = _detect_carrier_type(tracking_number, carrier_name)

    # Знаходимо перевізника — спочатку за типом, потім за назвою, потім створюємо
    carrier = (
        Carrier.objects.filter(carrier_type=carrier_type, is_active=True).first()
        or Carrier.objects.filter(name__iexact=display_name, is_active=True).first()
    )
    if not carrier:
        carrier = Carrier.objects.create(
            name=display_name,
            carrier_type=carrier_type,
            is_active=True,
        )
        logger.info("import_tracking: created Carrier '%s' type=%s", display_name, carrier_type)

    # Відправлення з цим трек-номером уже є — воно. Інакше — відправлення без трек-номера (чернетка).
    # Трек-номер ІНШОГО відправлення не перезаписується ніколи: замовлення частинами має кілька посилок, і
    # раніше «📦 Відстежувати» ставило номер першої посилки в найновішу (дві посилки з одним номером).
    numbers = {t.upper() for t in tracking_numbers_in(tracking_number)} or {tracking_number.upper()}
    live = list(Shipment.objects.filter(order=order).exclude(status=Shipment.Status.CANCELLED).order_by("-pk"))
    existing = next((s for s in live if (s.tracking_number or "").strip().upper() in numbers), None)
    if existing is None:
        existing = next((s for s in live if not (s.tracking_number or "").strip()), None)
    if existing:
        changed = False
        if not (existing.tracking_number or "").strip():
            existing.tracking_number = tracking_number
            changed = True
        if existing.status in (
            Shipment.Status.DRAFT,
            Shipment.Status.SUBMITTED,
            Shipment.Status.LABEL_READY,
        ):
            existing.status = Shipment.Status.IN_TRANSIT
            changed = True
        if changed:
            existing.save()
        return existing, False

    # Створюємо новий Shipment
    shipment = Shipment(
        order=order,
        carrier=carrier,
        tracking_number=tracking_number,
        status=Shipment.Status.IN_TRANSIT,
        carrier_service=carrier_name or display_name,
    )
    try:
        shipment.copy_from_order()
    except Exception:
        pass  # якщо адресу не вдалось скопіювати — не критично
    shipment.save()
    logger.info(
        "import_tracking: created Shipment #%s for order %s tracking=%s",
        shipment.pk, order.order_number, tracking_number,
    )
    return shipment, True


def ensure_shipment_for_jlc_order(
    jlc_order,
    tracking_number: str,
    carrier_name: str = "",
) -> Tuple[object, bool]:
    """
    Знаходить або створює Shipment для JLCPCB замовлення (incoming shipment).
    Повертає (shipment, created: bool).
    """
    from shipping.models import Carrier, Shipment

    tracking_number = (tracking_number or "").strip()
    if not tracking_number:
        raise ValueError("tracking_number обов'язковий")

    carrier_type, display_name = _detect_carrier_type(tracking_number, carrier_name)

    carrier = (
        Carrier.objects.filter(carrier_type=carrier_type, is_active=True).first()
        or Carrier.objects.filter(name__iexact=display_name, is_active=True).first()
    )
    if not carrier:
        carrier = Carrier.objects.create(
            name=display_name,
            carrier_type=carrier_type,
            is_active=True,
        )
        logger.info("import_tracking: created Carrier '%s' type=%s", display_name, carrier_type)

    existing = Shipment.objects.filter(jlc_order=jlc_order).first()
    if existing:
        changed = False
        if tracking_number and existing.tracking_number != tracking_number:
            existing.tracking_number = tracking_number
            changed = True
        if existing.status in (
            Shipment.Status.DRAFT,
            Shipment.Status.SUBMITTED,
            Shipment.Status.LABEL_READY,
        ):
            existing.status = Shipment.Status.IN_TRANSIT
            changed = True
        if changed:
            existing.save()
        return existing, False

    shipment = Shipment.objects.create(
        jlc_order=jlc_order,
        order=None,
        carrier=carrier,
        tracking_number=tracking_number,
        status=Shipment.Status.IN_TRANSIT,
        carrier_service=carrier_name or display_name,
        reference=jlc_order.jlc_order_number or jlc_order.jlc_order_id,
        description="PCB/PCBA from JLCPCB",
    )
    logger.info(
        "import_tracking: created Shipment #%s for JLCOrder %s tracking=%s",
        shipment.pk, jlc_order.jlc_order_number, tracking_number,
    )
    return shipment, True
