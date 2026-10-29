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
# Бланк Angebot як Quotation на бланку Commercial Invoice (template_builder VERSION 2): підписи блоків і таблиці
QUOTE_LABELS = {
    "de": {"number": "Angebotsnr.", "date": "Angebotsdatum", "ship_to": "Lieferadresse (Empfänger/Käufer)",
           "ship_from": "Absender (Verkäufer)", "contact_person": "Ansprechpartner", "billing": "Rechnungsadresse",
           "incoterms": "Lieferbedingungen (Incoterms)",
           "vat_note": "Alle Preise verstehen sich zzgl. MwSt., Zöllen und Steuern, sofern nicht anders angegeben.",
           "valid_text": "Dieses Angebot ist gültig bis {date}.", "payment_info": "Zahlungsinformationen",
           "name": "Name", "position": "Position", "shipping_row": "Verpackung und Versand", "total_net": "Summe netto",
           "h_description": "Beschreibung", "h_qty": "Menge,\nEinh.", "h_unit": "Einzelpreis,\n{cur}",
           "h_total": "Gesamt,\n{cur}", "h_lead": "Lieferzeit,\nWochen,\nca."},
    "en": {"number": "Quotation No.", "date": "Quotation Date", "ship_to": "Ship to (Receiver/Buyer)",
           "ship_from": "Ship from (Sender/Seller)", "contact_person": "Contact Person", "billing": "Billing address",
           "incoterms": "Terms of delivery (Incoterms)",
           "vat_note": "All prices are exclusive of VAT, customs duties and taxes unless stated otherwise.",
           "valid_text": "This quotation is valid until {date}.", "payment_info": "Payment information",
           "name": "Name", "position": "Position", "shipping_row": "Packaging and Shipping Costs",
           "total_net": "Total (net)", "h_description": "Description", "h_qty": "Qty,\nUnits",
           "h_unit": "Unit\nValue,\n{cur}", "h_total": "Total\nValue,\n{cur}", "h_lead": "Lead time,\nweeks,\napprox."},
    "uk": {"number": "Номер", "date": "Дата", "ship_to": "Отримувач (покупець)", "ship_from": "Відправник (продавець)",
           "contact_person": "Контактна особа", "billing": "Адреса для рахунку", "incoterms": "Умови поставки (Incoterms)",
           "vat_note": "Ціни без ПДВ, мит і податків, якщо не зазначено інше.",
           "valid_text": "Пропозиція дійсна до {date}.", "payment_info": "Платіжні реквізити", "name": "Ім'я",
           "position": "Посада", "shipping_row": "Пакування й доставка", "total_net": "Разом без ПДВ",
           "h_description": "Опис", "h_qty": "К-сть,\nод.", "h_unit": "Ціна,\n{cur}", "h_total": "Сума,\n{cur}",
           "h_lead": "Строк,\nтижнів,\nприбл."},
}
for _lang, _labels in QUOTE_LABELS.items():
    LABELS[_lang].update(_labels)
LABELS["de"]["tiers_note"] = "Staffelpreise: Die Summen beziehen sich jeweils auf die erste Menge."
LABELS["en"]["tiers_note"] = "Tiered prices: totals refer to the first quantity of each item."
LABELS["uk"]["tiers_note"] = "Ступінчасті ціни: суми — для першої кількості кожної позиції."
# «Deutsch + English» — один документ, підписи обома мовами («Menge / Qty»), числа й дати — як у німецькому
LABELS["de_en"] = {k: v if v == LABELS["en"][k] else f"{v} / {LABELS['en'][k]}" for k, v in LABELS["de"].items()}
LABELS["de_en"]["tiers_note"] = f"{LABELS['de']['tiers_note']}\n{LABELS['en']['tiers_note']}"
for _k in ("vat_note", "valid_text"):            # речення — обома мовами окремими рядками
    LABELS["de_en"][_k] = f"{LABELS['de'][_k]}\n{LABELS['en'][_k]}"
for _k in ("h_description", "h_qty", "h_unit", "h_total", "h_lead"):   # заголовки таблиці: рядок німецькою, рядок англійською
    LABELS["de_en"][_k] = f"{LABELS['de'][_k]}".replace("\n", " ") + "\n" + LABELS["en"][_k].replace("\n", " ")
EUROPEAN = ("de", "uk", "de_en")   # 28.10.2026 (en — 2026-10-28)
GERMAN_NUMBERS = ("de", "de_en", "en")   # 1.789,00 — як на бланку Quotation; uk — 1 789,00
PLAIN_UNITS = ("", "Stk", "Stk.", "pcs", "pc", "шт", "шт.")   # у колонці «Qty, Units» — лише число


def _fmt_money(value, lang) -> str:
    if value in (None, ""):
        return ""
    d = Decimal(str(value)).quantize(Decimal("0.01"))
    return _localize(f"{d:,.2f}", lang)               # 1,234.50 → 1.234,50 / 1 234,50


def _fmt_unit(value, lang) -> str:
    """Ціна за одиницю: щонайменше 2, до 4 знаків (дрібні компоненти)."""
    if value in (None, ""):
        return ""
    d = Decimal(str(value)).normalize()
    places = max(2, min(4, -d.as_tuple().exponent if d.as_tuple().exponent < 0 else 0))
    return _localize(f"{Decimal(str(value)):,.{places}f}", lang)


def _localize(s, lang) -> str:
    if lang in GERMAN_NUMBERS:
        return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return s.replace(",", " ").replace(".", ",") if lang in EUROPEAN else s


def _fmt_qty(value) -> str:
    d = Decimal(str(value)).normalize()
    return format(d, "f").rstrip("0").rstrip(".") if "." in format(d, "f") else format(d, "f")


def _fmt_date(iso, lang) -> str:
    if not iso:
        return ""
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}" if lang in EUROPEAN else f"{y}-{m}-{d}"


def _listing(text):
    """Кілька рядків для docxtpl; порожньо — '' (Listing('') — істина, і «{%p if … %}» показав би порожній блок)."""
    from docxtpl import Listing
    text = (text or "").strip()
    return Listing(text) if text else ""


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
            # різні строки для кількостей — з кількістю: «35 Stk: ab Lager / 100 Stk: 14 Tage»
            leads = [t.get("lead_time") or l.get("lead_time", "") for t in tiers]
            lead_time = Listing("\n".join(f"{_fmt_qty(t['qty'])} {l.get('unit', '')}".strip() + f": {x or '—'}"
                                          for t, x in zip(tiers, leads))) if len(set(leads)) > 1 else l.get("lead_time", "")
        else:
            qty = f"{_fmt_qty(l.get('qty', '0'))} {l.get('unit', '')}".strip()
            unit_price = L["on_request"] if on_request else _fmt_unit(l.get("unit_price"), lang)
            total = L["on_request"] if on_request else _fmt_money(l.get("line_total"), lang)
            lead_time = l.get("lead_time", "")
        lines.append({
            "pos": l.get("position"), "sku": l.get("sku", ""), "description": Listing(l.get("description", "")),
            "qty": qty, "unit_price": unit_price,
            "discount": f"{_fmt_qty(disc)} %" if disc else "",
            "total": total,
            "lead_time": lead_time,
        })
    has_tiers = any(l.get("tiers") for l in snap.get("lines", []))
    shipping = Decimal(snap.get("shipping_cost") or "0")
    # бланк Quotation: кожна кількість ступенів — окрема позиція (35 і 100 — два рядки), як у Commercial Invoice
    rows = []
    for l in snap.get("lines", []):
        desc, sku = (l.get("description") or "").strip(), (l.get("sku") or "").strip()
        if sku and sku.lower() not in desc.lower():
            desc = f"{desc} {sku}".strip()
        disc = Decimal(l.get("discount_percent") or "0")
        if disc:
            desc += f" ({L['discount']} {_fmt_qty(disc)} %)"
        unit = "" if (l.get("unit") or "") in PLAIN_UNITS else f" {l['unit']}"
        for t in (l.get("tiers") or [{"qty": l.get("qty", "0"), "unit_price": l.get("unit_price"),
                                       "line_total": l.get("line_total"), "lead_time": l.get("lead_time", "")}]):
            priced = t.get("unit_price") not in (None, "")
            rows.append({"pos": len(rows) + 1, "description": Listing(desc), "qty": f"{_fmt_qty(t['qty'])}{unit}",
                         "unit_price": _fmt_unit(t["unit_price"], lang) if priced else L["on_request"],
                         "total": _fmt_money(t.get("line_total"), lang) if priced else L["on_request"],
                         "lead_time": t.get("lead_time") or l.get("lead_time", "")})
    customer = snap.get("customer") or {}
    customer_name = customer.get("company") or customer.get("name") or ""
    # адреса без рядків, що вже стоять окремо (назва клієнта, контактна особа)
    address = "\n".join(ln for ln in (snap.get("billing_address") or "").splitlines()
                        if ln.strip() and ln.strip() not in (customer_name, snap.get("contact_name", "")))
    project = snap.get("project") or {}
    phone = snap.get("contact_phone", "")
    head = {k[2:]: Listing(v.format(cur=cur)) for k, v in L.items() if k.startswith("h_")}
    valid = _fmt_date(snap.get("valid_until"), lang)
    return {
        "L": L, "H": head, "currency": cur, "rows": rows, "shipping_pos": len(rows) + 1,
        "billing_line": ", ".join(x for x in [customer_name] + [ln.strip() for ln in address.splitlines()] if x),
        "contact_phone": phone, "contact_email_line": "" if phone else snap.get("contact_email", ""),
        "signer_position": snap.get("signer_position", ""),
        "valid_text": Listing(L["valid_text"].format(date=valid)) if valid else "",
        "payment_info": _listing(s.payment_info), "outro": _listing(snap.get("outro_text")),
        "show_total": not has_tiers and bool(rows),
        "show_vat": not has_tiers and bool(rows) and Decimal(snap.get("vat_rate") or "0") > 0,
        "company_block": Listing(s.company_block or ""), "footer_text": Listing(s.footer_text or ""),
        "number": snap.get("display_number") or snap.get("number", ""),
        "revision": snap.get("revision", 1),
        "date": _fmt_date(snap.get("date"), lang), "valid_until": _fmt_date(snap.get("valid_until"), lang),
        "customer_name": customer_name,
        "billing_address": Listing(address),
        "contact_name": snap.get("contact_name", ""), "contact_email": snap.get("contact_email", ""),
        "signer": snap.get("signer", ""),
        "project": f"{project.get('code', '')} · {project.get('name', '')}" if project else "",
        "intro_text": _listing(snap.get("intro_text")),
        "outro_text": Listing("\n\n".join(t for t in ((L["tiers_note"] if has_tiers else ""),
                                                        snap.get("outro_text", "")) if t)),
        "payment_terms": _listing(snap.get("payment_terms")),
        "delivery_terms": _listing(snap.get("delivery_terms")),
        "lines": lines,
        "has_discount": any(l["discount"] for l in lines),
        "has_lead_time": any(l["lead_time"] for l in lines),
        "has_shipping": shipping > 0, "shipping_cost": _fmt_money(shipping, lang),
        "vat_rate": _fmt_qty(Decimal(snap.get("vat_rate") or "0")),
        "net_total": _fmt_money(snap.get("net_total"), lang), "vat_total": _fmt_money(snap.get("vat_total"), lang),
        "gross_total": _fmt_money(snap.get("gross_total"), lang),
    }


# Довідник змінних шаблону Angebot (як у Commercial Invoice): (змінна, що підставляється, приклад)
TEMPLATE_VARS = [
    ("{{ L.title }}", "Заголовок мовою документа", "Angebot / Quotation"),
    ("{{ L.number }} … {{ L.gross }}", "Усі підписи мовою документа (L.date, L.customer, L.qty, L.total, L.net…)", "Menge / Qty"),
    ("{{ number }}", "Номер Angebot (з версією)", "A-2026-014"),
    ("{{ date }}", "Дата документа", "28.10.2026"),
    ("{{ valid_until }}", "Дійсний до", "27.11.2026"),
    ("{{ customer_name }}", "Клієнт (компанія або ім'я)", "Module ETP LLC"),
    ("{{ billing_address }}", "Адреса (кілька рядків)", "Peremohy Ave 12\n03057 Kyiv, Ukraine"),
    ("{{ contact_name }}", "Контактна особа", "Denys Repetynskyi"),
    ("{{ contact_email }}", "Email контакту", "procurement10@moduletp.com"),
    ("{{ contact_phone }}", "Телефон контакту", "+31 6 21674102"),
    ("{{ billing_line }}", "Адреса одним рядком (Billing address)", "SL-TIRE B.V., Willeskop 180A, 3421 GW Oudewater"),
    ("{{ project }}", "Проєкт (код · назва), може бути порожнім", "P-2026-002 · Indoor positioning"),
    ("{{ intro_text }}", "Вступ (з налаштувань Angebote / форми)", "vielen Dank für Ihre Anfrage…"),
    ("{%tr for l in rows %}", "Бланк Quotation: рядок-маркер таблиці — кожна кількість ступенів окремою позицією; "
     "далі {{ l.pos }} {{ l.description }} {{ l.qty }} {{ l.unit_price }} {{ l.total }} {{ l.lead_time }}", ""),
    ("{{ H.description }} {{ H.qty }} {{ H.unit }} {{ H.total }} {{ H.lead }}", "Заголовки колонок мовою документа",
     "Qty,\nUnits"),
    ("{%tr if show_total %}", "Рядок «Total (net)» — лише без ступенів (ступені — варіанти, суми немає)", ""),
    ("{{ L.vat_note }} / {{ valid_text }}", "«All prices are exclusive of VAT…» / «This quotation is valid until …»",
     "This quotation is valid until 2026-11-28."),
    ("{{ payment_info }}", "Платіжні реквізити з налаштувань Angebote", "Beneficiary: …\nIBAN: …"),
    ("{{ signer }} / {{ signer_position }}", "Підписує й посада (як у Commercial Invoice)", "Dr. Sergey Sevskiy / CEO"),
    ("{%tr for l in lines %}", "Старий шаблон: позиція з усіма ступенями в одних комірках (далі l.sku, l.discount…)", ""),
    ("{{ l.pos }}", "Позиція", "1"),
    ("{{ l.sku }}", "Артикул", "FT220407"),
    ("{{ l.description }}", "Опис", "Filter FT220407"),
    ("{{ l.qty }}", "Кількість — для ступенів кілька рядків", "35 Stk\n100 Stk"),
    ("{{ l.unit_price }}", "Ціна за одиницю (ступені — рядками)", "12,50\n10,90"),
    ("{{ l.discount }}", "Знижка, якщо є", "5 %"),
    ("{{ l.total }}", "Сума позиції (ступені — рядками)", "437,50\n1 090,00"),
    ("{{ l.lead_time }}", "Строк поставки позиції", "4 Wochen"),
    ("{%tr endfor %}", "Рядок-маркер кінця таблиці", ""),
    ("{{ currency }}", "Валюта", "EUR"),
    ("{%p if has_shipping %} … {%p endif %}", "Абзац лише якщо є доставка; {{ shipping_cost }} — сума", "1 000,00"),
    ("{{ net_total }}", "Сума netto (для ступенів — з першої кількості)", "437,50"),
    ("{{ vat_rate }} / {{ vat_total }}", "Ставка й сума ПДВ", "19 / 83,13"),
    ("{{ gross_total }}", "Разом brutto", "520,63"),
    ("{{ payment_terms }}", "Умови оплати", "Zahlbar innerhalb von 14 Tagen…"),
    ("{{ delivery_terms }}", "Умови поставки", "Lieferung DAP…"),
    ("{{ outro_text }}", "Завершення (+ примітка про ступені, якщо є)", "Für Rückfragen stehen wir…"),

    ("{{ company_block }} / {{ footer_text }}", "Реквізити з налаштувань (для шаблону без бланка)", "Sevskiy GmbH…"),
]


def sample_snapshot() -> dict:
    """Тестові дані — перевірити завантажений шаблон (і подивитися, як виглядає документ)."""
    return {
        "number": "A-2026-014", "display_number": "A-2026-014", "revision": 1, "date": "2026-10-28",
        "valid_until": "2026-11-27", "language": "en", "currency": "EUR", "contact_phone": "+380 44 000 00 00",
        "signer_position": "CEO",
        "customer": {"id": None, "name": "Denys Repetynskyi", "company": "Module ETP LLC"},
        "contact_name": "Denys Repetynskyi", "contact_email": "procurement10@moduletp.com",
        "billing_address": "Module ETP LLC\nPeremohy Ave 12\n03057 Kyiv, Ukraine", "signer": "Viacheslav Pryimak",
        "project": {"id": 1, "code": "P-2026-002", "name": "Indoor positioning"},
        "intro_text": "vielen Dank für Ihre Anfrage.", "payment_terms": "Zahlbar innerhalb von 14 Tagen.",
        "delivery_terms": "Lieferung DAP.", "outro_text": "Für Rückfragen stehen wir Ihnen gerne zur Verfügung.",
        "lines": [{"position": 1, "sku": "FT220407", "description": "Filter FT220407", "qty": "35", "unit": "Stk",
                   "unit_price": "12.5", "discount_percent": "0", "line_total": "437.50", "lead_time": "1-2",
                   "tiers": [{"qty": "35", "unit_price": "12.5", "line_total": "437.50", "lead_time": "1-2"},
                             {"qty": "100", "unit_price": "10.9", "line_total": "1090.00", "lead_time": "3-4"}]}],
        "shipping_cost": "30", "vat_rate": "0", "net_total": "467.50", "vat_total": "0", "gross_total": "467.50",
    }


def check_template(data: bytes) -> None:
    """Шаблон рендериться на тестових даних (інакше — виняток з поясненням docxtpl / Jinja)."""
    from docxtpl import DocxTemplate
    tpl = DocxTemplate(BytesIO(data))
    tpl.render(build_context(sample_snapshot()), autoescape=True)


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
