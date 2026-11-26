"""📦 Трек-номер замовлення JLCPCB без ручного вводу: з відповіді JLCPCB API (якщо вона його містить) і з листа
JLCPCB «відправлено» (W… + трек-номер), що прийшов у підключену скриньку Minerva (або переслав колега).

  from_raw(raw)    — (номер, перевізник) з полів відповіді API, чия назва схожа на express / track / logistic /
                     waybill; номер — як трек-номер (UPS 1Z…, DHL, FedEx, інші: 8–35 знаків, щонайменше 6 цифр);
  from_text(text)  — трек-номери в тексті листа: UPS 1Z… будь-де, інші — лише після слова «tracking / waybill …»;
  apply(order, number, carrier, source) — лише коли номера ще немає: номер, перевізник, «Відправлено», примітка;
                     збереження → посилка (signals.sync_jlc_shipment) і трекінг перевізника; закупівля — теж;
  looks_like_jlc_mail(msg) / on_mail(pk) — лист від @jlcpcb.com (або пересланий з ним) з номером замовлення W….
"""
from __future__ import annotations

import logging
import re

from django.utils import timezone

logger = logging.getLogger(__name__)

BATCH_RE = re.compile(r"\bW\d{16}\b")
UPS_RE = re.compile(r"\b1Z[0-9A-Z]{16}\b")
LABEL_RE = re.compile(r"(?:tracking|waybill|track(?:ing)?\s*(?:no|number|#)|awb|shipment\s*number|运单号|物流单号)"
                      r"[^A-Za-z0-9]{0,25}([A-Z0-9]{8,35})", re.I)
KEY_HINTS = ("express", "track", "logistic", "waybill", "shipno", "deliveryno", "trackingno")
CARRIER_KEYS = ("expresscompany", "logisticscompany", "carrier", "shippingmethod", "expressname", "logisticsname")


def _plausible(value: str) -> bool:
    v = (value or "").strip().upper()
    if not (8 <= len(v) <= 35) or not re.fullmatch(r"[A-Z0-9]+", v):
        return False
    if BATCH_RE.fullmatch(v):                # номер замовлення JLC — не трек-номер
        return False
    return sum(ch.isdigit() for ch in v) >= 6


def from_raw(raw) -> tuple | None:
    """(номер, перевізник) з відповіді API — або None."""
    found, carrier = [], ""

    def walk(node, depth=0):
        nonlocal carrier
        if depth > 6:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                key = str(k).lower().replace("_", "")
                if isinstance(v, str):
                    if any(h in key for h in KEY_HINTS) and _plausible(v):
                        found.append(v.strip().upper())
                    elif not carrier and any(c in key for c in CARRIER_KEYS) and v.strip():
                        carrier = v.strip()[:100]
                else:
                    walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node[:50]:
                walk(v, depth + 1)

    walk(raw if isinstance(raw, (dict, list)) else {})
    if not found:
        return None
    ups = [f for f in found if UPS_RE.fullmatch(f)]
    return (ups or found)[0], carrier


def from_text(text: str) -> list:
    """Трек-номери в тексті: UPS 1Z… будь-де; інші — лише одразу після «tracking / waybill / AWB …»."""
    text = text or ""
    out = list(dict.fromkeys(m.group(0).upper() for m in UPS_RE.finditer(text.upper())))
    for m in LABEL_RE.finditer(text):
        v = m.group(1).upper()
        if _plausible(v) and v not in out:
            out.append(v)
    return out


def apply(order, number: str, carrier: str = "", source: str = "") -> bool:
    """Трек-номер у замовлення JLC, якщо його ще немає. True — записано."""
    from jlcpcb.models import JLCOrder
    from jlcpcb.services.api import status_can_advance
    number = (number or "").strip().upper()[:255]
    if not number or order.tracking_number:
        return False
    order.tracking_number = number
    fields = ["tracking_number", "updated_at"]
    if carrier and not order.tracking_carrier:
        order.tracking_carrier = carrier[:100]
        fields.append("tracking_carrier")
    if status_can_advance(order.local_status, JLCOrder.LocalStatus.SHIPPED) and \
            order.local_status != JLCOrder.LocalStatus.SHIPPED:
        order.local_status = JLCOrder.LocalStatus.SHIPPED
        fields.append("local_status")
        if not order.shipped_date:
            order.shipped_date = timezone.localdate()
            fields.append("shipped_date")
    order.notes = ((order.notes + "\n") if order.notes else "") + \
        f"📦 Трек-номер {number} — {source or 'автоматично'} · {timezone.localtime():%d.%m.%Y %H:%M}"
    fields.append("notes")
    order.save(update_fields=fields)          # сигнал → посилка й трекінг перевізника
    logger.info("JLC %s: tracking %s from %s", order.jlc_order_id, number, source)
    try:
        from jlcpcb.services import purchase
        purchase.sync(order)
    except Exception:  # noqa: BLE001
        logger.warning("JLC %s: purchase sync after tracking failed", order.jlc_order_id, exc_info=True)
    return True


def _text_of(msg) -> str:
    body = msg.body_text or ""
    if not body.strip() and getattr(msg, "body_html", ""):
        try:
            from email_assistant.imap_client import html_to_text
            body = html_to_text(msg.body_html)
        except Exception:  # noqa: BLE001
            body = ""
    return f"{msg.subject or ''}\n{body}"


def looks_like_jlc_mail(msg) -> bool:
    sender = (msg.from_email or "").lower()
    if sender.endswith("jlcpcb.com"):
        return True
    text = (msg.body_text or "")[:20000].lower()
    return "@jlcpcb.com" in text and bool(BATCH_RE.search(f"{msg.subject or ''} {msg.body_text or ''}"))


def on_mail(pk) -> list:
    """Лист JLCPCB: номери замовлень W… і трек-номер → у замовлення без трек-номера. Повертає [номер замовлення]."""
    from email_assistant.models import EmailMessage
    from jlcpcb.models import JLCOrder
    msg = EmailMessage.objects.filter(pk=pk).first()
    if msg is None or not looks_like_jlc_mail(msg):
        return []
    text = _text_of(msg)
    batches = list(dict.fromkeys(BATCH_RE.findall(text)))
    numbers = from_text(text)
    if not batches or not numbers:
        return []
    carrier = "UPS" if UPS_RE.fullmatch(numbers[0]) else ""
    done = []
    for order in JLCOrder.objects.filter(jlc_order_number__in=batches):
        if apply(order, numbers[0], carrier, f"лист JLCPCB «{(msg.subject or '')[:60]}»"):
            done.append(order.jlc_order_number)
    return done
