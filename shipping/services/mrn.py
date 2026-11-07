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


UPS_FORMATS = ("pdf", "jpg", "jpeg", "png", "gif", "bmp", "doc", "docx", "rtf", "txt", "xls", "xlsx")
UPS_FILE_MAX = 10 * 1024 * 1024        # UPS: файл ≤ 10 МБ


def _order_messages(order, user):
    """Листи замовлення (хронологія CRM) лише зі скриньок цієї людини, найновіші першими, без повторів."""
    from crm.models import CustomerTimeline

    events = (CustomerTimeline.objects
              .filter(related_order_id=order.pk, email_message__isnull=False,
                      email_message__account__user=user)
              .select_related("email_message", "email_message__account")
              .order_by("-created_at")[:MAIL_SCAN])
    out, seen = [], set()
    for ev in events:
        if ev.email_message_id not in seen:
            seen.add(ev.email_message_id)
            out.append(ev.email_message)
    return out


def abd_files(order, user, shipper_country: str = "") -> list:
    """Файли з листів ABD замовлення (лише свої скриньки), які UPS приймає:
    [{ref 'msg:<pk>:<i>', name, subject, date, mrn, message}] — для «📑 ABD в UPS»."""
    if not order or not user or not getattr(user, "is_authenticated", False):
        return []
    out = []
    for msg in _order_messages(order, user):
        if not ABD_HINT.search(f"{msg.subject or ''} {(msg.body_text or '')[:2000]}"):
            continue
        for i, att in enumerate(msg.attachments or []):
            if not isinstance(att, dict) or att.get("is_inline"):
                continue
            name = att.get("name") or ""
            if name.rsplit(".", 1)[-1].lower() not in UPS_FORMATS:
                continue
            out.append({"ref": f"msg:{msg.pk}:{i}", "name": name, "subject": msg.subject or "",
                        "date": msg.sent_at or msg.created_at, "message": msg,
                        "mrn": find_in_text(msg.subject, shipper_country)})
    return out


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
