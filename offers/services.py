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
    from shop.countries import COUNTRIES
    country = (customer.country or "").strip()      # «DE» → «Germany» (як на бланку Quotation)
    country = COUNTRIES.get(country.upper(), {}).get("en", country) if len(country) == 2 else country
    parts = [customer.company or "", customer.name or "", customer.addr_street or "",
             " ".join(p for p in (customer.addr_zip, customer.addr_city) if p), country]
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
        "currency": pricing.currency_for(customer)["currency"] if customer is not None else s.default_currency,
        "price_mode": s.price_mode, "lead_mode": s.lead_mode,
        "vat_rate": s.default_vat_rate, "language": s.default_language,
        "valid_until": timezone.localdate() + timedelta(days=s.validity_days),
        "intro_text": s.intro_text, "payment_terms": s.payment_terms, "delivery_terms": s.delivery_terms,
        "outro_text": s.outro_text,
        "contact_name": getattr(customer, "name", "") or "", "contact_email": getattr(customer, "email", "") or "",
        "contact_phone": getattr(customer, "phone", "") or "", "billing_address": customer_address(customer),
        **dict(zip(("signer_name", "signer_position"), last_signer())),
    }


def last_signer() -> tuple:
    """Підписант і посада — як в останньому Commercial Invoice / пакувальному листі (підпис і печатка бланка)."""
    try:
        from shipping.services.packing_list_service import last_signer as _last
        return _last()
    except Exception:  # noqa: BLE001 — без доставки: підписує відповідальний
        return "", ""


def new_offer(*, customer=None, user=None, **fields) -> Offer:
    data = {**defaults(customer), **{k: v for k, v in fields.items() if v is not None}}
    return Offer.objects.create(customer=customer, owner=data.pop("owner", None) or user, created_by=user, **data)


def add_line(offer, *, product=None, qty=1, sku="", description="", unit_price=None, discount_percent=0,
             price_source=None, price_note="", lead_time="", unit="Stk", recalc=True) -> OfferLine:
    """Позиція: без ціни — ціна за «Ціни з» Angebot (pricing.quote_price), без строку — pricing.quote_lead."""
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


def _dk_title(sku) -> str:
    listing = pricing.dk_listing(None, sku) if sku else None
    return listing.dk_title if listing else ""


def fill_line(line, *, sku="", description="", unit_price=None, price_source=None, price_note=""):
    """Доповнити позицію: артикул, опис і строк — з товару (або нашого офера DigiKey), ціна — за «Ціни з»
    Angebot, якщо її не задано вручну. Немає ні в каталозі, ні на DigiKey — примітка «виробництво / розробка»."""
    p, offer = line.product, line.offer
    line.sku = sku or line.sku or (p.sku if p else "")
    line.description = description or line.description or (p.name_export or p.name if p else "") or \
        _dk_title(line.sku) or line.sku
    if not line.lead_time:
        lq = pricing.quote_lead(p, line.qty, offer.language, offer.lead_mode, sku=line.sku)
        line.lead_time, line.lead_note = lq.text, lq.note[:255]
    elif not line.lead_note:
        line.lead_note = pricing.MANUAL_LEAD
    if unit_price is not None:
        line.unit_price = Decimal(str(unit_price))
        line.price_source = price_source or OfferLine.MANUAL
        line.price_note = price_note
    elif line.unit_price is None:
        q = pricing.quote_price(p, line.qty, offer.currency, offer.price_mode, sku=line.sku)
        line.unit_price, line.price_source, line.price_note = q.unit_price, q.source, q.note[:255]
    elif line.unit_price is not None and not line.price_note:
        line.price_source, line.price_note = OfferLine.MANUAL, "Введено вручну"
    line.line_total = pricing.line_total(line.qty, line.unit_price, line.discount_percent)


def parse_tiers(text) -> list:
    """«35; 100» або «35: 12,50; 100: 10,90» → [(к-сть, ціна | None)] (ступені — через «;», ціна — після «:»).
    Без «:» кількості можна й через кому з пробілом: «35, 100»."""
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"[;\n|]+", text) if (";" in text or ":" in text or "\n" in text) else re.split(r",\s+|\s+", text)
    out = []
    for part in parts:
        q, _, p = part.partition(":")
        q, p = q.strip().replace(",", "."), p.strip().replace(" ", "").replace(",", ".")
        try:
            qty = Decimal(q)
        except Exception:  # noqa: BLE001
            continue
        if qty <= 0:
            continue
        try:
            price = Decimal(p) if p else None
        except Exception:  # noqa: BLE001
            price = None
        out.append((qty, price))
    return out


def tiers_text(line) -> str:
    """Ступені позиції для поля адмінки: «35: 12.5; 100: 10.9» (ціна з Minerva — теж показана)."""
    return "; ".join(f"{pricing._num(t['qty'])}: {pricing._num(t['unit_price'])}" if t.get("unit_price")
                     else pricing._num(t["qty"]) for t in (line.tiers or []))


def _lead_is_auto(line) -> bool:
    """Строк позиції підставила Minerva (не введено вручну; старі позиції без примітки — вручну)."""
    return not line.lead_time or bool(line.lead_note and line.lead_note != pricing.MANUAL_LEAD)


def set_tiers(line, tiers, leads=None) -> None:
    """Ступінчасті ціни (Staffelpreise): [(к-сть, ціна | None)]; без ціни — за «Ціни з» Angebot саме для цієї
    к-сті (ступені магазину / базові ціни / DigiKey). Строк — свій для кожної к-сті (35 є на складі — «ab Lager»,
    100 — термін поставки) або leads, введені вручну через «;». Перша — основна к-сть і ціна позиції (з неї сума
    Angebot); одна або жодної — звичайна позиція. Не зберігає."""
    offer, rows = line.offer, []
    auto_lead = not leads and _lead_is_auto(line)
    for i, (qty, price) in enumerate(tiers):
        source, note = OfferLine.MANUAL, "Введено вручну"
        if price is None:
            q = pricing.quote_price(line.product, qty, offer.currency, offer.price_mode, sku=line.sku)
            price, source, note = q.unit_price, q.source, q.note
        if leads:
            lead, lead_note = (leads[i] if i < len(leads) and leads[i] else leads[-1]), pricing.MANUAL_LEAD
        elif auto_lead:
            lq = pricing.quote_lead(line.product, qty, offer.language, offer.lead_mode, sku=line.sku)
            lead, lead_note = lq.text, lq.note
        else:
            lead, lead_note = line.lead_time, line.lead_note
        rows.append({"qty": pricing._num(qty), "unit_price": _s(price) if price is not None else "",
                     "line_total": _s(pricing.line_total(qty, price, line.discount_percent)), "price_note": note,
                     "price_source": source, "lead_time": lead, "lead_note": lead_note[:255]})
    line.tiers = rows if len(rows) > 1 else []
    if rows:
        first = rows[0]
        line.qty = Decimal(first["qty"])
        line.unit_price = Decimal(first["unit_price"]) if first["unit_price"] else None
        line.price_source, line.price_note = first["price_source"], first["price_note"][:255]
        line.lead_time, line.lead_note = first["lead_time"][:100], first["lead_note"]
    line.line_total = pricing.line_total(line.qty, line.unit_price, line.discount_percent)


def _sync_tiers(line) -> bool:
    """Перший ступінь = к-сть і ціна позиції (їх можна змінити в адмінці), суми ступенів — зі знижкою."""
    if not line.tiers:
        return False
    rows = [dict(t) for t in line.tiers]
    rows[0].update(qty=pricing._num(line.qty), unit_price=_s(line.unit_price) if line.unit_price is not None else "")
    for t in rows:
        t["line_total"] = _s(pricing.line_total(Decimal(t["qty"]), Decimal(t["unit_price"]) if t["unit_price"]
                                                else None, line.discount_percent))
    changed = rows != line.tiers
    line.tiers = rows
    return changed


def request_kind(lines) -> str:
    """📦 каталог / 🏭 виробництво й розробка (немає ні в каталозі Minerva, ні на DigiKey) / змішаний."""
    known = [bool(l.product_id) or (bool(l.sku) and pricing.dk_listing(None, l.sku) is not None) for l in lines]
    if not known or all(known):
        return Offer.STOCK
    return Offer.MIXED if any(known) else Offer.PRODUCTION


def recalculate(offer) -> Offer:
    lines = list(offer.lines.all())
    for i, line in enumerate(lines, 1):
        total = pricing.line_total(line.qty, line.unit_price, line.discount_percent)
        tiers_changed = _sync_tiers(line)
        if line.line_total != total or line.position != i or tiers_changed:
            line.line_total, line.position = total, i
            line.save(update_fields=["line_total", "position", "tiers"])
    t = pricing.totals([l.line_total for l in lines], offer.shipping_cost, offer.vat_rate)
    offer.net_total, offer.vat_total, offer.gross_total = t["net"], t["vat"], t["gross"]
    offer.request_kind = request_kind(lines)
    Offer.objects.filter(pk=offer.pk).update(net_total=t["net"], vat_total=t["vat"], gross_total=t["gross"],
                                             request_kind=offer.request_kind, updated_at=timezone.now())
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
    if offer.request_kind != Offer.STOCK:
        out.append("🏭 Є позиції, яких немає ні в каталозі Minerva, ні на DigiKey (виробництво / розробка) — "
                   "ціну й строк перевірте вручну")
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
        "contact_name": offer.contact_name, "contact_email": offer.contact_email, "contact_phone": offer.contact_phone,
        "billing_address": offer.billing_address,
        "signer": offer.signer_name or ((offer.owner.get_full_name() or offer.owner.get_username())
                                        if offer.owner_id else ""),
        "signer_position": offer.signer_position,
        "project": {"id": offer.project_id, "code": getattr(offer.project, "code", ""),
                    "name": getattr(offer.project, "name", "")} if offer.project_id else None,
        "intro_text": offer.intro_text, "payment_terms": offer.payment_terms,
        "delivery_terms": offer.delivery_terms, "outro_text": offer.outro_text,
        "lines": [{"position": l.position, "sku": l.sku, "description": l.description, "qty": _s(l.qty),
                   "unit": l.unit, "unit_price": _s(l.unit_price), "discount_percent": _s(l.discount_percent),
                   "line_total": _s(l.line_total), "lead_time": l.lead_time, "price_source": l.price_source,
                   "price_note": l.price_note, "product_id": l.product_id, "tiers": l.tiers or []}
                  for l in offer.lines.all()],
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
    fw = None
    if source is not None and is_own(source.from_email, own):   # колега переслав лист клієнта (FW / WG)
        from email_assistant.quotes import forwarded
        fw = forwarded(source.body_text, skip=lambda a: is_own(a, own))   # «WG: FW: …» — до листа ззовні
        if fw and customer is None:
            from email_assistant.crm_link import find_customer
            customer = find_customer([fw["from"]])
    offer = new_offer(customer=customer, user=user, project=project, source_message=source)
    if source is not None:
        if not offer.contact_email and (fw or not is_own(source.from_email, own)):
            offer.contact_email = fw["from"] if fw else source.from_email
            offer.contact_name = offer.contact_name or (fw["name"] if fw else source.from_name)
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
    langs = ["de", "en"] if offer.language == "de_en" else [offer.language if offer.language in _MAIL_TEXT else "de"]
    valid = offer.valid_until.strftime("%d.%m.%Y") if offer.valid_until else "—"
    paragraphs = []
    for lang in langs:   # «Deutsch + English» — обидва абзаци
        hello, hello_named, body = _MAIL_TEXT[lang]
        greeting = hello_named.format(name=offer.contact_name) if offer.contact_name else hello
        paragraphs.append(f"{html.escape(greeting)}<br><br>{html.escape(body.format(number=offer.display_number, valid=valid))}")
    subject = settings_.email_subject.format(
        number=offer.display_number, customer=getattr(offer.customer, "company", "") or getattr(offer.customer, "name", ""))
    att = revision_attachment(rev.pk)
    return {
        "to": offer.contact_email or getattr(offer.customer, "email", "") or "",
        "subject": subject,
        "body_html": "<br><br>".join(paragraphs),
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


# ── ⚡ Легка форма Angebot (як Commercial Invoice: сторінка → документ на бланку) ──────────────
QTY_RE = re.compile(r"(?<![\w.,/-])(\d{1,6}(?:[.,]\d{1,3})?)\s*(?:x\s*)?(?:units?|pcs\.?|pieces?|stk\.?|stück|"
                    r"st\.|шт\.?|штук\w*|items?|ea)(?!\w)", re.IGNORECASE)
SKU_RE = re.compile(r"\b([A-Z]{1,6}[-_]?\d{3,}[A-Z0-9-]*)\b")
_DE_RE = re.compile(r"\b(?:und|bitte|für|mit|wir|ihr|ihnen|sehr|geehrte\w*|grüßen|angebot|stück|liefer\w*|anfrage)\b",
                    re.IGNORECASE)
_EN_RE = re.compile(r"\b(?:the|and|please|for|with|we|you|regards|quotation|quote|units|pricing|delivery|lead)\b",
                    re.IGNORECASE)
BLANK_ROW = {"sku": "", "description": "", "qtys": "1", "prices": "", "lead_time": "", "unit": "Stk"}


def thread_source(thread):
    """Для Angebot з листування: (вихідний лист, пересланий лист клієнта | None, клієнт, проєкт).
    Колега переслав (FW / WG) — контакт і клієнт з пересланого листа."""
    from email_assistant.crm_link import find_customer, is_own, own_addresses
    from email_assistant.quotes import forwarded
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
    fw = None
    if source is not None and is_own(source.from_email, own):
        fw = forwarded(source.body_text, skip=lambda a: is_own(a, own))   # «WG: FW: …» — до листа ззовні
        if fw and customer is None:
            customer = find_customer([fw["from"]])
    return source, fw, customer, project


def guess_language(text: str, default: str) -> str:
    """Мова документа за мовою листа: англійський текст → en, німецький → de, інакше — як у налаштуваннях."""
    de, en = len(_DE_RE.findall(text or "")), len(_EN_RE.findall(text or ""))
    if en >= 3 and en > de * 1.5:
        return "en"
    if de >= 3 and de > en:
        return "de"
    return default


def _quantities(text: str) -> list:
    out = []
    for m in QTY_RE.finditer(text or ""):
        q = Decimal(m.group(1).replace(",", "."))
        if q > 0 and q not in out:
            out.append(q)
    return out


def _qtys_text(values) -> str:
    return "; ".join(pricing._num(v) for v in values)


_FILLER = {"and", "und", "or", "oder", "the", "for", "für", "with", "mit", "of", "von", "і", "та", "и"}
MAX_EXTRA_CODES = 5
_VAT_RE = re.compile(r"^[A-Z]{2}\d{8,12}$")    # USt-IdNr / VAT ID (DE123456789), не артикул


def _describe_code(text, code) -> str:
    """Опис позиції без каталогу: короткий рядок листа з артикулом («Filter FT220407») або слово перед ним."""
    line = next((ln.strip(" .:;,-") for ln in text.splitlines() if code in ln), "")
    if line and len(line) <= 60 and len(SKU_RE.findall(line)) == 1:   # рядок з кількома артикулами — ні
        return line
    m = re.search(rf"([A-Za-zÄÖÜäöüßА-Яа-яІіЇїЄє]+)\s+{re.escape(code)}", line)
    return f"{m.group(1)} {code}" if m and m.group(1).lower() not in _FILLER else code


def quick_prefill(thread=None) -> dict:
    """Дані легкої форми: з листування (клієнт, контакт, проєкт, мова листа, артикули каталогу; кількості з листа —
    ступенями, напр. «35 units and 100 units» → 35; 100) або порожні з налаштувань."""
    from email_assistant.quotes import new_text
    s = OfferSettings.get()
    data = {"customer": None, "contact_name": "", "contact_email": "", "contact_phone": "", "billing_address": "",
            "project": None, **dict(zip(("signer_name", "signer_position"), last_signer())),
            "language": s.default_language, "currency": s.default_currency, "valid_days": s.validity_days,
            "currency_reason": "з «Налаштувань Angebote» — виберіть клієнта, і валюта стане за його країною"
            if s.currency_by_country else "з «Налаштувань Angebote»",
            "price_mode": s.price_mode, "lead_mode": s.lead_mode, "origin": {},
            "shipping_cost": "0", "intro_text": s.intro_text, "payment_terms": s.payment_terms,
            "delivery_terms": s.delivery_terms, "outro_text": s.outro_text, "source": None, "forwarded": None,
            "lines": [dict(BLANK_ROW)]}
    if thread is None:
        return data
    source, fw, customer, project = thread_source(thread)
    if source is None:
        return data
    text = f"{fw['subject']}\n{fw['text']}" if fw else f"{source.subject or ''}\n{new_text(source.body_text)}"
    qtys = _quantities(text)
    items = detect_items(text)
    rows = []
    for product, qty in items:   # «35 units and 100 units» для всіх — кожному артикулу всі кількості
        tiers = qtys if len(qtys) > 1 and (len(items) == 1 or not qty) else ([qty] if qty else qtys[:1])
        rows.append({**BLANK_ROW, "sku": product.sku, "description": product.name_export or product.name,
                     "qtys": _qtys_text(tiers) or "1"})
    # схожі на артикул слова, яких немає в каталозі (FT220407) — теж позиції: DigiKey або 🏭 виробництво / розробка
    from email_assistant.quotes import strip_signature
    from inventory.services.stock import resolve_sku
    from sales.models import SalesOrder
    known = {r["sku"].upper() for r in rows}
    extra = []
    for code in dict.fromkeys(m.group(1) for m in SKU_RE.finditer(strip_signature(text))):   # без підпису
        if code.upper() in known or len(extra) >= MAX_EXTRA_CODES or _VAT_RE.match(code) or \
                resolve_sku(code) is not None or SalesOrder.objects.filter(order_number__iexact=code).exists():
            continue
        extra.append(code)
        rows.append({**BLANK_ROW, "sku": code, "description": _describe_code(text, code),
                     "qtys": _qtys_text(qtys) or "1"})
    if not rows:
        rows.append(dict(BLANK_ROW))
    # клієнта в CRM немає — адреса й ім'я з підпису листа (для пересланого — з пересланого листа)
    from email_assistant.quotes import signature
    sig = signature(fw["text"] if fw else new_text(source.body_text))
    language = guess_language(text, "")
    data.update(
        customer=customer, project=project, source=source, forwarded=fw, lines=rows,
        contact_name=(customer.name if customer else "") or sig["name"] or (fw["name"] if fw else source.from_name) or "",
        contact_email=(fw["from"] if fw else source.from_email) or getattr(customer, "email", "") or "",
        contact_phone=getattr(customer, "phone", "") or sig.get("phone", ""),
        billing_address=customer_address(customer) or ("\n".join(sig["lines"]) if customer is None else ""),
        language=language or s.default_language)
    cur = pricing.currency_for(customer, address=data["billing_address"], email=data["contact_email"])
    data.update(currency=cur["currency"], currency_reason=cur["reason"])
    # звідки що взято — підписи під полями форми
    if customer is None:
        who = "клієнта в CRM не знайдено — дані з листа"
    elif thread.crm_customer_id == customer.pk:
        who = "лист прив'язаний до цього клієнта CRM"
    elif project is not None and project.customer_id == customer.pk:
        who = f"клієнт проєкту {project.code}"
    else:
        who = "знайдено в CRM за адресою пересланого листа"
    data["origin"] = {
        "customer": who,
        "contact": "картка CRM" if customer and customer.name else "підпис листа" if sig["name"] else
        "«Von:» пересланого листа" if fw else "відправник листа",
        "email": "«Von:» пересланого листа" if fw else "відправник листа",
        "address": "картка CRM" if customer_address(customer) else "підпис листа" if sig["lines"] and customer is None
        else "у листі не знайдено — введіть",
        "phone": "картка CRM" if getattr(customer, "phone", "") else "підпис листа" if sig.get("phone") else "",
        "language": "за мовою листа" if language else "з «Налаштувань Angebote» (мову листа не визначено)",
        "lines": "; ".join(x for x in (
            "артикули з каталогу Minerva, знайдені в листі" if items else "",
            f"без каталогу Minerva: {', '.join(extra)} — артикул і опис з листа" if extra else "",
            "артикулів у листі не знайдено — введіть" if not (items or extra) else "",
            f"кількості з листа: {_qtys_text(qtys)}" if qtys else "") if x),
    }
    return data


def quick_from_offer(offer) -> dict:
    """Чернетка Angebot у легкій формі: ціни — лише введені вручну (інші знову рахує Minerva)."""
    def manual(t):
        return t.get("unit_price") if t.get("price_source", OfferLine.MANUAL) == OfferLine.MANUAL else ""
    rows = []
    for l in offer.lines.all():
        tiers = l.tiers or [{"qty": _s(l.qty), "unit_price": _s(l.unit_price) if l.unit_price is not None else "",
                             "price_source": l.price_source}]
        prices = [manual(t) for t in tiers]
        leads = [t.get("lead_time") or l.lead_time for t in tiers]
        lead = "" if _lead_is_auto(l) else ("; ".join(leads) if len(set(leads)) > 1 else l.lead_time)
        rows.append({"sku": l.sku, "description": l.description,
                     "qtys": _qtys_text(Decimal(t["qty"]) for t in tiers),
                     "prices": "; ".join(pricing._num(p).replace(".", ",") if p else "" for p in prices).strip("; "),
                     "lead_time": lead, "unit": l.unit})
    s = OfferSettings.get()
    valid_days = (offer.valid_until - timezone.localdate()).days if offer.valid_until else s.validity_days
    return {"customer": offer.customer, "contact_name": offer.contact_name, "contact_email": offer.contact_email,
            "contact_phone": offer.contact_phone, "signer_name": offer.signer_name,
            "signer_position": offer.signer_position,
            "billing_address": offer.billing_address, "project": offer.project, "language": offer.language,
            "currency": offer.currency, "currency_reason": "збережено в цьому Angebot",
            "price_mode": offer.price_mode, "lead_mode": offer.lead_mode, "origin": {},
            "valid_days": max(valid_days, 0), "shipping_cost": _s(offer.shipping_cost),
            "intro_text": offer.intro_text, "payment_terms": offer.payment_terms,
            "delivery_terms": offer.delivery_terms, "outro_text": offer.outro_text, "source": offer.source_message,
            "forwarded": None, "lines": rows or [dict(BLANK_ROW)]}


def _row_tiers(row) -> list:
    """Рядок форми: «35; 100» + «12,50; 10,90» → [(35, 12.50), (100, 10.90)]; ціни немає — None (з Minerva)."""
    qtys = [q for q, _p in parse_tiers(row.get("qtys") or "")] or [Decimal("1")]
    prices = []
    for part in (row.get("prices") or "").split(";"):
        part = part.strip().replace(" ", "").replace(",", ".")
        try:
            prices.append(Decimal(part) if part else None)
        except Exception:  # noqa: BLE001
            prices.append(None)
    return [(q, prices[i] if i < len(prices) else None) for i, q in enumerate(qtys)]


@transaction.atomic
def save_quick(fields: dict, rows: list, user=None, offer=None, source=None, thread=None) -> Offer:
    """Легка форма → Angebot (новий або та сама чернетка): поля й позиції (артикул з каталогу — товар і ціна з
    Minerva для кожної кількості; ступені — set_tiers). Позиції чернетки замінюються цілком."""
    from inventory.models import Product
    keys = ("contact_name", "contact_email", "billing_address", "language", "currency", "valid_until",
            "shipping_cost", "intro_text", "payment_terms", "delivery_terms", "outro_text", "project",
            "price_mode", "lead_mode", "contact_phone", "signer_name", "signer_position")
    if offer is None:
        offer = new_offer(customer=fields.get("customer"), user=user, source_message=source,
                          **{k: fields[k] for k in keys if k in fields})
    else:
        if offer.is_locked:
            raise OfferError("Надіслану пропозицію не змінюють — створіть нову версію.")
        offer.customer = fields.get("customer")
        for k in keys:
            if k in fields:
                setattr(offer, k, fields[k])
        offer.save()
        offer.lines.all().delete()
    for row in rows:
        sku = (row.get("sku") or "").strip()[:255]
        description = (row.get("description") or "").strip()
        if not (sku or description):
            continue
        product = Product.objects.filter(sku__iexact=sku).first() if sku else None
        tiers = _row_tiers(row)
        leads = [x.strip()[:100] for x in (row.get("lead_time") or "").split(";")]   # «ab Lager; 4 Wochen»
        line = add_line(offer, product=product, qty=tiers[0][0], sku=sku, description=description,
                        unit_price=tiers[0][1], lead_time=leads[0],
                        unit=(row.get("unit") or "Stk").strip()[:16] or "Stk", recalc=False)
        if len(tiers) > 1:
            set_tiers(line, tiers, leads if len(leads) > 1 else None)
            line.save()
    recalculate(offer)
    if thread is not None:
        from .detection import mark_done
        mark_done(thread, offer, user)
    return offer


_SEP_RE = re.compile(r"[\s\-_/.,]+")
SEARCH_LIMIT = 15


def _norm(text) -> str:
    """«ft-220 407» → «FT220407»: артикул без розділювачів і регістру."""
    return _SEP_RE.sub("", str(text or "")).upper()


def search_products(q: str, limit: int = SEARCH_LIMIT) -> list:
    """Пошук для поля «Artikel» легкої форми (нічого не зберігає): «FT» → усі FT…; кілька слів — кожне в артикулі
    (без розділювачів: «FT-220» = «FT220»), назві, аліасі, категорії, виробнику чи технічних параметрах; плюс наші
    офери DigiKey без товару в каталозі. Порядок: точний артикул → артикул починається з → містить → решта;
    у межах — є на складі першими."""
    from django.db.models import F, Q, TextField, Value
    from django.db.models.functions import Cast, Replace, Upper

    from inventory.models import Product
    from inventory.services.stock import annotate_stock
    words = [w for w in (q or "").split() if w][:5]
    if not words:
        return []
    norm = Upper("sku")
    for sep in ("-", " ", "/", ".", "_"):
        norm = Replace(norm, Value(sep), Value(""))
    qs = Product.objects.filter(is_active=True).annotate(sku_norm=norm, attrs_text=Cast("tech_attributes", TextField()))
    for w in words:
        n = _norm(w)
        cond = (Q(sku__icontains=w) | Q(name__icontains=w) | Q(name_export__icontains=w) | Q(sku_short__icontains=w)
                | Q(manufacturer__icontains=w) | Q(category__icontains=w) | Q(productalias__alias__icontains=w)
                | Q(attrs_text__icontains=w))
        if n:
            cond |= Q(sku_norm__contains=n)
        qs = qs.filter(cond)
    ids = list(qs.values_list("pk", flat=True).distinct()[:300])
    first = _norm(words[0])
    found = []
    for p in annotate_stock(Product.objects.filter(pk__in=ids)):
        s = _norm(p.sku)
        rank = 0 if s == first else 1 if s.startswith(first) else 2 if first in s else 3
        found.append((rank, 0 if p._available > 0 else 1, p.sku.upper(), p))
    found.sort(key=lambda t: t[:3])
    out = [{"sku": p.sku, "name": p.name_export or p.name, "available": pricing._num(p._available),
            "price": pricing._num(p.sale_price) if p.sale_price is not None else "", "category": p.category or "",
            "source": "minerva"} for *_k, p in found[:limit]]
    try:   # наші офери DigiKey, яких немає в каталозі Minerva
        from bots.models import DigiKeyListing
        dk = DigiKeyListing.objects.filter(product__isnull=True)
        for w in words:
            dk = dk.filter(Q(dk_supplier_sku__icontains=w) | Q(dk_title__icontains=w))
        out += [{"sku": l.dk_supplier_sku, "name": l.dk_title, "available": "", "price": "", "category": "",
                 "source": "digikey"} for l in dk.exclude(dk_supplier_sku="")[:max(0, limit - len(out))]]
    except ImportError:
        pass
    return out


def price_hints(sku: str, qtys: str, currency: str, price_mode="auto", lead_mode="auto", language="de") -> dict:
    """Підказка в легкій формі (нічого не зберігає): для кожної кількості — ціна й строк і звідки вони
    (📦 склад Minerva / 🔷 DigiKey); склад і офер DigiKey; немає ніде — kind=production (виробництво / розробка)."""
    from inventory.models import Product
    sku = (sku or "").strip()
    product = Product.objects.filter(sku__iexact=sku).first() if sku else None
    listing = pricing.dk_listing(product, sku) if sku else None
    if product is None and listing is None:
        return {"found": False, "kind": Offer.PRODUCTION, "tiers": [], "note": pricing.PRODUCTION_NOTE}
    out = []
    for q, _x in parse_tiers(qtys or "1") or [(Decimal("1"), None)]:
        p = pricing.quote_price(product, q, currency, price_mode, sku)
        lq = pricing.quote_lead(product, q, language, lead_mode, sku)
        out.append({"qty": pricing._num(q), "unit_price": pricing._num(p.unit_price) if p.unit_price is not None else "",
                    "note": p.note, "source": p.source, "group": pricing.SOURCE_GROUPS.get(p.source, "manual"),
                    "lead_time": lq.text, "lead_note": lq.note, "lead_source": lq.source})
    stock = None
    if product is not None:
        from inventory.services.stock import get_available
        stock = pricing._num(get_available(product))
    dk = {"title": listing.dk_title, "moq": listing.dk_min_order_qty, "available": listing.dk_quantity_available,
          "active": listing.dk_is_active} if listing else None
    return {"found": True, "kind": Offer.STOCK, "in_catalog": product is not None, "stock": stock, "digikey": dk,
            "name": (product.name_export or product.name) if product else listing.dk_title, "tiers": out}
