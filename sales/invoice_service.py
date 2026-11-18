"""Рахунки (Rechnung) — уся логіка розділу «🧾 Рахунки» (/rechnungen/).

- номер: замовлення інтернет-магазину — нумерація магазину `shop.numbering.doc_number(order, "CI")`
  (CI-<Kundennummer>-NNN, той самий номер, що Commercial Invoice й кабінет покупця), інакше
  `InvoiceSettings.number_prefix` + номер замовлення; без замовлення — префікс + дата;
- `initial_data(order)` — новий рахунок із замовлення: адреса рахунку з сайту (`sales.payment_info.invoice`),
  доставка, якщо інша, позиції, доставка, ПДВ (`vat_for`: своя країна — з ПДВ, ЄС з USt-IdNr — Reverse Charge,
  поза ЄС — експорт; ставка, яку порахував сайт, має пріоритет), мова (`language_for`), строк і текст оплати за
  способом / станом оплати (`pay_kind`);
- тексти зберігаються з підстановками {number} {date} {due_date} {amount} {order} {customer} {method} —
  заповнюються під час формування (`fill`), тож зміна позицій не ламає суму в тексті;
- `build_context` → `render_docx` (docxtpl на шаблоні: власний `InvoiceSettings.template` → з бланка Commercial
  Invoice `blank_template` → простий) → `generate` (DOCX + PDF, копія в документи замовлення);
- лист: `compose_initial` (вкладення `invoice:<pk>` — email_assistant.attachments), `mark_sent`.
"""
from __future__ import annotations

import logging
import re
import shutil
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from .models_invoice import LANGS, VAT_MODES, InvoiceSettings, SalesInvoice

logger = logging.getLogger(__name__)

GERMAN_COUNTRIES = ("DE", "AT", "CH", "LI")
LANG_CODES = [c for c, _ in LANGS]
VAT_CODES = [c for c, _ in VAT_MODES]
PAY_KINDS = ("invoice", "prepayment", "paid")
UNITS = {"de": "Stk.", "en": "pcs", "de_en": "Stk."}

LABELS = {
    "de": {
        "title": "Rechnung", "number": "Rechnungsnummer", "date": "Rechnungsdatum", "service_date": "Lieferdatum",
        "due_date": "Zahlbar bis", "customer_no": "Kundennummer", "order": "Bestellung", "po": "Ihre Bestellnummer",
        "bill_to": "Rechnungsempfänger", "ship_to": "Lieferadresse", "contact": "Ansprechpartner",
        "seller": "Verkäufer", "vat_id": "USt-IdNr.", "shipping_row": "Verpackung und Versand",
        "total_net": "Gesamtbetrag netto", "vat": "MwSt.", "gross": "Gesamtbetrag brutto",
        "payment_info": "Bankverbindung", "name": "Name", "position": "Position", "part_no": "Art.-Nr.",
        "h_pos": "Pos.", "h_description": "Beschreibung", "h_qty": "Menge", "h_unit": "Einzelpreis",
        "h_total": "Gesamt", "bank": "Bank", "holder": "Kontoinhaber",
    },
    "en": {
        "title": "Invoice", "number": "Invoice No.", "date": "Invoice date", "service_date": "Delivery date",
        "due_date": "Payment due", "customer_no": "Customer No.", "order": "Order", "po": "Your order No.",
        "bill_to": "Invoice to", "ship_to": "Delivery address", "contact": "Contact person",
        "seller": "Seller", "vat_id": "VAT ID", "shipping_row": "Packaging and shipping costs",
        "total_net": "Total net", "vat": "VAT", "gross": "Total gross",
        "payment_info": "Bank details", "name": "Name", "position": "Position", "part_no": "Part No.",
        "h_pos": "Pos.", "h_description": "Description", "h_qty": "Qty", "h_unit": "Unit price",
        "h_total": "Total", "bank": "Bank", "holder": "Account holder",
    },
}
LABELS["de_en"] = {k: (v if v == LABELS["en"][k] else f"{v} / {LABELS['en'][k]}") for k, v in LABELS["de"].items()}
for _k in ("h_description", "h_qty", "h_unit", "h_total"):     # заголовки таблиці: рядок DE, рядок EN
    LABELS["de_en"][_k] = f"{LABELS['de'][_k]}\n{LABELS['en'][_k]}"
METHOD_NAMES = {
    "de": {"invoice": "Rechnung", "prepayment": "Vorkasse", "paypal": "PayPal",
           "stripe": "Kreditkarte / Online (Stripe)", "other": "Sonstige"},
    "en": {"invoice": "invoice", "prepayment": "prepayment", "paypal": "PayPal",
           "stripe": "credit card / online (Stripe)", "other": "other"},
}
TEXT_KEYS = ("intro", "payment", "vat_note", "outro")
PLACEHOLDERS = [
    ("{number}", "номер рахунку"), ("{date}", "дата рахунку"), ("{due_date}", "оплатити до"),
    ("{amount}", "сума до оплати (брутто з валютою)"), ("{order}", "номер замовлення"),
    ("{customer}", "компанія або ім'я покупця"), ("{method}", "спосіб оплати"),
]
_PH_RE = re.compile(r"\{(number|date|due_date|amount|order|customer|method)\}")


class InvoiceError(Exception):
    pass


# ── Правила ──────────────────────────────────────────────────────────────────

def _eu() -> set:
    try:
        from shop.countries import REGION_CODES
        return set(REGION_CODES.get("eu", []))
    except Exception:  # noqa: BLE001
        return set()


def language_for(country: str, s: InvoiceSettings | None = None) -> str:
    s = s or InvoiceSettings.get()
    if s.default_language in LANG_CODES:
        return s.default_language
    return "de" if (country or "").upper() in GERMAN_COUNTRIES else "en"


def vat_for(country: str, vat_id: str, site_rate=None, s: InvoiceSettings | None = None) -> tuple[str, Decimal]:
    """(режим ПДВ, ставка): своя країна — з ПДВ; ЄС з USt-IdNr — Reverse Charge; поза ЄС — експорт.
    Ставку, яку порахував сайт (> 0), бере як є (покупець без USt-IdNr в іншій країні ЄС)."""
    s = s or InvoiceSettings.get()
    rate = Decimal(str(s.vat_rate or 0))
    c, home = (country or "").upper(), (s.home_country or "DE").upper()
    if site_rate is not None:
        try:
            site = Decimal(str(site_rate))
        except (InvalidOperation, ValueError):
            site = None
        if site is not None and site > 0:
            return "domestic", site
    if not c or c == home:
        return "domestic", rate
    if c in _eu():
        return ("reverse_charge" if (vat_id or "").strip() else "domestic"), rate
    return "export", rate


def pay_kind(method: str, status: str) -> str:
    if status == "paid":
        return "paid"
    return "prepayment" if method == "prepayment" else "invoice"


def due_for(kind: str, start: date, s: InvoiceSettings | None = None):
    s = s or InvoiceSettings.get()
    if kind == "paid":
        return None
    return start + timedelta(days=s.prepayment_due_days if kind == "prepayment" else s.due_days)


def standard_texts(lang: str, vat_mode: str, kind: str, s: InvoiceSettings | None = None) -> dict:
    """Тексти за замовчуванням мовою документа (з підстановками)."""
    s = s or InvoiceSettings.get()
    vat_key = {"reverse_charge": "vat_reverse", "export": "vat_export"}.get(vat_mode)
    return {
        "intro_text": s.text("intro", lang),
        "payment_text": s.text(f"pay_{kind if kind in PAY_KINDS else 'invoice'}", lang),
        "vat_note": s.text(vat_key, lang) if vat_key else "",
        "outro_text": s.text("outro", lang),
    }


def all_standard_texts(s: InvoiceSettings | None = None) -> dict:
    """Для редактора (JS): {мова: {ПДВ: {спосіб: тексти}}} — стандартні тексти міняються разом з мовою / ПДВ."""
    s = s or InvoiceSettings.get()
    return {lang: {vm: {k: standard_texts(lang, vm, k, s) for k in PAY_KINDS} for vm in VAT_CODES}
            for lang in LANG_CODES}


def number_for(order=None, s: InvoiceSettings | None = None) -> str:
    """Замовлення магазину — номер магазину (CI-<Kundennummer>-NNN); інше — префікс + номер замовлення."""
    s = s or InvoiceSettings.get()
    if order is not None:
        try:
            from shop.numbering import doc_number
            shop_no = doc_number(order, "CI")
        except Exception as e:  # noqa: BLE001 — без магазину / збій нумерації — як для звичайного замовлення
            logger.warning("invoice number for %s from shop: %s", getattr(order, "pk", None), e)
            shop_no = ""
        if shop_no:
            return shop_no
        return f"{s.number_prefix or ''}{order.order_number}"
    base = f"{s.number_prefix or ''}{timezone.localdate():%Y%m%d}"
    n, number = 1, base
    while SalesInvoice.objects.filter(number=number).exists():
        n += 1
        number = f"{base}-{n}"
    return number


def unique_number(base: str, exclude_pk=None) -> str:
    """Номер, якого ще немає в іншого рахунку: «X», «X-2», «X-3» … (другий рахунок того самого замовлення)."""
    qs = SalesInvoice.objects.exclude(pk=exclude_pk) if exclude_pk else SalesInvoice.objects.all()
    number, n = base[:40], 1
    while qs.filter(number=number).exists():
        n += 1
        number = f"{base[:36]}-{n}"
    return number


def _customer_number(order) -> str:
    try:
        from shop.models import CustomerNumber, ShopOrder
        from shop.numbering import crm_of
        info = ShopOrder.objects.filter(order=order).select_related("shop").first()
        if info is None or info.shop.is_test:
            return ""
        row = CustomerNumber.objects.filter(crm_customer=crm_of(order)).first()
        return str(row.number) if row else ""
    except Exception:  # noqa: BLE001
        return ""


def last_signer() -> tuple:
    try:
        from offers.services import last_signer as _last
        return _last()
    except Exception:  # noqa: BLE001
        return "", ""


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v if v not in (None, "") else 0))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _num(d: Decimal):
    """Для JSON позицій: ціле — int, інакше рядок без зайвих нулів."""
    d = d.normalize()
    return int(d) if d == d.to_integral_value() else format(d, "f")


def order_lines(order) -> list:
    out = []
    for ln in order.lines.select_related("product").order_by("pk"):
        p = ln.product
        qty = _dec(ln.qty)
        unit = _dec(ln.unit_price)
        if not unit and qty:
            unit = (_dec(ln.total_price) / qty).quantize(Decimal("0.0001"))
        out.append({
            "description": (p.name_export if p else "") or (p.name if p else "") or ln.sku_raw or "",
            "part_no": ln.sku_raw or (p.sku if p else ""),
            "qty": _num(qty), "unit": "", "unit_price": _num(unit),
        })
    return out


def country_name(code: str, lang: str) -> str:
    """Назва країни мовою документа (de — німецькою, інакше англійською); невідомий код — як є."""
    try:
        from shop.countries import COUNTRIES
        c = COUNTRIES.get((code or "").upper())
    except Exception:  # noqa: BLE001
        c = None
    return c["de" if lang == "de" else "en"] if c else (code or "")


def _zip_city(zip_code, city) -> str:
    return " ".join(x for x in (zip_code or "", city or "") if x)


def bill_from(order, lang: str) -> tuple[dict, dict, str]:
    """(адреса рахунку, адреса доставки або {}, країна ISO): з сайту (ShopOrder.billing), інакше — замовлення."""
    from . import payment_info
    inv = payment_info.invoice(order)
    billing = getattr(order.shop_info, "billing", None) if _has_shop(order) else None
    if isinstance(billing, dict) and billing:
        country = (billing.get("country") or order.addr_country or "").upper()
        bill = {"company": billing.get("company", ""), "name": billing.get("name", ""),
                "street": billing.get("street", ""), "zip_city": _zip_city(billing.get("zip"), billing.get("city")),
                "country": country_name(country, lang)}
    else:
        country = (order.addr_country or "").upper()
        name = order.contact_name or ""
        bill = {"company": order.client or "", "name": "" if name == (order.client or "") else name,
                "street": order.addr_street or "", "zip_city": _zip_city(order.addr_zip, order.addr_city),
                "country": country_name(country, lang)}
    bill.update({"vat_id": inv["vat_id"], "email": inv["email"]})
    ship = {}
    if inv["separate"]:
        ship = {"company": order.ship_company or "", "name": order.ship_name or "", "street": order.addr_street or "",
                "zip_city": _zip_city(order.addr_zip, order.addr_city),
                "country": country_name(order.addr_country, lang)}
    clean = lambda d: {k: v.strip() for k, v in d.items() if (v or "").strip()}  # noqa: E731
    return clean(bill), clean(ship), country


def initial_data(order=None, s: InvoiceSettings | None = None) -> dict:
    """Поля нового рахунку (без номера — номер видається при збереженні)."""
    s = s or InvoiceSettings.get()
    today = timezone.localdate()
    signer, position = (s.signer_name, s.signer_position) if s.signer_name else last_signer()
    data = {"inv_date": today, "service_date": None, "language": language_for("", s), "currency": "EUR",
            "bill_to": {}, "ship_to": {}, "customer_number": "", "order_ref": "", "customer_po": "", "lines": [],
            "shipping_cost": Decimal("0"), "vat_mode": "domestic", "vat_rate": Decimal(str(s.vat_rate or 0)),
            "payment_method": "", "signer_name": signer or "", "signer_position": position or "",
            "sales_order": order}
    kind = "invoice"
    if order is not None:
        from . import payment_info
        country = (getattr(order.shop_info, "billing", {}) or {}).get("country", "") if _has_shop(order) else ""
        country = (country or order.addr_country or "").upper()
        lang = language_for(country, s)
        bill, ship, country = bill_from(order, lang)
        inv = payment_info.invoice(order)
        mode, rate = vat_for(country, inv["vat_id"], inv["vat_rate"], s)
        currency = ((order.currency or "") or next((ln.currency for ln in order.lines.all() if ln.currency), "")
                    or "EUR").upper()[:3]
        kind = pay_kind(order.payment_method or "", order.payment_status or "")
        data.update({
            "language": lang, "currency": currency, "bill_to": bill, "ship_to": ship,
            "customer_number": _customer_number(order), "order_ref": order.order_number or "",
            "lines": order_lines(order), "shipping_cost": _dec(order.shipping_cost), "vat_mode": mode,
            "vat_rate": rate, "payment_method": order.payment_method or "",
            "service_date": order.shipped_at,
        })
    data["due_date"] = due_for(kind, today, s)
    data.update(standard_texts(data["language"], data["vat_mode"], kind, s))
    return data


def _has_shop(order) -> bool:
    try:
        order.shop_info  # noqa: B018
        return True
    except Exception:  # noqa: BLE001
        return False


def create_from_order(order, user=None) -> SalesInvoice:
    s = InvoiceSettings.get()
    number = unique_number(number_for(order, s))      # спершу номер: нумерація магазину видає й Kundennummer
    data = initial_data(order, s)
    return SalesInvoice.objects.create(number=number, created_by=user, **data)


def quick_from_order(order, user=None) -> tuple[SalesInvoice, bool]:
    """«⚡ Одразу сформувати» з картки замовлення: рахунок замовлення вже є — він (нічого нового), інакше новий із
    даних замовлення + DOCX / PDF. (рахунок, створено). Збій документа — рахунок лишається, виняток іде далі."""
    existing = order.sales_invoices.order_by("-pk").first()
    if existing is not None:
        return existing, False
    inv = create_from_order(order, user)
    generate(inv)
    return inv, True


# ── Документ ─────────────────────────────────────────────────────────────────

def _fmt():
    from offers import document
    return document


def _date(d, lang) -> str:
    return _fmt()._fmt_date(d.isoformat(), lang) if d else ""


def labels(lang: str, currency: str) -> tuple[dict, dict]:
    L = dict(LABELS.get(lang) or LABELS["de"])
    cur = currency or "EUR"
    H = {"pos": L["h_pos"], "description": L["h_description"], "qty": L["h_qty"],
         "unit": "\n".join(f"{x}, {cur}" for x in L["h_unit"].split("\n")),
         "total": "\n".join(f"{x}, {cur}" for x in L["h_total"].split("\n"))}
    return L, H


def method_name(method: str, lang: str) -> str:
    if not method:
        return ""
    if lang == "de_en":
        de, en = METHOD_NAMES["de"].get(method, method), METHOD_NAMES["en"].get(method, method)
        return de if de == en else f"{de} / {en}"
    return METHOD_NAMES["de" if lang == "de" else "en"].get(method, method)


def values(inv: SalesInvoice) -> dict:
    """Значення підстановок {number} {date} … для текстів рахунку й листа."""
    doc, lang = _fmt(), inv.language
    bill = inv.bill_to or {}
    return {
        "number": inv.number, "date": _date(inv.inv_date, lang), "due_date": _date(inv.due_date, lang),
        "amount": f"{doc._fmt_money(inv.gross_total, lang)} {inv.currency}",
        "order": inv.order_ref or (inv.sales_order.order_number if inv.sales_order_id else ""),
        "customer": bill.get("company") or bill.get("name") or "",
        "method": method_name(inv.payment_method, lang),
    }


def fill(text: str, vals: dict) -> str:
    """{number} … → значення; інші фігурні дужки лишаються як є."""
    return _PH_RE.sub(lambda m: str(vals.get(m.group(1), m.group(0))), text or "")


def payment_info_text(s: InvoiceSettings, lang: str) -> str:
    if (s.payment_info or "").strip():
        return s.payment_info.strip()
    try:
        from accounting.models import CompanySettings
        c = CompanySettings.get()
    except Exception:  # noqa: BLE001
        return ""
    L = LABELS.get(lang) or LABELS["de"]
    rows = [(L["holder"], c.legal_name or c.name), (L["bank"], c.bank_name), ("IBAN", c.iban), ("BIC", c.swift)]
    if not c.iban:
        return ""
    return "\n".join(f"{k}: {v}" for k, v in rows if v)


def company_block() -> str:
    try:
        from accounting.models import CompanySettings
        c = CompanySettings.get()
    except Exception:  # noqa: BLE001
        return ""
    city = " ".join(x for x in (c.addr_zip, c.addr_city) if x)
    parts = [c.legal_name or c.name, c.addr_street, city, c.vat_id and f"USt-IdNr.: {c.vat_id}", c.email, c.phone]
    return " · ".join(x for x in parts if x)


def rows(inv: SalesInvoice) -> list:
    doc, lang = _fmt(), inv.language
    L = LABELS.get(lang) or LABELS["de"]
    out = []
    for i, line in enumerate(inv.lines or [], 1):
        qty, unit = _dec(line.get("qty")), _dec(line.get("unit_price"))
        desc = (line.get("description") or "").strip()
        part = (line.get("part_no") or "").strip()
        if part and part not in desc:
            desc = f"{desc}\n{L['part_no']}: {part}" if desc else part
        u = (line.get("unit") or "").strip()
        q = doc._fmt_qty(qty)
        out.append({"pos": i, "description": doc._listing(desc), "qty": f"{q} {u}" if u else q,
                    "unit_price": doc._fmt_unit(unit, lang),
                    "total": doc._fmt_money((qty * unit).quantize(Decimal("0.01")), lang)})
    return out


def build_context(inv: SalesInvoice, s: InvoiceSettings | None = None) -> dict:
    s = s or InvoiceSettings.get()
    doc, lang = _fmt(), inv.language if inv.language in LANG_CODES else "de"
    L, H = labels(lang, inv.currency)
    vals = values(inv)
    bill = {k: (inv.bill_to or {}).get(k, "") for k in ("company", "name", "street", "zip_city", "country",
                                                         "vat_id", "email")}
    bill["vat_line"] = f"{L['vat_id']}: {bill['vat_id']}" if bill["vat_id"] else ""
    ship = inv.ship_to or {}
    ship_line = ", ".join(x for x in (ship.get(k, "") for k in ("company", "name", "street", "zip_city", "country")) if x)
    items = rows(inv)
    ship_cost = Decimal(inv.shipping_cost or 0)
    shipping = {"pos": len(items) + 1, "description": L["shipping_row"], "qty": "1",
                "unit_price": doc._fmt_money(ship_cost, lang), "total": doc._fmt_money(ship_cost, lang)}
    bill_block = "\n".join(x for x in (bill["company"], bill["name"], bill["street"], bill["zip_city"],
                                       bill["country"], bill["vat_line"]) if x)
    rate = inv.effective_vat_rate.normalize()
    return {
        "L": L, "H": H, "lang": lang, "currency": inv.currency,
        "number": inv.number, "date": vals["date"], "service_date": _date(inv.service_date, lang),
        "due_date": vals["due_date"], "customer_number": inv.customer_number, "order_ref": inv.order_ref,
        "customer_po": inv.customer_po,
        "bill": bill, "bill_block": doc._listing(bill_block), "ship_line": ship_line,
        "rows": items, "all_rows": items + ([shipping] if ship_cost else []),
        "has_shipping": bool(ship_cost), "shipping_pos": len(items) + 1,
        "shipping_cost": doc._fmt_money(ship_cost, lang),
        "goods_total": doc._fmt_money(inv.goods_total, lang), "net_total": doc._fmt_money(inv.net_total, lang),
        "vat_rate": doc._localize(format(rate, "f"), lang) if rate != rate.to_integral_value() else str(int(rate)),
        "vat_total": doc._fmt_money(inv.vat_total, lang), "gross_total": doc._fmt_money(inv.gross_total, lang),
        "intro": doc._listing(fill(inv.intro_text, vals)),
        "payment_text": doc._listing(fill(inv.payment_text, vals)),
        "vat_note": doc._listing(fill(inv.vat_note, vals)),
        "outro": doc._listing(fill(inv.outro_text, vals)),
        "payment_info": doc._listing(payment_info_text(s, lang)),
        "signer": inv.signer_name, "signer_position": inv.signer_position,
        "company_block": company_block(),
    }


# ── Шаблон ───────────────────────────────────────────────────────────────────

TEMPLATE_DIR = "invoice_templates"
TEMPLATE_VARS = [
    ("{{ number }}", "номер рахунку"), ("{{ date }}", "дата рахунку"), ("{{ service_date }}", "дата поставки"),
    ("{{ due_date }}", "оплатити до"), ("{{ customer_number }}", "Kundennummer"),
    ("{{ order_ref }}", "номер замовлення"), ("{{ customer_po }}", "номер замовлення клієнта"),
    ("{{ bill.company }} {{ bill.name }} {{ bill.street }} {{ bill.zip_city }} {{ bill.country }}", "адреса рахунку"),
    ("{{ bill.vat_line }}", "«USt-IdNr.: …» або порожньо"), ("{{ bill.email }}", "email покупця"),
    ("{{ ship_line }}", "адреса доставки одним рядком (порожньо — та сама)"),
    ("{%tr for l in rows %} … {%tr endfor %}", "позиції: l.pos, l.description, l.qty, l.unit_price, l.total"),
    ("{{ all_rows }}", "позиції разом із рядком доставки (для простого шаблону)"),
    ("{{ shipping_cost }} / {{ has_shipping }}", "доставка"),
    ("{{ net_total }} {{ vat_rate }} {{ vat_total }} {{ gross_total }} {{ currency }}", "суми"),
    ("{{ intro }} {{ payment_text }} {{ vat_note }} {{ outro }}", "тексти (підстановки вже заповнені)"),
    ("{{ payment_info }}", "банківські реквізити"), ("{{ signer }} / {{ signer_position }}", "підписант"),
    ("{{ L.title }}, {{ L.number }} … {{ H.description }} …", "підписи мовою документа"),
]


def blank_template() -> str:
    """Шаблон на бланку Commercial Invoice; перебудовується, коли бланк змінився або змінилась побудова."""
    from . import invoice_template
    try:
        from shipping.services.commercial_invoice_service import active_template_path
        blank = Path(active_template_path())
    except ImportError:
        return ""
    if not blank.exists():
        return ""
    out = Path(settings.MEDIA_ROOT) / TEMPLATE_DIR / f"from_ci_v{invoice_template.VERSION}.docx"
    try:
        if not out.exists() or out.stat().st_mtime < blank.stat().st_mtime:
            out.parent.mkdir(parents=True, exist_ok=True)
            invoice_template.build(out, blank=blank)
        return str(out)
    except Exception as e:  # noqa: BLE001 — запасний простий шаблон
        logger.warning("invoice template from CI blank: %s", e)
        return ""


def simple_template() -> str:
    from . import invoice_template
    out = Path(settings.MEDIA_ROOT) / TEMPLATE_DIR / f"simple_v{invoice_template.VERSION}.docx"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        invoice_template.build(out)
    return str(out)


def template_kind(s: InvoiceSettings | None = None) -> str:
    s = s or InvoiceSettings.get()
    if s.template:
        try:
            if Path(s.template.path).exists():
                return "custom"
        except (ValueError, NotImplementedError):
            pass
    return "blank" if blank_template() else "simple"


def template_path(s: InvoiceSettings | None = None) -> str:
    s = s or InvoiceSettings.get()
    if template_kind(s) == "custom":
        return s.template.path
    return blank_template() or simple_template()


def _render(path, ctx) -> bytes:
    from docxtpl import DocxTemplate
    tpl = DocxTemplate(path)
    tpl.render(ctx, autoescape=True)
    buf = BytesIO()
    tpl.save(buf)
    return buf.getvalue()


def render_docx(inv: SalesInvoice) -> bytes:
    s = InvoiceSettings.get()
    return _render(template_path(s), build_context(inv, s))


def sample_invoice() -> SalesInvoice:
    """Приклад (не зберігається) — для перегляду шаблону й перевірки завантаженого."""
    s = InvoiceSettings.get()
    inv = SalesInvoice(
        number="CI-31728-001", inv_date=timezone.localdate(), service_date=timezone.localdate(),
        due_date=timezone.localdate() + timedelta(days=s.due_days), language="de", currency="EUR",
        bill_to={"company": "Muster Elektronik GmbH", "name": "Max Mustermann", "street": "Hauptstraße 1",
                 "zip_city": "10115 Berlin", "country": "Deutschland", "vat_id": "DE123456789",
                 "email": "einkauf@example.com"},
        ship_to={"company": "Muster Elektronik GmbH", "street": "Lagerweg 5", "zip_city": "12345 Potsdam",
                 "country": "Deutschland"},
        customer_number="31728", order_ref="PO-31728-001", customer_po="4500012345",
        lines=[{"description": "Antenne 2.4 GHz", "part_no": "AN220213-01C", "qty": 10, "unit_price": "12.5"},
               {"description": "Filter 2.7–3.65 GHz", "part_no": "FT220407", "qty": 2, "unit_price": "48.9"}],
        shipping_cost=Decimal("12.90"), vat_mode="domestic", vat_rate=Decimal("19"), payment_method="invoice",
        signer_name=s.signer_name or "Max Muster", signer_position=s.signer_position or "Geschäftsführer")
    for k, v in standard_texts("de", "domestic", "invoice", s).items():
        setattr(inv, k, v)
    return inv


def check_template(data: bytes) -> None:
    """Шаблон рендериться на прикладі (інакше — виняток з поясненням docxtpl / Jinja)."""
    from docxtpl import DocxTemplate
    tpl = DocxTemplate(BytesIO(data))
    tpl.render(build_context(sample_invoice()), autoescape=True)


def file_base(inv: SalesInvoice, s: InvoiceSettings | None = None) -> str:
    s = s or InvoiceSettings.get()
    name = (s.file_name or "Rechnung_{number}").replace("{number}", inv.number).replace("{order}", inv.order_ref or "")
    name = re.sub(r"[^\w.\-]+", "_", name).strip("._") or f"Rechnung_{inv.pk}"
    return name[:120]


def generate(inv: SalesInvoice) -> SalesInvoice:
    """DOCX (+ PDF, якщо є LibreOffice); копія в документи замовлення (media/orders/{джерело}/{номер}/)."""
    from documents.service import _convert_to_pdf
    s = InvoiceSettings.get()
    data = render_docx(inv)
    base = file_base(inv, s)
    for f in (inv.docx_file, inv.pdf_file):
        if f:
            f.delete(save=False)
    inv.docx_file.save(f"{base}.docx", ContentFile(data), save=False)
    pdf = None
    try:
        pdf = _convert_to_pdf(data)
    except Exception as e:  # noqa: BLE001 — без LibreOffice — лише DOCX
        logger.warning("invoice %s: PDF failed: %s", inv.number, e)
    if pdf:
        inv.pdf_file.save(f"{base}.pdf", ContentFile(pdf), save=False)
    inv.save(update_fields=["docx_file", "pdf_file", "updated_at"])
    if inv.sales_order_id:
        copy_to_order(inv)
    return inv


def copy_to_order(inv: SalesInvoice) -> None:
    order = inv.sales_order
    dest = Path(settings.MEDIA_ROOT) / "orders" / (order.source or "manual") / (order.order_number or "")
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for f in (inv.pdf_file, inv.docx_file):
            if f:
                shutil.copyfile(f.path, dest / Path(f.name).name)
    except OSError as e:
        logger.warning("invoice %s: copy to order folder failed: %s", inv.number, e)


# ── Лист ─────────────────────────────────────────────────────────────────────

def attachment(pk) -> dict | None:
    inv = SalesInvoice.objects.filter(pk=pk).first()
    if inv is None:
        return None
    f = inv.pdf_file or inv.docx_file
    if not f:
        return None
    with f.open("rb") as fh:
        content = fh.read()
    ext = "pdf" if inv.pdf_file else "docx"
    ctype = "application/pdf" if ext == "pdf" else \
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return {"name": f"{file_base(inv)}.{ext}", "content": content, "content_type": ctype}


def compose_initial(pk, user=None) -> dict:
    """Початкові дані листа: кому (email рахунку / замовлення), тема й текст мовою рахунку, PDF вкладенням."""
    import html
    from django.shortcuts import get_object_or_404
    inv = get_object_or_404(SalesInvoice, pk=pk)
    if not (inv.pdf_file or inv.docx_file) and (user is None or user.has_perm("sales.change_salesinvoice")):
        generate(inv)
    s = InvoiceSettings.get()
    vals = values(inv)
    lang = inv.language if inv.language in LANG_CODES else "de"
    subject = fill(s.text("email_subject", "de" if lang == "de_en" else lang), vals)
    if lang == "de_en":
        subject = " / ".join(x for x in (fill(s.email_subject_de, vals), fill(s.email_subject_en, vals)) if x)
    body = fill(s.text("email_body", lang), vals)
    att = attachment(inv.pk)
    to = (inv.bill_to or {}).get("email") or (inv.sales_order.email if inv.sales_order_id else "") or ""
    return {"to": to, "subject": subject, "body_html": html.escape(body).replace("\n", "<br>"),
            "att_refs": [{"ref": f"invoice:{inv.pk}", "name": att["name"]}] if att else []}


def mark_sent(pk, to: str = "") -> None:
    SalesInvoice.objects.filter(pk=pk).update(sent_at=timezone.now(), sent_to=(to or "")[:255])


def mark_sent_refs(refs, to: str = "", user=None) -> int:
    """Лист із рахунком («invoice:<pk>» у вкладеннях) надіслано → «надіслано» в рахунку (лише з правом змінювати)."""
    if user is not None and not user.has_perm("sales.change_salesinvoice"):
        return 0
    n = 0
    for ref in refs or []:
        kind, _, pk = str(ref).partition(":")
        if kind == "invoice" and pk.isdigit():
            mark_sent(int(pk), to)
            n += 1
    return n


# ── Форма редактора ──────────────────────────────────────────────────────────

def parse_lines(raw) -> list:
    """Позиції з форми (JSON) → чистий список: опис / артикул — текст, к-сть і ціна — числа ≥ 0."""
    import json
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        raise InvoiceError("Позиції: не вдалося прочитати дані форми.")
    out = []
    for i, line in enumerate(data or [], 1):
        if not isinstance(line, dict):
            continue
        desc = str(line.get("description") or "").strip()[:500]
        part = str(line.get("part_no") or "").strip()[:80]
        if not desc and not part:
            continue
        qty, price = _dec(str(line.get("qty") or "0").replace(",", ".")), \
            _dec(str(line.get("unit_price") or "0").replace(",", "."))
        if qty < 0 or price < 0:
            raise InvoiceError(f"Позиція {i}: кількість і ціна не можуть бути від'ємними.")
        out.append({"description": desc, "part_no": part, "qty": _num(qty),
                    "unit": str(line.get("unit") or "").strip()[:10], "unit_price": _num(price)})
    return out


ADDRESS_KEYS = ("company", "name", "street", "zip_city", "country", "vat_id", "email")


def parse_address(post, prefix: str) -> dict:
    return {k: (post.get(f"{prefix}_{k}") or "").strip()[:200] for k in ADDRESS_KEYS
            if (post.get(f"{prefix}_{k}") or "").strip()}


def _date_in(v):
    from django.utils.dateparse import parse_date
    try:
        return parse_date(v) if v else None
    except ValueError:
        return None


def save_form(inv: SalesInvoice, post) -> SalesInvoice:
    """Зберегти поля редактора (номер не змінюється; змінити — окремо, якщо вільний)."""
    number = (post.get("number") or "").strip()[:40]
    if number and number != inv.number:
        if SalesInvoice.objects.filter(number=number).exclude(pk=inv.pk).exists():
            raise InvoiceError(f"Номер «{number}» уже має інший рахунок.")
        inv.number = number
    inv.inv_date = _date_in(post.get("inv_date")) or inv.inv_date or timezone.localdate()
    inv.service_date = _date_in(post.get("service_date"))
    inv.due_date = _date_in(post.get("due_date"))
    lang = post.get("language") or inv.language
    inv.language = lang if lang in LANG_CODES else "de"
    inv.currency = ((post.get("currency") or inv.currency or "EUR").strip().upper())[:3]
    inv.bill_to = parse_address(post, "bill")
    inv.ship_to = parse_address(post, "ship")
    for f in ("customer_number", "order_ref", "customer_po"):
        setattr(inv, f, (post.get(f) or "").strip()[:60])
    if not inv.customer_number and inv.sales_order_id:   # Kundennummer видається разом із номером магазину
        inv.customer_number = _customer_number(inv.sales_order)
    inv.lines = parse_lines(post.get("lines") or "[]")
    inv.shipping_cost = max(Decimal("0"), _dec(str(post.get("shipping_cost") or "0").replace(",", ".")))
    vm = post.get("vat_mode") or inv.vat_mode
    inv.vat_mode = vm if vm in VAT_CODES else "domestic"
    rate = _dec(str(post.get("vat_rate") or "0").replace(",", "."))
    if rate < 0 or rate > 100:
        raise InvoiceError("Ставка ПДВ — від 0 до 100 %.")
    inv.vat_rate = rate
    inv.payment_method = (post.get("payment_method") or "").strip()[:20]
    for f in ("intro_text", "payment_text", "vat_note", "outro_text"):
        setattr(inv, f, (post.get(f) or "").strip())
    inv.signer_name = (post.get("signer_name") or "").strip()[:120]
    inv.signer_position = (post.get("signer_position") or "").strip()[:120]
    inv.save()
    return inv
