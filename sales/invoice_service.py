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
PAY_KINDS = ("invoice", "prepayment", "cod", "paid")
NO_BANK = ("paid", "cod")                    # уже оплачено / платить кур'єру — без банківських реквізитів
UNITS = {"de": "Stk.", "en": "pcs", "de_en": "Stk."}

LABELS = {
    "de": {
        "title": "Rechnung", "number": "Rechnungsnummer", "date": "Rechnungsdatum", "service_date": "Leistungsdatum",
        "due_date": "Zahlbar bis", "customer_no": "Kundennummer", "order": "Bestellnummer", "po": "Ihre Bestellnummer",
        "ship_to": "Lieferadresse", "same_address": "wie Rechnungsadresse", "contact": "Ansprechpartner",
        "shipping_row": "Versandkosten", "total_net": "Gesamtsumme netto", "vat": "Umsatzsteuer",
        "gross": "Gesamtsumme brutto", "regards": "Mit freundlichen Grüßen",
        "h_pos": "Pos.", "h_description": "Beschreibung", "h_qty": "Menge", "h_unit": "Einzelpreis",
        "h_total": "Betrag", "bank": "Bank", "holder": "Kontoinhaber",
        "attn": "z. Hd.", "mr": "Herrn", "ms": "Frau", "dear_mr": "Sehr geehrter Herr {name},",
        "dear_ms": "Sehr geehrte Frau {name},", "dear": "Sehr geehrte Damen und Herren,",
        "tel": "Tel.", "fax": "Fax", "mob": "Mob", "email": "E-Mail", "web": "Internet", "tax_no": "St.-Nr.",
        "vat_id": "USt-IdNr.", "seat": "Firmensitz", "court": "Registergericht", "ceo": "Geschäftsführer",
        "office": "Büro in", "page": "Seite",
    },
    "en": {
        "title": "Invoice", "number": "Invoice No.", "date": "Invoice date", "service_date": "Date of supply",
        "due_date": "Payment due", "customer_no": "Customer No.", "order": "Order No.", "po": "Your order No.",
        "ship_to": "Delivery address", "same_address": "same as invoice address", "contact": "Contact person",
        "shipping_row": "Shipping costs", "total_net": "Total net", "vat": "VAT", "gross": "Total gross",
        "regards": "Kind regards",
        "h_pos": "Pos.", "h_description": "Description", "h_qty": "Qty", "h_unit": "Unit price",
        "h_total": "Amount", "bank": "Bank", "holder": "Account holder",
        "attn": "Attn.", "mr": "Mr", "ms": "Ms", "dear_mr": "Dear Mr {name},", "dear_ms": "Dear Ms {name},",
        "dear": "Dear Sir or Madam,",
        "tel": "Tel.", "fax": "Fax", "mob": "Mob.", "email": "Email", "web": "Web", "tax_no": "Tax No.",
        "vat_id": "VAT ID", "seat": "Registered office", "court": "Registration court", "ceo": "Managing director",
        "office": "Office in", "page": "Page",
    },
}
# «Deutsch + English»: підписи «DE / EN»; адреса, звертання до людини й колонтитул — німецькою (звертання — обома)
LABELS["de_en"] = {k: (v if v == LABELS["en"][k] else f"{v} / {LABELS['en'][k]}") for k, v in LABELS["de"].items()}
for _k in ("attn", "mr", "ms", "dear_mr", "dear_ms", "dear", "tel", "fax", "mob", "email", "web", "seat", "court",
           "ceo", "office", "same_address"):
    LABELS["de_en"][_k] = LABELS["de"][_k]
SALUTATIONS = [("", "—"), ("mr", "Herr / Mr"), ("ms", "Frau / Ms")]
NO_ORDER_INTRO = {"de": "wir berechnen Ihnen die folgenden Positionen:",
                  "en": "We invoice you for the following items:"}
METHOD_NAMES = {
    "de": {"invoice": "Rechnung", "prepayment": "Vorkasse", "cod": "Nachnahme", "paypal": "PayPal",
           "stripe": "Kreditkarte / Online (Stripe)", "other": "Sonstige"},
    "en": {"invoice": "invoice", "prepayment": "prepayment", "cod": "cash on delivery", "paypal": "PayPal",
           "stripe": "credit card / online (Stripe)", "other": "other"},
}
TEXT_KEYS = ("intro", "payment", "vat_note", "outro")
PLACEHOLDERS = [
    ("{number}", "номер рахунку"), ("{date}", "дата рахунку"), ("{due_date}", "оплатити до"),
    ("{amount}", "сума до оплати (брутто з валютою)"), ("{order}", "номер замовлення"),
    ("{customer}", "компанія або ім'я покупця"), ("{method}", "спосіб оплати"),
    ("{salutation}", "звертання: «Sehr geehrter Herr …,» / «Dear Mr …,»"), ("{signer}", "підписант"),
    ("{company}", "назва вашої фірми"),
]
_PH_RE = re.compile(r"\{(number|date|due_date|amount|order|customer|method|salutation|signer|company)\}")
ONLINE_METHODS = ("stripe", "paypal")       # рахунок автоматично — лише коли оплату отримано
BACKGROUND = True                            # тести вимикають — автоматичний лист надсилається одразу
AUTO_LOCK = "invoice-auto:{pk}"
AUTO_LOG_MAX = 15


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
    return method if method in ("prepayment", "cod") else "invoice"


def due_for(kind: str, start: date, s: InvoiceSettings | None = None):
    """Оплатити до: за рахунком — через `due_days`; передоплата — лише коли текст передоплати має {due_date}
    («vorab» без дати — як у зразку); уже оплачено — без строку."""
    s = s or InvoiceSettings.get()
    if kind in ("paid", "cod"):
        return None
    if kind == "prepayment":
        texts = (s.pay_prepayment_de or "") + (s.pay_prepayment_en or "")
        return start + timedelta(days=s.prepayment_due_days) if "{due_date}" in texts else None
    return start + timedelta(days=s.due_days)


def standard_texts(lang: str, vat_mode: str, kind: str, s: InvoiceSettings | None = None,
                   has_order: bool = True) -> dict:
    """Тексти за замовчуванням мовою документа (з підстановками). Без замовлення — вступ без «{order}»."""
    s = s or InvoiceSettings.get()
    vat_key = {"reverse_charge": "vat_reverse", "export": "vat_export"}.get(vat_mode)
    intro = s.text("intro", lang)
    if not has_order and "{order}" in intro:
        intro = "\n".join(NO_ORDER_INTRO[x] for x in (("de", "en") if lang == "de_en" else (lang,)))
    return {
        "intro_text": intro,
        "payment_text": s.text(f"pay_{kind if kind in PAY_KINDS else 'invoice'}", lang),
        "vat_note": s.text(vat_key, lang) if vat_key else "",
        "outro_text": s.text("outro", lang),
    }


def all_standard_texts(s: InvoiceSettings | None = None, has_order: bool = True) -> dict:
    """Для редактора (JS): {мова: {ПДВ: {спосіб: тексти}}} — стандартні тексти міняються разом з мовою / ПДВ."""
    s = s or InvoiceSettings.get()
    return {lang: {vm: {k: standard_texts(lang, vm, k, s, has_order) for k in PAY_KINDS} for vm in VAT_CODES}
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
            "sales_order": order, "pay_kind": "invoice"}
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
    data["pay_kind"] = kind
    data.update(standard_texts(data["language"], data["vat_mode"], kind, s, has_order=order is not None))
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
    from offers.document import _listing
    L = dict(LABELS.get(lang) or LABELS["de"])
    cur = currency or "EUR"

    def two(key):   # «Einzelpreis,» / «EUR» — двома рядками; «Deutsch + English» — «Einzelpreis, EUR» / «Unit price, EUR»
        parts = L[key].split(" / ")
        return _listing("\n".join(f"{x}, {cur}" for x in parts) if len(parts) > 1 else f"{parts[0]},\n{cur}")
    H = {"pos": L["h_pos"], "description": _listing(L["h_description"].replace(" / ", "\n")),
         "qty": _listing(L["h_qty"].replace(" / ", "\n")), "unit": two("h_unit"), "total": two("h_total")}
    return L, H


def method_name(method: str, lang: str) -> str:
    if not method:
        return ""
    if lang == "de_en":
        de, en = METHOD_NAMES["de"].get(method, method), METHOD_NAMES["en"].get(method, method)
        return de if de == en else f"{de} / {en}"
    return METHOD_NAMES["de" if lang == "de" else "en"].get(method, method)


def values(inv: SalesInvoice, lang: str | None = None) -> dict:
    """Значення підстановок {number} {date} … для текстів рахунку й листа (lang — мова частини тексту)."""
    doc, lang = _fmt(), lang or inv.language
    bill = inv.bill_to or {}
    c = _company()
    return {
        "number": inv.number, "date": _date(inv.inv_date, lang), "due_date": _date(inv.due_date, lang),
        "amount": f"{doc._fmt_money(inv.gross_total, lang)} {inv.currency}",
        "order": inv.order_ref or (inv.sales_order.order_number if inv.sales_order_id else ""),
        "customer": bill.get("company") or bill.get("name") or "",
        "method": method_name(inv.payment_method, lang),
        "salutation": salutation(bill, lang if lang in LABELS else "de"),
        "signer": inv.signer_name or _ceo(c),
        "company": (c.legal_name or c.name) if c is not None else "",
    }


def fill(text: str, vals: dict) -> str:
    """{number} … → значення; інші фігурні дужки лишаються як є."""
    return _PH_RE.sub(lambda m: str(vals.get(m.group(1), m.group(0))), text or "")


def _company():
    try:
        from accounting.models import CompanySettings
        return CompanySettings.get()
    except Exception:  # noqa: BLE001
        return None


def iban_groups(iban: str) -> str:
    raw = re.sub(r"\s+", "", iban or "")
    return " ".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def bank_rows(s: InvoiceSettings, lang: str) -> list:
    """Банківські реквізити рядками {label, value}: «Банківські реквізити» з налаштувань рахунків (рядок
    «Назва: значення»), інакше — з налаштувань компанії (Kontoinhaber, IBAN групами по 4, BIC, Bank)."""
    L = LABELS.get(lang) or LABELS["de"]
    if (s.payment_info or "").strip():
        out = []
        for line in s.payment_info.strip().splitlines():
            label, sep, value = line.partition(":")
            out.append({"label": f"{label.strip()}:", "value": value.strip()} if sep and value.strip()
                       else {"label": "", "value": line.strip()})
        return [r for r in out if r["value"] or r["label"]]
    c = _company()
    if c is None or not c.iban:
        return []
    rows = [(L["holder"], c.legal_name or c.name), ("IBAN", iban_groups(c.iban)), ("BIC", c.swift),
            (L["bank"], c.bank_name)]
    return [{"label": f"{k}:", "value": v} for k, v in rows if v]


def _ceo(c) -> str:
    return (c.ceo_name or "").split(",")[0].strip() if c is not None else ""


def seller(lang: str) -> dict:
    """Дані фірми праворуч і рядок відправника над адресою — з «Бухгалтерія → Налаштування компанії»."""
    c = _company()
    L = LABELS.get(lang) or LABELS["de"]
    if c is None:
        return {"name": "", "return_line": "", "phones": [], "web": [], "tax": []}
    name = c.legal_name or c.name
    zip_city = " ".join(x for x in (c.addr_zip, c.addr_city) if x)
    country = country_name(c.addr_country, "de" if lang == "de_en" else lang)

    def pairs(items):
        return [{"label": f"{k}:", "value": v} for k, v in items if v]
    return {
        "name": name, "street": c.addr_street, "zip_city": zip_city, "country": country,
        "return_line": ", ".join(x for x in (name, c.addr_street, zip_city, country) if x),
        "phones": pairs([(L["tel"], c.phone), (L["fax"], c.fax), (L["mob"], c.mobile)]),
        "web": pairs([(L["email"], c.email), (L["web"], c.website)]),
        "tax": pairs([(L["tax_no"], c.tax_id), (L["vat_id"], c.vat_id)]),
    }


def footer(lang: str) -> dict:
    """Нижній колонтитул — три колонки: фірма й реєстрація · офіс · банк."""
    from offers.document import _listing
    c = _company()
    L = LABELS.get(lang) or LABELS["de"]
    if c is None:
        return {"c1": "", "c2": "", "c3": ""}
    name, city = c.legal_name or c.name, c.addr_city
    ceo = _ceo(c)
    zip_city = " ".join(x for x in (c.addr_zip, c.addr_city) if x)
    cols = [
        [f"{name}, {L['seat']}: {city}" if city else name,
         f"{L['court']}: {c.registration_court}" if c.registration_court else "",
         f"{L['ceo']}: {ceo}" if ceo else ""],
        [f"{L['office']} {city}" if city else "", c.addr_street, zip_city],
        [f"{L['bank']}: {c.bank_name}" if c.bank_name else "", f"IBAN: {c.iban}" if c.iban else "",
         f"BIC / SWIFT: {c.swift}" if c.swift else ""],
    ]
    return {f"c{i}": _listing("\n".join(x for x in col if x)) for i, col in enumerate(cols, 1)}


COMPANY_FIELDS = [("legal_name", "Юридична назва"), ("addr_street", "Адреса"), ("addr_zip", "Поштовий індекс"),
                  ("addr_city", "Місто"), ("addr_country", "Країна (ISO 2)"), ("phone", "Телефон (Tel)"),
                  ("email", "Email"), ("vat_id", "VAT ID / ІПН"), ("tax_id", "TAX ID (Steuernummer)"), ("iban", "IBAN"),
                  ("swift", "SWIFT/BIC"), ("bank_name", "Банк"), ("registration_court", "Реєстраційний суд"),
                  ("ceo_name", "Підписант (для рахунків)")]


def company_missing() -> list:
    """Поля «Налаштувань компанії», які рахунок показує, але вони порожні (підказка на сторінці рахунків)."""
    c = _company()
    if c is None:
        return [label for _, label in COMPANY_FIELDS]
    return [label for field, label in COMPANY_FIELDS
            if not (getattr(c, field, "") or (field == "legal_name" and c.name))]


def address_view(bill: dict, lang: str) -> dict:
    """Адреса покупця для листа: перший рядок (компанія або ім'я), «z. Hd. Herrn …», USt-IdNr."""
    L = LABELS.get(lang) or LABELS["de"]
    company, name = (bill.get("company") or "").strip(), (bill.get("name") or "").strip()
    title = {"mr": L["mr"], "ms": L["ms"]}.get(bill.get("salutation") or "", "")
    attn = f"{L['attn']} {title + ' ' if title else ''}{name}" if company and name else ""
    out = {k: (bill.get(k) or "") for k in ("street", "zip_city", "country", "vat_id", "email")}
    out.update({"company": company, "name": name, "line1": company or name, "attn": attn,
                "vat_line": f"{L['vat_id']}: {bill['vat_id']}" if bill.get("vat_id") else ""})
    return out


def salutation(bill: dict, lang: str) -> str:
    """«Sehr geehrter Herr Bartl,» / «Sehr geehrte Frau …,» / «Sehr geehrte Damen und Herren,»; «Deutsch + English» —
    обидва рядки."""
    name = (bill.get("name") or "").strip()
    last = name.split()[-1] if name else ""
    sal = bill.get("salutation") or ""

    def one(code):
        L = LABELS[code]
        if last and sal in ("mr", "ms"):
            return L[f"dear_{sal}"].format(name=last)
        return L["dear"]
    return "\n".join(one(x) for x in (("de", "en") if lang == "de_en" else (lang if lang in LABELS else "de",)))


def rich(text: str, vals: dict, bold=("order",)):
    """Текст з підстановками як RichText: значення {order} — жирним (як «gemäß Ihrer Bestellung **PO-…**»)."""
    from docxtpl import RichText
    text = text or ""
    rt, pos = RichText(), 0
    for m in _PH_RE.finditer(text):
        if m.start() > pos:
            rt.add(text[pos:m.start()])
        rt.add(str(vals.get(m.group(1), m.group(0))), bold=m.group(1) in bold)
        pos = m.end()
    if pos < len(text):
        rt.add(text[pos:])
    return rt


def rows(inv: SalesInvoice) -> list:
    """Позиції таблиці: опис і артикул одним рядком («Logarithmisch-periodische Antenne AN220201-04A»)."""
    doc, lang = _fmt(), inv.language
    out = []
    for i, line in enumerate(inv.lines or [], 1):
        qty, unit = _dec(line.get("qty")), _dec(line.get("unit_price"))
        desc = (line.get("description") or "").strip()
        part = (line.get("part_no") or "").strip()
        if part and part not in desc:
            desc = f"{desc} {part}" if desc else part
        u = (line.get("unit") or "").strip()
        q = doc._fmt_qty(qty)
        out.append({"pos": i, "description": doc._listing(desc), "qty": f"{q} {u}" if u else q,
                    "unit_price": doc._fmt_unit(unit, lang),
                    "total": doc._fmt_money((qty * unit).quantize(Decimal("0.01")), lang)})
    return out


def build_context(inv: SalesInvoice, s: InvoiceSettings | None = None) -> dict:
    from docxtpl import RichText
    s = s or InvoiceSettings.get()
    doc, lang = _fmt(), inv.language if inv.language in LANG_CODES else "de"
    L, H = labels(lang, inv.currency)
    vals = values(inv)
    ship = inv.ship_to or {}
    ship_line = ", ".join(x for x in (ship.get(k, "") for k in ("company", "name", "street", "zip_city", "country")) if x)
    ship_text = RichText()
    ship_text.add(f"{L['ship_to']}: ", bold=True)
    ship_text.add(ship_line or f"{L['same_address']}.")
    items = rows(inv)
    ship_cost = Decimal(inv.shipping_cost or 0)
    rate = inv.effective_vat_rate.normalize()
    return {
        "L": L, "H": H, "lang": lang, "currency": inv.currency,
        "number": inv.number, "date": vals["date"], "service_date": _date(inv.service_date, lang),
        "due_date": vals["due_date"], "customer_number": inv.customer_number, "order_ref": inv.order_ref,
        "customer_po": inv.customer_po,
        "bill": address_view(inv.bill_to or {}, lang), "salutation": doc._listing(salutation(inv.bill_to or {}, lang)),
        "seller": seller(lang), "F": footer(lang), "ship_line": ship_line, "ship_text": ship_text,
        "rows": items, "has_shipping": bool(ship_cost), "shipping_pos": len(items) + 1,
        "shipping_cost": doc._fmt_money(ship_cost, lang),
        "goods_total": doc._fmt_money(inv.goods_total, lang), "net_total": doc._fmt_money(inv.net_total, lang),
        "vat_rate": doc._localize(format(rate, "f"), lang) if rate != rate.to_integral_value() else str(int(rate)),
        "vat_total": doc._fmt_money(inv.vat_total, lang), "gross_total": doc._fmt_money(inv.gross_total, lang),
        "has_intro": bool((inv.intro_text or "").strip()), "intro": rich(inv.intro_text, vals),
        "payment_text": doc._listing(fill(inv.payment_text, vals)),
        "vat_note": doc._listing(fill(inv.vat_note, vals)),
        "outro": doc._listing(fill(inv.outro_text, vals)),
        "bank": bank_rows(s, lang) if inv.pay_kind not in NO_BANK else [], "wide_bank": lang != "de",
        "signer": inv.signer_name or _ceo(_company()), "signer_position": inv.signer_position,
    }


# ── Шаблон ───────────────────────────────────────────────────────────────────

TEMPLATE_DIR = "invoice_templates"
TEMPLATE_VARS = [
    ("{{ number }}", "номер рахунку"), ("{{ date }}", "дата рахунку"), ("{{ service_date }}", "дата поставки"),
    ("{{ due_date }}", "оплатити до"), ("{{ customer_number }}", "Kundennummer"),
    ("{{ order_ref }}", "номер замовлення"), ("{{ customer_po }}", "номер замовлення клієнта"),
    ("{{ bill.line1 }} {{ bill.attn }} {{ bill.street }} {{ bill.zip_city }} {{ bill.country }}",
     "адреса покупця: компанія (або ім'я), «z. Hd. Herrn …», вулиця, індекс і місто, країна"),
    ("{{ bill.vat_line }}", "«USt-IdNr.: …» або порожньо"), ("{{ bill.email }}", "email покупця"),
    ("{{ salutation }}", "звертання: «Sehr geehrter Herr …,» або «Sehr geehrte Damen und Herren,»"),
    ("{{ seller.return_line }}", "рядок відправника над адресою"),
    ("{{ seller.name }} {{ seller.street }} {{ seller.zip_city }} {{ seller.country }}",
     "фірма (з «Бухгалтерія → Налаштування компанії»)"),
    ("{%p for x in seller.phones %} {{ x.label }} {{ x.value }} — так само seller.web, seller.tax",
     "Tel / Fax / Mob · E-Mail / Internet · St.-Nr. / USt-IdNr."),
    ("{{ F.c1 }} {{ F.c2 }} {{ F.c3 }}", "нижній колонтитул: фірма й реєстрація · офіс · банк"),
    ("{{r ship_text }} / {{ ship_line }}", "«Lieferadresse: …» (або «wie Rechnungsadresse») / лише адреса"),
    ("{%tr for l in rows %} … {%tr endfor %}", "позиції: l.pos, l.description (з артикулом), l.qty, l.unit_price, l.total"),
    ("{{ shipping_cost }} / {{ has_shipping }}", "доставка"),
    ("{{ net_total }} {{ vat_rate }} {{ vat_total }} {{ gross_total }} {{ currency }}", "суми"),
    ("{{r intro }} / {{ has_intro }}", "вступ ({order} — жирним)"),
    ("{{ payment_text }} {{ vat_note }} {{ outro }}", "тексти (підстановки вже заповнені)"),
    ("{%p for b in bank %} {{ b.label }} {{ b.value }}", "банківські реквізити (немає, коли вже оплачено)"),
    ("{{ signer }} / {{ signer_position }}", "підписант («Ansprechpartner» і під «Mit freundlichen Grüßen»)"),
    ("{{ L.title }}, {{ L.number }}, {{ L.regards }} … {{ H.description }} …", "підписи мовою документа"),
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


def sample_invoice(lang: str = "de") -> SalesInvoice:
    """Приклад (не зберігається) — для перегляду шаблону, перевірки завантаженого й тестового листа."""
    s = InvoiceSettings.get()
    inv = SalesInvoice(
        number="CI-31728-001", inv_date=timezone.localdate(), service_date=timezone.localdate(),
        due_date=timezone.localdate() + timedelta(days=s.due_days), language=lang, currency="EUR",
        bill_to={"company": "Muster Elektronik GmbH", "name": "Max Mustermann", "salutation": "mr",
                 "street": "Hauptstraße 1", "zip_city": "10115 Berlin", "country": "Deutschland",
                 "vat_id": "DE123456789", "email": "einkauf@example.com"},
        ship_to={"company": "Muster Elektronik GmbH", "street": "Lagerweg 5", "zip_city": "12345 Potsdam",
                 "country": "Deutschland"},
        customer_number="31728", order_ref="PO-31728-001", customer_po="4500012345",
        lines=[{"description": "Antenne 2.4 GHz", "part_no": "AN220213-01C", "qty": 10, "unit_price": "12.5"},
               {"description": "Filter 2.7–3.65 GHz", "part_no": "FT220407", "qty": 2, "unit_price": "48.9"}],
        shipping_cost=Decimal("12.90"), vat_mode="domestic", vat_rate=Decimal("19"), payment_method="invoice",
        pay_kind="invoice",
        signer_name=s.signer_name or "Max Muster", signer_position=s.signer_position or "Geschäftsführer")
    for k, v in standard_texts(lang, "domestic", "invoice", s).items():
        setattr(inv, k, v)
    if lang != "de":
        inv.bill_to = {**inv.bill_to, "country": "Germany"}
        inv.ship_to = {**inv.ship_to, "country": "Germany"}
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


def pdf_from_docx(path: str) -> bytes | None:
    """PDF з файлу DOCX через LibreOffice (documents.service._convert_to_pdf приймає ШЛЯХ до файлу, не байти);
    None — немає LibreOffice або конвертація не вдалася (причина — у журналі)."""
    from documents.service import _convert_to_pdf
    try:
        return _convert_to_pdf(str(path))
    except Exception as e:  # noqa: BLE001
        logger.warning("invoice PDF from %s failed: %s", path, e)
        return None


def pdf_from_bytes(data: bytes, name: str = "Rechnung") -> bytes | None:
    """PDF з DOCX у пам'яті (приклад, тестовий лист): тимчасовий файл → pdf_from_docx."""
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, f"{name}.docx")
        with open(path, "wb") as fh:
            fh.write(data)
        return pdf_from_docx(path)


def generate(inv: SalesInvoice) -> SalesInvoice:
    """DOCX (+ PDF, якщо є LibreOffice); копія в документи замовлення (media/orders/{джерело}/{номер}/)."""
    s = InvoiceSettings.get()
    data = render_docx(inv)
    base = file_base(inv, s)
    for f in (inv.docx_file, inv.pdf_file):
        if f:
            f.delete(save=False)
    inv.docx_file.save(f"{base}.docx", ContentFile(data), save=False)
    pdf = pdf_from_docx(inv.docx_file.path)    # без LibreOffice — лише DOCX
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
    """Вкладення листа — лише PDF (DOCX покупцю не надсилаємо); PDF ще немає — None."""
    inv = SalesInvoice.objects.filter(pk=pk).first()
    if inv is None or not inv.pdf_file:
        return None
    with inv.pdf_file.open("rb") as fh:
        content = fh.read()
    return {"name": f"{file_base(inv)}.pdf", "content": content, "content_type": "application/pdf"}


def ensure_pdf(inv: SalesInvoice) -> bool:
    """PDF є, або сформувати заново (DOCX + PDF); False — PDF не вийшов (на сервері немає LibreOffice)."""
    if not inv.pdf_file:
        generate(inv)
    return bool(inv.pdf_file)


def compose_initial(pk, user=None) -> dict:
    """Початкові дані листа: кому (email рахунку / замовлення), тема й текст мовою рахунку, PDF вкладенням."""
    import html
    from django.shortcuts import get_object_or_404
    inv = get_object_or_404(SalesInvoice, pk=pk)
    if not inv.pdf_file and (user is None or user.has_perm("sales.change_salesinvoice")):
        ensure_pdf(inv)                        # поштою — лише PDF
    subject, body = mail_text(inv)
    att = attachment(inv.pk)
    return {"to": recipient(inv), "subject": subject, "body_html": html.escape(body).replace("\n", "<br>"),
            "att_refs": [{"ref": f"invoice:{inv.pk}", "name": att["name"]}] if att else []}


def recipient(inv: SalesInvoice) -> str:
    return (inv.bill_to or {}).get("email") or (inv.sales_order.email if inv.sales_order_id else "") or ""


def mail_text(inv: SalesInvoice, s: InvoiceSettings | None = None) -> tuple[str, str]:
    """(тема, текст) листа з рахунком мовою рахунку; «Deutsch + English» — обидві частини, кожна своєю мовою."""
    s = s or InvoiceSettings.get()
    lang = inv.language if inv.language in LANG_CODES else "de"
    parts = ("de", "en") if lang == "de_en" else (lang,)
    subject = " / ".join(x for x in (fill(getattr(s, f"email_subject_{x}"), values(inv, x)) for x in parts) if x)
    body = "\n\n".join(x for x in (fill(getattr(s, f"email_body_{x}"), values(inv, x)).strip() for x in parts) if x)
    return subject, body


def _shop_of(order):
    try:
        from shop.models import ShopOrder
    except ImportError:
        return None
    info = ShopOrder.objects.filter(order=order).select_related("shop").first() if order is not None else None
    return info.shop if info is not None else None


def default_shop():
    try:
        from shop.models import Shop
    except ImportError:
        return None
    return Shop.objects.filter(is_default=True).first() or Shop.objects.order_by("pk").first()


def send_mail(inv: SalesInvoice, to: str, shop, *, subject_prefix: str = "", files: list | None = None) -> None:
    """Лист покупцю з рахунком у вкладенні — з пошти магазину (призначення «notices», як «Оплату отримано»:
    скринька магазину або SMTP сповіщень), Reply-To і прихована копія — як у листах магазину. Помилки — винятком."""
    import html
    from django.core.mail import EmailMultiAlternatives
    from shop import mail as shop_mail
    subject, body = mail_text(inv)
    cfg = shop_mail.smtp_settings(shop, "notices")
    msg = EmailMultiAlternatives(
        subject=subject_prefix + subject, body=body, from_email=cfg["from"], to=[to],
        connection=shop_mail._connection(cfg), reply_to=[shop.mail_reply_to] if shop.mail_reply_to else None,
        bcc=shop_mail.addresses(shop.mail_bcc) or None)
    msg.attach_alternative('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.45">'
                           + html.escape(body).replace("\n", "<br>") + "</div>", "text/html")
    for item in files if files is not None else [attachment(inv.pk)]:
        if item:
            msg.attach(item["name"], item["content"], item["content_type"])
    shop_mail._deliver(msg, cfg)
    logger.info("invoice %s mailed to %s (%s)", inv.number, to, shop.slug)


# ── Автоматично покупцю (замовлення з інтернет-магазину) ─────────────────────

def _log_auto(order, result: str, to: str = "", number: str = "") -> None:
    """Журнал останніх автоматичних листів (InvoiceSettings.auto_log) — UPDATE, налаштування не перезаписуються."""
    entry = {"at": timezone.now().isoformat(timespec="seconds"), "order": getattr(order, "order_number", ""),
             "order_pk": getattr(order, "pk", None), "number": number, "to": to, "result": result}
    log = list(InvoiceSettings.objects.filter(pk=1).values_list("auto_log", flat=True).first() or [])
    InvoiceSettings.objects.filter(pk=1).update(auto_log=([entry] + log)[:AUTO_LOG_MAX])


AUTO_TEXT = {
    "sent": "надіслано", "no_email": "немає email покупця", "no_pdf": "немає PDF (на сервері немає LibreOffice)",
    "not_configured": "не налаштовано пошту магазину",
}


def auto_send(order_pk) -> str:
    """Рахунок покупцю автоматично: лише замовлення інтернет-магазину (продаж, не скасоване), один раз на
    замовлення. Онлайн-оплата — лише оплачене (тоді текст «вже оплачено»). Повертає результат (і в журнал):
    off / gone / not_shop / skip / wait_payment / busy / already / no_email / no_pdf / sent / error: …"""
    from shop import mail as shop_mail
    from .models import SalesOrder
    s = InvoiceSettings.get()
    if not s.auto_send:
        return "off"
    order = SalesOrder.objects.filter(pk=order_pk).first()
    if order is None:
        return "gone"
    shop = _shop_of(order)
    if shop is None:
        return "not_shop"
    if order.status == "cancelled" or (order.document_type or "SALE") != "SALE":
        return "skip"
    if order.payment_method in ONLINE_METHODS and order.payment_status != "paid":
        return "wait_payment"
    from core.shared_cache import cache
    lock = AUTO_LOCK.format(pk=order.pk)
    if not cache.add(lock, 1, 600):
        return "busy"
    inv, result = None, ""
    try:
        inv = order.sales_invoices.order_by("-pk").first()
        if inv is not None and inv.sent_at:
            return "already"
        if inv is None:
            inv, _ = quick_from_order(order)
        elif order.payment_status == "paid" and inv.pay_kind != "paid":   # рахунок створили до оплати
            inv.pay_kind, inv.due_date, inv.payment_method = "paid", None, order.payment_method or inv.payment_method
            inv.payment_text = standard_texts(inv.language, inv.vat_mode, "paid", s)["payment_text"]
            inv.save()
            generate(inv)
        elif not inv.pdf_file:
            generate(inv)
        to = recipient(inv)
        if not to:
            result = "no_email"
        elif not inv.pdf_file:
            result = "no_pdf"                  # покупцю — лише PDF, не DOCX
        else:
            send_mail(inv, to, shop)
            mark_sent(inv.pk, to)
            result = "sent"
        _log_auto(order, result, to, inv.number)
        return result
    except shop_mail.MailNotConfigured as e:
        logger.warning("invoice auto-send for %s: %s", order.order_number, e)
        _log_auto(order, "not_configured", number=getattr(inv, "number", ""))
        return "not_configured"
    except Exception as e:  # noqa: BLE001 — замовлення вже прийнято; причина — у журналі на сторінці рахунків
        logger.exception("invoice auto-send for %s failed", order.order_number)
        _log_auto(order, f"error: {e}"[:200], number=getattr(inv, "number", ""))
        return f"error: {e}"
    finally:
        cache.delete(lock)


def _worth_trying(order_pk) -> bool:
    """Швидка перевірка до фонового потоку (імпорт сотень замовлень не запускає сотні потоків): увімкнено, замовлення
    інтернет-магазину, ще немає надісланого рахунку."""
    if not InvoiceSettings.objects.filter(pk=1, auto_send=True).exists():
        return False
    try:
        from shop.models import ShopOrder
    except ImportError:
        return False
    return (ShopOrder.objects.filter(order_id=order_pk).exists()
            and not SalesInvoice.objects.filter(sales_order_id=order_pk, sent_at__isnull=False).exists())


def start_auto_send(order_pk) -> None:
    """Після commit (сигнал замовлення): у фоні — сайт не чекає на LibreOffice і SMTP; у тестах — одразу."""
    if not _worth_trying(order_pk):
        return
    if not BACKGROUND:
        auto_send(order_pk)
        return
    import threading

    def work():
        from django.db import connection
        try:
            auto_send(order_pk)
        except Exception:  # noqa: BLE001
            logger.exception("invoice auto-send #%s", order_pk)
        finally:
            connection.close()
    threading.Thread(target=work, name=f"invoice-auto-{order_pk}", daemon=True).start()


def send_test(to: str, lang: str = "de") -> str:
    """Тестовий лист: приклад рахунку (вигадані дані покупця, ваша фірма й шаблон) — так, як його отримає покупець,
    з тієї ж пошти магазину. Нічого не зберігає. Повертає, з якої адреси пішов лист."""
    from shop import mail as shop_mail
    shop = default_shop()
    if shop is None:
        raise InvoiceError("Немає інтернет-магазину — рахунки покупцям надсилаються з його пошти.")
    inv = sample_invoice(lang if lang in LANG_CODES else "de")
    pdf = pdf_from_bytes(render_docx(inv), file_base(inv))
    if not pdf:
        raise InvoiceError("PDF не сформовано — на сервері немає LibreOffice (поштою надсилається лише PDF).")
    item = {"name": f"{file_base(inv)}.pdf", "content": pdf, "content_type": "application/pdf"}
    send_mail(inv, to, shop, subject_prefix="[TEST] ", files=[item])
    return shop_mail.smtp_settings(shop, "notices")["from"]


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


ADDRESS_KEYS = ("company", "name", "salutation", "street", "zip_city", "country", "vat_id", "email")


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
    kind = post.get("pay_kind") or inv.pay_kind
    inv.pay_kind = kind if kind in PAY_KINDS else "invoice"
    for f in ("intro_text", "payment_text", "vat_note", "outro_text"):
        setattr(inv, f, (post.get(f) or "").strip())
    inv.signer_name = (post.get("signer_name") or "").strip()[:120]
    inv.signer_position = (post.get("signer_position") or "").strip()[:120]
    inv.save()
    return inv
