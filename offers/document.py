"""Документ Angebot: DOCX за шаблоном (docxtpl) + PDF через LibreOffice (documents.service._convert_to_pdf).

Дані — лише зі знімка версії (OfferRevision.snapshot), тож документ надісланої версії відтворюваний.
Шаблон: власний (OfferSettings.template), інакше — на бланку Commercial Invoice (offers/template_builder.py,
кеш MEDIA_ROOT/offer_templates/), інакше простий offers/templates_docx/offer_template.docx
(build_offer_template). Підписи — мовою документа (de / en / de_en / uk).
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
LABELS["de"]["seller"], LABELS["en"]["seller"], LABELS["uk"]["seller"] = "Anbieter", "Supplier", "Постачальник"
LABELS["de"]["tiers_note"] = "Staffelpreise: Die Summen beziehen sich jeweils auf die erste Menge."
LABELS["en"]["tiers_note"] = "Tiered prices: totals refer to the first quantity of each item."
LABELS["uk"]["tiers_note"] = "Ступінчасті ціни: суми — для першої кількості кожної позиції."
# «Deutsch + English» — один документ, підписи обома мовами («Menge / Qty»), числа й дати — як у німецькому
LABELS["de_en"] = {k: v if v == LABELS["en"][k] else f"{v} / {LABELS['en'][k]}" for k, v in LABELS["de"].items()}
LABELS["de_en"]["tiers_note"] = f"{LABELS['de']['tiers_note']}\n{LABELS['en']['tiers_note']}"
EUROPEAN = ("de", "uk", "de_en")   # 1 234,50 і 28.10.2026


def _fmt_money(value, lang) -> str:
    if value in (None, ""):
        return ""
    d = Decimal(str(value)).quantize(Decimal("0.01"))
    s = f"{d:,.2f}"                                   # 1,234.50
    return s.replace(",", " ").replace(".", ",") if lang in EUROPEAN else s


def _fmt_unit(value, lang) -> str:
    """Ціна за одиницю: щонайменше 2, до 4 знаків (дрібні компоненти)."""
    if value in (None, ""):
        return ""
    d = Decimal(str(value)).normalize()
    places = max(2, min(4, -d.as_tuple().exponent if d.as_tuple().exponent < 0 else 0))
    s = f"{Decimal(str(value)):,.{places}f}"
    return s.replace(",", " ").replace(".", ",") if lang in EUROPEAN else s


def _fmt_qty(value) -> str:
    d = Decimal(str(value)).normalize()
    return format(d, "f").rstrip("0").rstrip(".") if "." in format(d, "f") else format(d, "f")


def _fmt_date(iso, lang) -> str:
    if not iso:
        return ""
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}" if lang in EUROPEAN else f"{y}-{m}-{d}"


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
        tiers = l.get("tiers") or []
        if tiers:   # Staffelpreise: кожна к-сть — окремий рядок у тих самих комірках (шаблон не змінюється)
            qty = Listing("\n".join(f"{_fmt_qty(t['qty'])} {l.get('unit', '')}".strip() for t in tiers))
            unit_price = Listing("\n".join(_fmt_unit(t["unit_price"], lang) if t.get("unit_price") else L["on_request"]
                                           for t in tiers))
            total = Listing("\n".join(_fmt_money(t["line_total"], lang) if t.get("unit_price") else L["on_request"]
                                      for t in tiers))
        else:
            qty = f"{_fmt_qty(l.get('qty', '0'))} {l.get('unit', '')}".strip()
            unit_price = L["on_request"] if on_request else _fmt_unit(l.get("unit_price"), lang)
            total = L["on_request"] if on_request else _fmt_money(l.get("line_total"), lang)
        lines.append({
            "pos": l.get("position"), "sku": l.get("sku", ""), "description": Listing(l.get("description", "")),
            "qty": qty, "unit_price": unit_price,
            "discount": f"{_fmt_qty(disc)} %" if disc else "",
            "total": total,
            "lead_time": l.get("lead_time", ""),
        })
    has_tiers = any(l.get("tiers") for l in snap.get("lines", []))
    shipping = Decimal(snap.get("shipping_cost") or "0")
    customer = snap.get("customer") or {}
    customer_name = customer.get("company") or customer.get("name") or ""
    # адреса без рядків, що вже стоять окремо (назва клієнта, контактна особа)
    address = "\n".join(ln for ln in (snap.get("billing_address") or "").splitlines()
                        if ln.strip() and ln.strip() not in (customer_name, snap.get("contact_name", "")))
    project = snap.get("project") or {}
    return {
        "L": L, "currency": cur,
        "company_block": Listing(s.company_block or ""), "footer_text": Listing(s.footer_text or ""),
        "number": snap.get("display_number") or snap.get("number", ""),
        "revision": snap.get("revision", 1),
        "date": _fmt_date(snap.get("date"), lang), "valid_until": _fmt_date(snap.get("valid_until"), lang),
        "customer_name": customer_name,
        "billing_address": Listing(address),
        "contact_name": snap.get("contact_name", ""), "contact_email": snap.get("contact_email", ""),
        "signer": snap.get("signer", ""),
        "project": f"{project.get('code', '')} · {project.get('name', '')}" if project else "",
        "intro_text": Listing(snap.get("intro_text", "")),
        "outro_text": Listing("\n\n".join(t for t in ((L["tiers_note"] if has_tiers else ""),
                                                        snap.get("outro_text", "")) if t)),
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


def blank_template() -> str:
    """Шаблон Angebot на бланку Commercial Invoice (той самий логотип і реквізити); перебудовується, коли бланк
    змінився (завантажили свій) або змінилась побудова. Немає бланка / збій — ''."""
    from . import template_builder
    try:
        from shipping.services.commercial_invoice_service import active_template_path
        blank = Path(active_template_path())
    except ImportError:
        return ""
    if not blank.exists():
        return ""
    out = Path(settings.MEDIA_ROOT) / "offer_templates" / f"from_ci_v{template_builder.VERSION}.docx"
    try:
        if not out.exists() or out.stat().st_mtime < blank.stat().st_mtime:
            out.parent.mkdir(parents=True, exist_ok=True)
            template_builder.build(out, blank=blank)
        return str(out)
    except Exception as e:  # noqa: BLE001 — запасний простий шаблон
        logger.warning("offer template from CI blank: %s", e)
        return ""


def template_path() -> str:
    from .models import OfferSettings
    s = OfferSettings.get()
    if s.template:
        try:
            return s.template.path
        except (ValueError, NotImplementedError):
            pass
    return blank_template() or str(DEFAULT_TEMPLATE)


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
