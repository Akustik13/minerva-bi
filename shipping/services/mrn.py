"""
shipping/services/mrn.py — MRN (Movement Reference Number) з Ausfuhrbegleitdokument (ABD).

UPS Shipping API: Shipment.MovementReferenceNumber — рівно 18 латинських літер / цифр,
символи 3–4 — ISO-код країни відправника (26DE760298696748B4).
"""
import re

MRN_LEN = 18
MRN_RE = re.compile(r"(?<![A-Z0-9])(\d{2}[A-Z]{2}[A-Z0-9]{14})(?![A-Z0-9])")
ABD_HINT = re.compile(r"ausfuhrbegleit|\bABD\b|\bMRN\b|export accompanying", re.I)
MAIL_SCAN = 30


def normalize(value: str) -> str:
    return re.sub(r"[\s\-.]", "", (value or "")).upper()


def problem(mrn: str, shipper_country: str = "") -> str:
    """Порожньо — MRN правильний (або не вказаний), інакше — що не так."""
    if not mrn:
        return ""
    if len(mrn) != MRN_LEN:
        return f"MRN має бути рівно {MRN_LEN} знаків (зараз {len(mrn)})."
    if not mrn.isalnum() or not mrn.isascii():
        return "MRN — лише латинські літери й цифри."
    country = (shipper_country or "").upper()[:2]
    if country and mrn[2:4] != country:
        return f"Символи 3–4 MRN ({mrn[2:4]}) мають бути країною відправника ({country})."
    return ""


def find_in_text(text: str, shipper_country: str = "") -> str:
    """Перший MRN у тексті; з країною відправника — першим."""
    found = MRN_RE.findall((text or "").upper())
    country = (shipper_country or "").upper()[:2]
    for m in found:
        if not country or m[2:4] == country:
            return m
    return ""


def find_in_mail(order, user, shipper_country: str = ""):
    """MRN з листа ABD, прив'язаного до замовлення (хронологія CRM), лише зі скриньок цієї людини.
    → {mrn, subject, date, message_pk} або None."""
    if not order or not user or not getattr(user, "is_authenticated", False):
        return None
    from crm.models import CustomerTimeline
    from email_assistant import quotes

    events = (CustomerTimeline.objects
              .filter(related_order_id=order.pk, email_message__isnull=False,
                      email_message__account__user=user)
              .select_related("email_message")
              .order_by("-created_at")[:MAIL_SCAN])
    messages, seen = [], set()
    for ev in events:
        msg = ev.email_message
        if msg.pk not in seen:
            seen.add(msg.pk)
            messages.append(msg)
    messages.sort(key=lambda m: not ABD_HINT.search(m.subject or ""))
    for msg in messages:
        mrn = find_in_text(msg.subject, shipper_country)
        if not mrn and ABD_HINT.search(f"{msg.subject or ''} {msg.body_text or ''}"):
            mrn = find_in_text(quotes.new_text(msg.body_text or ""), shipper_country)
        if mrn and not problem(mrn, shipper_country):
            return {"mrn": mrn, "subject": msg.subject or "", "date": msg.sent_at or msg.created_at,
                    "message_pk": msg.pk}
    return None
