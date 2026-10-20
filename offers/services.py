"""Angebote: створення, позиції, перерахунок, версії, статуси, замовлення з прийнятої пропозиції.

Логіка лише тут (адмінка, пошта, проєкти, продажі викликають ці функції).
"""
import logging
import re
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from . import pricing
from .models import Offer, OfferLine, OfferRevision, OfferSettings

logger = logging.getLogger(__name__)

ORDER_SOURCE = "angebot"
# «500 pcs», «500 Stk.», «x 500», «500 шт» біля артикула
_QTY_RE = re.compile(r"(?:(?<![\w./-])(\d{1,7}(?:[.,]\d+)?)\s*(?:pcs|pc|stk\.?|stück|st\.|шт\.?|units?|x|×)(?!\w))"
                     r"|(?:(?:x|×|qty:?|menge:?)\s*(\d{1,7})(?!\w))", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+-]{2,}")


class OfferError(Exception):
    pass


# ── Створення ────────────────────────────────────────────────────────────────

def customer_address(customer) -> str:
    if customer is None:
        return ""
    parts = [customer.company or "", customer.name or "", customer.addr_street or "",
             " ".join(p for p in (customer.addr_zip, customer.addr_city) if p), customer.country or ""]
    seen, out = set(), []
    for p in parts:
        p = (p or "").strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return "\n".join(out)


def defaults(customer=None) -> dict:
    s = OfferSettings.get()
    return {
        "currency": s.default_currency, "vat_rate": s.default_vat_rate, "language": s.default_language,
        "valid_until": timezone.localdate() + timedelta(days=s.validity_days),
        "intro_text": s.intro_text, "payment_terms": s.payment_terms, "delivery_terms": s.delivery_terms,
        "outro_text": s.outro_text,
        "contact_name": getattr(customer, "name", "") or "", "contact_email": getattr(customer, "email", "") or "",
        "billing_address": customer_address(customer),
    }


def new_offer(*, customer=None, user=None, **fields) -> Offer:
    data = {**defaults(customer), **{k: v for k, v in fields.items() if v is not None}}
    return Offer.objects.create(customer=customer, owner=data.pop("owner", None) or user, created_by=user, **data)


def add_line(offer, *, product=None, qty=1, sku="", description="", unit_price=None, discount_percent=0,
             price_source=None, price_note="", lead_time="", unit="Stk", recalc=True) -> OfferLine:
    """Позиція: без ціни — ціна з каталогу Minerva (pricing.quote_price)."""
    if offer.is_locked:
        raise OfferError("Надіслану пропозицію не змінюють — створіть нову версію.")
    line = OfferLine(offer=offer, product=product, qty=Decimal(str(qty)), unit=unit,
                     discount_percent=Decimal(str(discount_percent or 0)), lead_time=lead_time,
                     position=(offer.lines.count() + 1))
    fill_line(line, sku=sku, description=description, unit_price=unit_price, price_source=price_source,
              price_note=price_note)
    line.save()
    if recalc:
        recalculate(offer)
    return line


def fill_line(line, *, sku="", description="", unit_price=None, price_source=None, price_note=""):
    """Доповнити позицію з товару: артикул, опис, ціна з каталогу (якщо ціни не задано вручну)."""
    p = line.product
    line.sku = sku or line.sku or (p.sku if p else "")
    line.description = description or line.description or (p.name_export or p.name if p else "") or line.sku
    if p and not line.lead_time and getattr(p, "lead_time_days", None):
        line.lead_time = f"{p.lead_time_days} Tage" if line.offer.language == "de" else f"{p.lead_time_days} days"
    if unit_price is not None:
        line.unit_price = Decimal(str(unit_price))
        line.price_source = price_source or OfferLine.MANUAL
        line.price_note = price_note
    elif line.unit_price is None and p is not None:
        q = pricing.quote_price(p, line.qty, line.offer.currency)
        line.unit_price, line.price_source, line.price_note = q.unit_price, q.source, q.note
    elif line.unit_price is not None and not line.price_note:
        line.price_source, line.price_note = OfferLine.MANUAL, "Введено вручну"
    line.line_total = pricing.line_total(line.qty, line.unit_price, line.discount_percent)


def recalculate(offer) -> Offer:
    lines = list(offer.lines.all())
    for i, line in enumerate(lines, 1):
        total = pricing.line_total(line.qty, line.unit_price, line.discount_percent)
        if line.line_total != total or line.position != i:
            line.line_total, line.position = total, i
            line.save(update_fields=["line_total", "position"])
    t = pricing.totals([l.line_total for l in lines], offer.shipping_cost, offer.vat_rate)
    offer.net_total, offer.vat_total, offer.gross_total = t["net"], t["vat"], t["gross"]
    Offer.objects.filter(pk=offer.pk).update(net_total=t["net"], vat_total=t["vat"], gross_total=t["gross"],
                                             updated_at=timezone.now())
    return offer


def warnings(offer) -> list:
    out = []
    missing = [l.position for l in offer.lines.all() if l.unit_price is None]
    if missing:
        out.append(f"Позиції без ціни (на запит): {', '.join(map(str, missing))}")
    if not offer.lines.exists():
        out.append("Немає позицій")
    if not (offer.contact_email or getattr(offer.customer, "email", "")):
        out.append("Немає email клієнта")
    return out


# ── Версії й документ ────────────────────────────────────────────────────────

def _s(value) -> str:
    return format(Decimal(str(value)), "f") if value is not None else ""


def snapshot(offer) -> dict:
    """Незмінний знімок версії: усе, що потрапляє в документ (Decimal — рядками)."""
    return {
        "number": offer.number, "revision": offer.revision, "display_number": offer.display_number,
        "date": timezone.localdate().isoformat(),
        "valid_until": offer.valid_until.isoformat() if offer.valid_until else "",
        "language": offer.language, "currency": offer.currency,
        "customer": {"id": offer.customer_id, "name": getattr(offer.customer, "name", ""),
                     "company": getattr(offer.customer, "company", "")},
        "contact_name": offer.contact_name, "contact_email": offer.contact_email,
        "billing_address": offer.billing_address,
        "project": {"id": offer.project_id, "code": getattr(offer.project, "code", ""),
                    "name": getattr(offer.project, "name", "")} if offer.project_id else None,
        "intro_text": offer.intro_text, "payment_terms": offer.payment_terms,
        "delivery_terms": offer.delivery_terms, "outro_text": offer.outro_text,
        "lines": [{"position": l.position, "sku": l.sku, "description": l.description, "qty": _s(l.qty),
                   "unit": l.unit, "unit_price": _s(l.unit_price), "discount_percent": _s(l.discount_percent),
                   "line_total": _s(l.line_total), "lead_time": l.lead_time, "price_source": l.price_source,
                   "price_note": l.price_note, "product_id": l.product_id} for l in offer.lines.all()],
        "shipping_cost": _s(offer.shipping_cost), "vat_rate": _s(offer.vat_rate),
        "net_total": _s(offer.net_total), "vat_total": _s(offer.vat_total), "gross_total": _s(offer.gross_total),
    }


def current_revision(offer):
    return offer.revisions.filter(revision=offer.revision).first()


def generate_document(offer, user=None) -> OfferRevision:
    """Знімок + DOCX/PDF поточної версії. Надіслану версію не перезаписує (повертає як є)."""
    rev = current_revision(offer)
    if rev is not None and (rev.sent_at or offer.is_locked):
        return rev
    recalculate(offer)
    offer.refresh_from_db()
    if rev is None:
        rev = OfferRevision(offer=offer, revision=offer.revision, created_by=user)
    rev.snapshot = snapshot(offer)
    rev.save()
    from .document import render_files
    render_files(rev)
    return rev


@transaction.atomic
def new_version(offer, user=None) -> Offer:
    """Зміни після відправки — лише в новій версії: попередня лишається незмінною."""
    if offer.status in (Offer.ACCEPTED,):
        raise OfferError("Прийняту пропозицію не змінюють.")
    offer.revision += 1
    offer.status, offer.sent_at, offer.decided_at = Offer.DRAFT, None, None
    offer.save(update_fields=["revision", "status", "sent_at", "decided_at", "updated_at"])
    return offer


def set_status(offer, status, user=None) -> Offer:
    allowed = {
        Offer.REVIEW: (Offer.DRAFT, Offer.APPROVED),
        Offer.APPROVED: (Offer.DRAFT, Offer.REVIEW),
        Offer.DRAFT: (Offer.REVIEW, Offer.APPROVED),
        Offer.REJECTED: (Offer.SENT, Offer.EXPIRED),
        Offer.CANCELLED: (Offer.DRAFT, Offer.REVIEW, Offer.APPROVED, Offer.SENT, Offer.EXPIRED),
    }
    if offer.status not in allowed.get(status, ()):
        raise OfferError(f"Перехід «{offer.get_status_display()}» → «{dict(Offer.STATUS_CHOICES)[status]}» неможливий.")
    offer.status = status
    if status == Offer.REJECTED:
        offer.decided_at = timezone.now()
    offer.save(update_fields=["status", "decided_at", "updated_at"])
    return offer


def mark_sent(revision, message_id="") -> OfferRevision:
    now = timezone.now()
    if not revision.sent_at:
        revision.sent_at, revision.sent_message_id = now, (message_id or "")[:500]
        revision.save(update_fields=["sent_at", "sent_message_id"])
    offer = revision.offer
    if offer.revision == revision.revision and offer.status in (Offer.DRAFT, Offer.REVIEW, Offer.APPROVED):
        offer.status, offer.sent_at = Offer.SENT, now
        offer.save(update_fields=["status", "sent_at", "updated_at"])
    return revision


def expire_overdue() -> int:
    return Offer.objects.filter(status=Offer.SENT, valid_until__lt=timezone.localdate()) \
        .update(status=Offer.EXPIRED, updated_at=timezone.now())


@transaction.atomic
def accept(offer, user=None):
    """Клієнт прийняв → замовлення Minerva з позицій каталогу. Повертає (замовлення, пропущені позиції)."""
    from sales.models import SalesOrder, SalesOrderLine, SalesSource
    if offer.order_id:
        return offer.order, []
    if offer.status not in (Offer.SENT, Offer.APPROVED, Offer.EXPIRED):
        raise OfferError("Прийняти можна надіслану (або погоджену) пропозицію.")
    SalesSource.objects.get_or_create(slug=ORDER_SOURCE, defaults={"name": "Angebot"})
    c = offer.customer
    order = SalesOrder.objects.create(
        source=ORDER_SOURCE, document_type="SALE", order_number=offer.display_number,
        order_date=timezone.localdate(), status="received",
        client=(getattr(c, "company", "") or getattr(c, "name", "") or offer.contact_name)[:255],
        contact_name=offer.contact_name[:255], email=offer.contact_email or getattr(c, "email", ""),
        phone=getattr(c, "phone", "") or "", customer_key=getattr(c, "external_key", "") or "",
        addr_street=getattr(c, "addr_street", "") or "", addr_city=getattr(c, "addr_city", "") or "",
        addr_zip=getattr(c, "addr_zip", "") or "", addr_country=getattr(c, "country", "") or "",
    )
    skipped = []
    for l in offer.lines.all():
        if l.product_id is None:
            skipped.append(l.position)
            continue
        SalesOrderLine.objects.create(order=order, product_id=l.product_id, sku_raw=l.sku[:128], qty=l.qty,
                                      unit_price=(l.line_total / l.qty).quantize(pricing.Q4) if l.qty else l.unit_price,
                                      total_price=l.line_total, currency=offer.currency)
    offer.order, offer.status, offer.decided_at = order, Offer.ACCEPTED, timezone.now()
    offer.save(update_fields=["order", "status", "decided_at", "updated_at"])
    rev = current_revision(offer)
    if rev is not None:
        _copy_to_order_folder(rev, order)
    return order, skipped


def _copy_to_order_folder(rev, order):
    """Документ пропозиції — до документів замовлення (media/orders/{source}/{order}/)."""
    import shutil
    from pathlib import Path

    from django.conf import settings
    dest = Path(settings.MEDIA_ROOT) / "orders" / order.source / order.order_number
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for f in (rev.pdf_file, rev.docx_file):
            if f:
                shutil.copyfile(f.path, dest / Path(f.name).name)
    except OSError as e:
        logger.warning("offer %s: copy to order folder failed: %s", rev.offer.number, e)


# ── З листа, проєкту, запиту магазину ────────────────────────────────────────

def detect_items(text) -> list:
    """Артикули каталогу в тексті листа: [(товар, к-сть|None)] — кількість з того самого рядка."""
    from django.db.models import Q
    from django.db.models.functions import Lower

    from inventory.models import Product
    found, order = {}, []
    lines = (text or "").splitlines()
    tokens = {t.strip(".,;:()[]").lower() for line in lines for t in _TOKEN_RE.findall(line)}
    tokens = {t for t in tokens if any(ch.isdigit() for ch in t) and len(t) >= 4}
    if not tokens:
        return []
    products = list(Product.objects.annotate(ls=Lower("sku"), lss=Lower("sku_short"))
                    .filter(Q(ls__in=tokens) | Q(lss__in=tokens), is_active=True))
    by_token = {}
    for p in products:
        by_token.setdefault(p.sku.lower(), p)
        if p.sku_short:
            by_token.setdefault(p.sku_short.lower(), p)
    for line in lines:
        qtys = []   # (початок, кінець, к-сть) кількостей у рядку
        for m in _QTY_RE.finditer(line):
            raw = (m.group(1) or m.group(2) or "").replace(",", ".")
            try:
                if Decimal(raw) > 0:
                    qtys.append((m.start(), m.end(), Decimal(raw)))
            except ArithmeticError:
                pass
        for tm in _TOKEN_RE.finditer(line):
            p = by_token.get(tm.group(0).strip(".,;:()[]").lower())
            if p is None or p.pk in found:
                continue
            # кількість, найближча до артикула («500 pcs ANT-100 and CAB-7 x 20» → 500 і 20)
            near = [(min(abs(tm.start() - e), abs(s - tm.end())), q) for s, e, q in qtys
                    if e <= tm.start() or s >= tm.end()]
            found[p.pk] = (p, min(near)[1] if near else None)
            order.append(p.pk)
    return [found[pk] for pk in order]


@transaction.atomic
def create_from_thread(thread, user=None) -> Offer:
    """«📝 Angebot з листа»: клієнт і проєкт гілки, вихідний лист, позиції з артикулів у тексті."""
    from email_assistant.crm_link import is_own, own_addresses
    msgs = list(thread.messages.filter(is_deleted=False).order_by("-sent_at"))
    own = own_addresses()
    source = next((m for m in msgs if not is_own(m.from_email, own)), msgs[0] if msgs else None)
    project = None
    try:
        from projects.models import ProjectLink
        link = (ProjectLink.objects.filter(thread=thread, status=ProjectLink.CONFIRMED)
                .select_related("project").order_by("-created_at").first())
        project = link.project if link else None
    except ImportError:
        pass
    customer = thread.crm_customer or (project.customer if project else None)
    offer = new_offer(customer=customer, user=user, project=project, source_message=source)
    if source is not None:
        if not offer.contact_email and not is_own(source.from_email, own):
            offer.contact_email = source.from_email
            offer.contact_name = offer.contact_name or source.from_name
            offer.save(update_fields=["contact_email", "contact_name"])
        for product, qty in detect_items(f"{source.subject}\n{source.body_text}"):
            line = add_line(offer, product=product, qty=qty or 1, recalc=False)
            if qty is None:
                line.price_note = (line.price_note + " · к-сть не знайдено в листі — перевірте")[:255]
                line.save(update_fields=["price_note"])
    recalculate(offer)
    return offer


@transaction.atomic
def create_from_quote(order, user=None) -> Offer:
    """Запит пропозиції з магазину (SalesOrder QUOTE) → Angebot з поточними цінами Minerva."""
    from crm.models import Customer
    customer = Customer.objects.filter(external_key=order.customer_key).first() if order.customer_key else None
    if customer is None and order.email:
        customer = Customer.objects.filter(email__iexact=order.email).first()
    offer = new_offer(customer=customer, user=user, source_quote=order,
                      contact_name=order.contact_name or None, contact_email=order.email or None)
    for l in order.lines.select_related("product"):
        line = add_line(offer, product=l.product, qty=l.qty, recalc=False)
        if l.unit_price is not None:
            line.price_note = (f"{line.price_note} · у запиті: {pricing._num(l.unit_price)} {l.currency}")[:255]
            line.save(update_fields=["price_note"])
    recalculate(offer)
    return offer


def revision_attachment(rev_pk) -> dict | None:
    """Для листа: документ версії (PDF, інакше DOCX) як вкладення."""
    rev = OfferRevision.objects.filter(pk=rev_pk).select_related("offer").first()
    if rev is None:
        return None
    f = rev.pdf_file or rev.docx_file
    if not f:
        return None
    with f.open("rb") as fh:
        content = fh.read()
    ext = "pdf" if rev.pdf_file else "docx"
    ctype = "application/pdf" if ext == "pdf" else \
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return {"name": f"{rev.file_name}.{ext}", "content": content, "content_type": ctype}


# ── Відправка листом ─────────────────────────────────────────────────────────

_MAIL_TEXT = {
    "de": ("Sehr geehrte Damen und Herren,", "Sehr geehrte/r {name},",
           "anbei erhalten Sie unser Angebot {number}. Es ist gültig bis {valid}."),
    "en": ("Dear Sir or Madam,", "Dear {name},",
           "please find attached our quotation {number}. It is valid until {valid}."),
    "uk": ("Доброго дня,", "Доброго дня, {name},",
           "надсилаємо комерційну пропозицію {number}. Вона дійсна до {valid}."),
}


def compose_initial(offer_pk, user=None) -> dict:
    """Початкові дані листа з Angebot: кому, тема, короткий текст, PDF поточної версії (вкладення-посилання)."""
    import html
    from django.shortcuts import get_object_or_404
    offer = get_object_or_404(Offer, pk=offer_pk)
    rev = generate_document(offer, user)          # надіслана версія не перезаписується
    settings_ = OfferSettings.get()
    lang = offer.language if offer.language in _MAIL_TEXT else "de"
    hello, hello_named, body = _MAIL_TEXT[lang]
    valid = offer.valid_until.strftime("%d.%m.%Y") if offer.valid_until else "—"
    greeting = hello_named.format(name=offer.contact_name) if offer.contact_name else hello
    text = body.format(number=offer.display_number, valid=valid)
    subject = settings_.email_subject.format(
        number=offer.display_number, customer=getattr(offer.customer, "company", "") or getattr(offer.customer, "name", ""))
    att = revision_attachment(rev.pk)
    return {
        "to": offer.contact_email or getattr(offer.customer, "email", "") or "",
        "subject": subject,
        "body_html": f"{html.escape(greeting)}<br><br>{html.escape(text)}",
        "att_refs": [{"ref": f"offer:{rev.pk}", "name": att["name"]}] if att else [],
    }


def mark_sent_refs(refs, message_id="") -> int:
    """Лист із документом Angebot надіслано → позначити відповідні версії надісланими."""
    n = 0
    for ref in refs:
        _, _, pk = ref.partition(":")
        rev = OfferRevision.objects.filter(pk=int(pk)).select_related("offer").first() if pk.isdigit() else None
        if rev is not None:
            mark_sent(rev, message_id)
            n += 1
    return n
