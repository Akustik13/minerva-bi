"""Документ Angebot: DOCX за шаблоном (docxtpl) + PDF через LibreOffice (documents.service._convert_to_pdf).

Дані — лише зі знімка версії (OfferRevision.snapshot), тож документ надісланої версії відтворюваний.
Шаблон: власний (OfferSettings.template) або стандартний offers/templates_docx/offer_template.docx
(створюється командою build_offer_template). Підписи — мовою документа (de / en / uk).
"""
import logging
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE = Path(settings.BASE_DIR) / "offers" / "templates_docx" / "offer_template.docx"

LABELS = {
    "de": {"title": "Angebot", "number": "Angebotsnummer", "date": "Datum", "valid_until": "Gültig bis",
           "customer": "Kunde", "contact": "Ansprechpartner", "project": "Projekt", "greeting": "Sehr geehrte Damen und Herren,",
           "pos": "Pos.", "sku": "Artikel", "description": "Beschreibung", "qty": "Menge", "unit_price": "Einzelpreis",
           "discount": "Rabatt", "total": "Gesamt", "lead_time": "Lieferzeit", "shipping": "Versand",
           "net": "Summe netto", "vat": "MwSt.", "gross": "Gesamtbetrag", "payment": "Zahlungsbedingungen",
           "delivery": "Lieferbedingungen", "on_request": "auf Anfrage", "closing": "Mit freundlichen Grüßen",
           "version": "Version"},
    "en": {"title": "Quotation", "number": "Quotation no.", "date": "Date", "valid_until": "Valid until",
           "customer": "Customer", "contact": "Contact", "project": "Project", "greeting": "Dear Sir or Madam,",
           "pos": "Pos.", "sku": "Part no.", "description": "Description", "qty": "Qty", "unit_price": "Unit price",
           "discount": "Discount", "total": "Total", "lead_time": "Lead time", "shipping": "Shipping",
           "net": "Net total", "vat": "VAT", "gross": "Total amount", "payment": "Payment terms",
           "delivery": "Delivery terms", "on_request": "on request", "closing": "Kind regards",
           "version": "Version"},
    "uk": {"title": "Комерційна пропозиція", "number": "Номер", "date": "Дата", "valid_until": "Дійсна до",
           "customer": "Клієнт", "contact": "Контактна особа", "project": "Проєкт", "greeting": "Шановні колеги,",
           "pos": "№", "sku": "Артикул", "description": "Опис", "qty": "К-сть", "unit_price": "Ціна за од.",
           "discount": "Знижка", "total": "Сума", "lead_time": "Строк поставки", "shipping": "Доставка",
           "net": "Разом без ПДВ", "vat": "ПДВ", "gross": "Разом до сплати", "payment": "Умови оплати",
           "delivery": "Умови поставки", "on_request": "за запитом", "closing": "З повагою",
           "version": "Версія"},
}


def _fmt_money(value, lang) -> str:
    if value in (None, ""):
        return ""
    d = Decimal(str(value)).quantize(Decimal("0.01"))
    s = f"{d:,.2f}"                                   # 1,234.50
    return s.replace(",", " ").replace(".", ",") if lang in ("de", "uk") else s


def _fmt_unit(value, lang) -> str:
    """Ціна за одиницю: щонайменше 2, до 4 знаків (дрібні компоненти)."""
    if value in (None, ""):
        return ""
    d = Decimal(str(value)).normalize()
    places = max(2, min(4, -d.as_tuple().exponent if d.as_tuple().exponent < 0 else 0))
    s = f"{Decimal(str(value)):,.{places}f}"
    return s.replace(",", " ").replace(".", ",") if lang in ("de", "uk") else s


def _fmt_qty(value) -> str:
    d = Decimal(str(value)).normalize()
    return format(d, "f").rstrip("0").rstrip(".") if "." in format(d, "f") else format(d, "f")


def _fmt_date(iso, lang) -> str:
    if not iso:
        return ""
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}" if lang in ("de", "uk") else f"{y}-{m}-{d}"


def build_context(snap: dict) -> dict:
    from docxtpl import Listing

    from .models import OfferSettings
    s = OfferSettings.get()
    lang = snap.get("language") if snap.get("language") in LABELS else "de"
    L = LABELS[lang]
    cur = snap.get("currency", "")
    lines = []
    for l in snap.get("lines", []):
        on_request = l.get("unit_price") in (None, "")
        disc = Decimal(l.get("discount_percent") or "0")
        lines.append({
            "pos": l.get("position"), "sku": l.get("sku", ""), "description": Listing(l.get("description", "")),
            "qty": f"{_fmt_qty(l.get('qty', '0'))} {l.get('unit', '')}".strip(),
            "unit_price": L["on_request"] if on_request else _fmt_unit(l.get("unit_price"), lang),
            "discount": f"{_fmt_qty(disc)} %" if disc else "",
            "total": L["on_request"] if on_request else _fmt_money(l.get("line_total"), lang),
            "lead_time": l.get("lead_time", ""),
        })
    shipping = Decimal(snap.get("shipping_cost") or "0")
    customer = snap.get("customer") or {}
    project = snap.get("project") or {}
    return {
        "L": L, "currency": cur,
        "company_block": Listing(s.company_block or ""), "footer_text": Listing(s.footer_text or ""),
        "number": snap.get("display_number") or snap.get("number", ""),
        "revision": snap.get("revision", 1),
        "date": _fmt_date(snap.get("date"), lang), "valid_until": _fmt_date(snap.get("valid_until"), lang),
        "customer_name": customer.get("company") or customer.get("name") or "",
        "billing_address": Listing(snap.get("billing_address", "")),
        "contact_name": snap.get("contact_name", ""),
        "project": f"{project.get('code', '')} · {project.get('name', '')}" if project else "",
        "intro_text": Listing(snap.get("intro_text", "")), "outro_text": Listing(snap.get("outro_text", "")),
        "payment_terms": Listing(snap.get("payment_terms", "")),
        "delivery_terms": Listing(snap.get("delivery_terms", "")),
        "lines": lines,
        "has_discount": any(l["discount"] for l in lines),
        "has_lead_time": any(l["lead_time"] for l in lines),
        "has_shipping": shipping > 0, "shipping_cost": _fmt_money(shipping, lang),
        "vat_rate": _fmt_qty(Decimal(snap.get("vat_rate") or "0")),
        "net_total": _fmt_money(snap.get("net_total"), lang), "vat_total": _fmt_money(snap.get("vat_total"), lang),
        "gross_total": _fmt_money(snap.get("gross_total"), lang),
    }


def template_path() -> str:
    from .models import OfferSettings
    s = OfferSettings.get()
    if s.template:
        try:
            return s.template.path
        except (ValueError, NotImplementedError):
            pass
    return str(DEFAULT_TEMPLATE)


def render_docx(snap: dict) -> bytes:
    from docxtpl import DocxTemplate
    tpl = DocxTemplate(template_path())
    tpl.render(build_context(snap), autoescape=True)
    buf = BytesIO()
    tpl.save(buf)
    return buf.getvalue()


def render_files(rev) -> None:
    """DOCX (+ PDF, якщо є LibreOffice) для версії."""
    from documents.service import _convert_to_pdf
    for f in (rev.docx_file, rev.pdf_file):
        if f:
            f.delete(save=False)
    rev.docx_file.save(f"{rev.file_name}.docx", ContentFile(render_docx(rev.snapshot)), save=False)
    try:
        pdf = _convert_to_pdf(rev.docx_file.path)
    except Exception as e:  # noqa: BLE001
        logger.warning("Angebot %s: PDF conversion failed: %s", rev, e)
        pdf = None
    if pdf:
        rev.pdf_file.save(f"{rev.file_name}.pdf", ContentFile(pdf), save=False)
    rev.save()
