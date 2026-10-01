"""
shipping/services/commercial_invoice_service.py — Commercial Invoice з власного Word-шаблону (docxtpl).

Не плутати з invoice_service.py (інвойси Sevskiy → DigiKey Marketplace, /invoices/).
Дані: замовлення + митна декларація останнього відправлення (опис, HS-код, країна),
fallback: товар → категорія товарів.
"""
import logging
import shutil
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile

from shipping.services.packing_list_service import (
    _customs_by_sku, _dec, _fmt_exact, build_ship_to, is_placeholder_description,
    last_signer, latest_shipment,
)

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE = Path(settings.BASE_DIR) / "shipping" / "templates_docx" / "commercial_invoice_template.docx"

INCOTERMS = {
    "DAP": "DAP (Delivered at Place)",
    "DDP": "DDP (Delivered Duty Paid)",
    "EXW": "EXW (Ex Works)",
    "FCA": "FCA (Free Carrier)",
    "CPT": "CPT (Carriage Paid To)",
    "CIP": "CIP (Carriage and Insurance Paid To)",
    "FOB": "FOB (Free on Board)",
    "CFR": "CFR (Cost and Freight)",
    "CIF": "CIF (Cost, Insurance and Freight)",
}

# Shipment.export_reason / customs_articles.type → текст для інвойсу
EXPORT_REASONS = {
    "commercial": "commercial sale", "gift": "gift", "sample": "sample",
    "return": "return", "repair": "repair", "personal": "personal effects",
    "private": "personal effects", "other": "other", "claim": "warranty claim",
}

CI_TEMPLATE_VARS = [
    ("{{ inv_number }}",        "Номер інвойсу",                          "17701"),
    ("{{ inv_date }}",          "Дата (YYYY-MM-DD)",                      "2026-10-01"),
    ("{{ ship_to.company }}",   "Компанія отримувача",                    "Voltaat Store for Trading"),
    ("{{ ship_to.street }}",    "Вулиця, будинок",                        "Zone 32, Street 958"),
    ("{{ ship_to.city_line }}", "Індекс + місто",                         "Doha"),
    ("{{ ship_to.country }}",   "Країна англійською",                     "Qatar"),
    ("{{ ship_to.contact }}",   "Контактна особа",                        "Yahya Alhomsi"),
    ("{{ ship_to.phone }}",     "Телефон",                                "+974 7733 4439"),
    ("{{ ship_to.email }}",     "Email",                                  "yalhomsi@voltaat.com"),
    ("{{ billing_address }}",   "Адреса для рахунку (один рядок)",        "Voltaat…, Doha, Qatar."),
    ("{{ currency }}",          "Валюта (в заголовках колонок)",          "USD"),
    ("{%tr for it in lines %}", "Рядок-маркер початку таблиці товарів",   ""),
    ("{{ it.pos }}",            "Позиція",                                "1"),
    ("{{ it.description }}",    "Опис товару",                            "Antenna"),
    ("{{ it.part_no }}",        "P/N (артикул)",                          "AN240208-01A"),
    ("{{ it.hs_code }}",        "HS-код (Tariff Number)",                 "85177100"),
    ("{{ it.origin }}",         "Країна походження англійською",          "Germany"),
    ("{{ it.qty }}",            "Кількість",                              "1"),
    ("{{ it.unit_value }}",     "Ціна за одиницю",                        "149.99"),
    ("{{ it.total }}",          "Сума рядка",                             "149.99"),
    ("{%tr endfor %}",          "Рядок-маркер кінця таблиці",             ""),
    ("{%tr if has_shipping %}", "Рядок «Packaging and shipping costs» лише якщо сума > 0", ""),
    ("{{ shipping_cost }}",     "Пакування і доставка",                   "100.00"),
    ("{%tr endif %}",           "Кінець умовного рядка",                  ""),
    ("{{ goods_total }}",       "Сума товарів",                           "149.99"),
    ("{{ total_amount }}",      "Total invoice amount",                   "249.99"),
    ("{{ incoterm_text }}",     "Incoterm з розшифровкою",                "DAP (Delivered at Place)"),
    ("{{ export_reason }}",     "Reason of export",                       "commercial sale"),
    ("{{ payment_terms }}",     "Payment terms",                          "advance payment."),
    ("{{ signer_name }}",       "Підписант",                              "Dr. Sergey Sevskiy"),
    ("{{ signer_position }}",   "Посада",                                 "CEO"),
]


# ── Шаблон ────────────────────────────────────────────────────────────────────

def _custom_template_path() -> Path:
    return Path(settings.MEDIA_ROOT) / "commercial_invoice_templates" / "custom.docx"


def active_template_path() -> Path:
    custom = _custom_template_path()
    return custom if custom.exists() else DEFAULT_TEMPLATE


def has_custom_template() -> bool:
    return _custom_template_path().exists()


def save_custom_template(uploaded) -> None:
    from docxtpl import DocxTemplate
    data = uploaded.read()
    tpl = DocxTemplate(BytesIO(data))
    tpl.render(build_context(_sample_data()))
    path = _custom_template_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def reset_custom_template() -> None:
    _custom_template_path().unlink(missing_ok=True)


def _sample_data() -> dict:
    return {
        "number": "00000", "inv_date": date.today(), "currency": "USD",
        "ship_to": {"company": "Sample Co", "street": "Street 1", "city_line": "City",
                    "country": "Country", "contact": "Name", "phone": "+00", "email": "a@b.c"},
        "billing_address": "Sample Co, Street 1, City, Country.",
        "lines": [{"description": "Item", "part_no": "PN", "hs_code": "8517", "origin": "Germany",
                   "qty": 1, "unit_value": 10}],
        "shipping_cost": 5, "incoterm": "DAP", "export_reason": "commercial sale",
        "payment_terms": "advance payment.", "signer_name": "Name", "signer_position": "CEO",
    }


# ── Префіл із замовлення ──────────────────────────────────────────────────────

def _lines(order, shipment) -> list:
    from config.country_utils import country_name_en
    from inventory.models import ProductCategory

    order_lines = list(order.lines.select_related("product").order_by("pk"))
    cat_slugs = {ln.product.category for ln in order_lines if ln.product and ln.product.category}
    cats = {c.slug: c for c in ProductCategory.objects.filter(slug__in=cat_slugs)} if cat_slugs else {}
    customs = _customs_by_sku(shipment, order)
    sender_country = (shipment.carrier.sender_country if shipment and shipment.carrier else "") or "DE"

    lines = []
    for ln in order_lines:
        p = ln.product
        cat = cats.get(p.category) if p and p.category else None
        sku = (p.sku if p else ln.sku_raw) or ""
        c = customs.get(sku, {})
        c_desc = "" if is_placeholder_description(c.get("description"), sku) else c.get("description", "")
        qty = _dec(ln.qty)
        unit = _dec(ln.unit_price)
        if not unit and qty:
            unit = _dec(ln.total_price) / qty
        origin = (c.get("origin_country") or (p.country_of_origin if p else "")
                  or (cat.customs_country_of_origin if cat else "") or sender_country)
        lines.append({
            "description": (c_desc or (p.name_export if p else "") or (cat.customs_description_de if cat else "")
                            or (p.name if p else "") or sku),
            "part_no":     sku,
            "hs_code":     c.get("customs_number") or (p.hs_code if p else "") or (cat.customs_hs_code if cat else ""),
            "origin":      country_name_en(origin),
            "qty":         float(qty),
            "unit_value":  float(unit.normalize()) if unit else 0,
            "product_id":  p.pk if p else None,
        })
    return lines


def initial_data(order) -> dict:
    from shipping.models import CommercialInvoice

    shipment = latest_shipment(order)
    ship_to = build_ship_to(order, shipment)
    lines = _lines(order, shipment)
    currency = ((order.currency or "") or next((l.currency for l in order.lines.all() if l.currency), "") or "USD").upper()[:3]

    notes = []
    shipping_cost = _dec(order.shipping_cost)
    ship_cur = (order.shipping_currency or currency).upper()
    if shipping_cost and ship_cur != currency:
        notes.append(f"Доставка в замовленні: {shipping_cost} {ship_cur}, а інвойс у {currency} — "
                     f"перерахуйте і впишіть суму вручну.")
        shipping_cost = Decimal(0)

    customs = (shipment.customs_articles or {}) if shipment else {}
    incoterm = (customs.get("incoterm") or "DAP").upper()
    reason_key = (customs.get("type") or (shipment.export_reason if shipment else "") or "commercial").lower()

    if shipment and shipment.declared_value:
        goods = sum(_dec(l["qty"]) * _dec(l["unit_value"]) for l in lines)
        if abs(goods - _dec(shipment.declared_value)) > Decimal("0.01"):
            notes.append(f"Задекларована вартість у відправленні #{shipment.pk}: "
                         f"{shipment.declared_value} {shipment.declared_currency}, сума товарів: {goods:.2f} {currency}.")

    last = CommercialInvoice.objects.order_by("-created_at").first()
    signer_name, signer_position = last_signer()
    billing = ", ".join(x for x in (ship_to.get("company"), ship_to.get("street"),
                                    ship_to.get("city_line"), ship_to.get("country")) if x)
    return {
        "number":          order.order_number or "",
        "inv_date":        date.today(),
        "ship_to":         ship_to,
        "billing_address": f"{billing}." if billing else "",
        "currency":        currency,
        "lines":           lines,
        "shipping_cost":   float(shipping_cost),
        "incoterm":        incoterm if incoterm in INCOTERMS else "DAP",
        "export_reason":   EXPORT_REASONS.get(reason_key, "commercial sale"),
        "payment_terms":   last.payment_terms if last and last.payment_terms else "advance payment.",
        "signer_name":     signer_name,
        "signer_position": signer_position,
        "shipment":        shipment,
        "notes":           notes,
    }


# ── Контекст шаблону ──────────────────────────────────────────────────────────

def _money(v) -> str:
    return _fmt_exact(v, 2).replace(",", ".")


def _qty(v) -> str:
    d = _dec(v)
    return str(int(d)) if d == int(d) else _fmt_exact(d, 0).replace(",", ".")


def build_context(data: dict) -> dict:
    lines, goods = [], Decimal(0)
    for i, ln in enumerate(data.get("lines") or [], 1):
        qty, unit = _dec(ln.get("qty")), _dec(ln.get("unit_value"))
        total = (qty * unit).quantize(Decimal("0.01"))
        goods += total
        lines.append({
            "pos": i, "description": ln.get("description", ""), "part_no": ln.get("part_no", ""),
            "hs_code": ln.get("hs_code", ""), "origin": ln.get("origin", ""),
            "qty": _qty(qty), "unit_value": _money(unit), "total": _money(total),
        })
    shipping = _dec(data.get("shipping_cost")).quantize(Decimal("0.01"))
    d = data.get("inv_date")
    incoterm = (data.get("incoterm") or "").upper()
    return {
        "inv_number":      data.get("number", ""),
        "inv_date":        d.isoformat() if hasattr(d, "isoformat") else (d or ""),
        "ship_to":         data.get("ship_to") or {},
        "billing_address": data.get("billing_address", ""),
        "currency":        data.get("currency", ""),
        "lines":           lines,
        "has_shipping":    shipping > 0,
        "shipping_cost":   _money(shipping),
        "goods_total":     _money(goods),
        "total_amount":    _money(goods + shipping),
        "incoterm_text":   INCOTERMS.get(incoterm, incoterm),
        "export_reason":   data.get("export_reason", ""),
        "payment_terms":   data.get("payment_terms", ""),
        "signer_name":     data.get("signer_name", ""),
        "signer_position": data.get("signer_position", ""),
    }


# ── Генерація ─────────────────────────────────────────────────────────────────

def generate_files(ci) -> None:
    from docxtpl import DocxTemplate
    from documents.service import _convert_to_pdf

    tpl = DocxTemplate(str(active_template_path()))
    tpl.render(build_context({
        "number": ci.number, "inv_date": ci.inv_date, "ship_to": ci.ship_to,
        "billing_address": ci.billing_address, "currency": ci.currency, "lines": ci.lines,
        "shipping_cost": ci.shipping_cost, "incoterm": ci.incoterm, "export_reason": ci.export_reason,
        "payment_terms": ci.payment_terms, "signer_name": ci.signer_name, "signer_position": ci.signer_position,
    }))
    buf = BytesIO()
    tpl.save(buf)

    base = f"Invoice_{ci.number}"
    for f in (ci.docx_file, ci.pdf_file):
        if f:
            f.delete(save=False)
    ci.docx_file.save(f"{base}.docx", ContentFile(buf.getvalue()), save=False)
    try:
        pdf_bytes = _convert_to_pdf(ci.docx_file.path)
    except Exception as e:
        logger.warning("Commercial invoice PDF conversion failed: %s", e)
        pdf_bytes = None
    if pdf_bytes:
        ci.pdf_file.save(f"{base}.pdf", ContentFile(pdf_bytes), save=False)
    ci.save()

    order = ci.sales_order
    if order and order.order_number:
        dest = Path(settings.MEDIA_ROOT) / "orders" / (order.source or "manual") / order.order_number
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ci.docx_file.path, dest / f"{base}.docx")
        if ci.pdf_file:
            shutil.copyfile(ci.pdf_file.path, dest / f"{base}.pdf")
