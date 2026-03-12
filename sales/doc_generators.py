"""PDF document generators for SalesOrder.

Generates:
- Packing List
- Proforma Invoice
- Customs Declaration CN23
"""
from __future__ import annotations

from decimal import Decimal
from io import BytesIO

import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
)
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


# ── Unicode font registration ──────────────────────────────────────────────────

def _register_unicode_fonts():
    """Register a Unicode-capable TTF font for Cyrillic/multilingual support.
    Tries DejaVu Sans (Linux/Docker), then Arial (Windows), falls back to Helvetica."""
    candidates = [
        # Linux / Docker (apt: fonts-dejavu-core)
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
        # Windows
        (r"C:\Windows\Fonts\DejaVuSans.ttf",
         r"C:\Windows\Fonts\DejaVuSans-Bold.ttf"),
        (r"C:\Windows\Fonts\arial.ttf",
         r"C:\Windows\Fonts\arialbd.ttf"),
    ]
    for regular, bold in candidates:
        if os.path.isfile(regular) and os.path.isfile(bold):
            pdfmetrics.registerFont(TTFont("UnicodeFont",     regular))
            pdfmetrics.registerFont(TTFont("UnicodeFont-Bold", bold))
            return "UnicodeFont", "UnicodeFont-Bold"
    return "Helvetica", "Helvetica-Bold"


_FONT, _FONT_BOLD = _register_unicode_fonts()


# ── Unit type → short Latin label (used in PDFs to avoid Cyrillic) ────────────

_UNIT_LABEL = {
    "piece":    "pcs",
    "meter":    "m",
    "kilogram": "kg",
    "liter":    "L",
    "set":      "set",
}


# ── Colour constants ───────────────────────────────────────────────────────────

_DARK_BLUE   = colors.HexColor("#1a237e")
_HEADER_BG   = colors.HexColor("#e8eaf6")
_LINE_COLOR  = colors.HexColor("#9fa8da")
_MUTED       = colors.HexColor("#546e7a")
_WHITE       = colors.white
_BLACK       = colors.black
_ACCENT      = colors.HexColor("#2196f3")


# ── Helper: company info ───────────────────────────────────────────────────────

def _get_doc_settings():
    """Returns DocumentSettings singleton (or safe defaults if DB not available)."""
    try:
        from config.models import DocumentSettings
        return DocumentSettings.get()
    except Exception:
        class _Defaults:
            doc_language              = "en"
            packing_list_show_prices  = False
            packing_list_footer_note  = ""
            proforma_payment_terms    = "Payment within 30 days"
            proforma_notes            = ""
            customs_default_type      = "SALE"
            customs_reason            = "Gewerblich / Commercial"
        return _Defaults()


def _get_company():
    """Returns company info dict from accounting.CompanySettings or defaults."""
    defaults = {
        "name": "Minerva",
        "address": "",
        "vat_number": "",
        "iban": "",
        "swift": "",
        "bank_name": "",
        "country": "",
    }
    try:
        from accounting.models import CompanySettings
        cs = CompanySettings.get()
        addr_line = ", ".join(filter(None, [
            cs.addr_street,
            " ".join(filter(None, [cs.addr_zip, cs.addr_city])),
        ]))
        return {
            "name":       cs.name or defaults["name"],
            "address":    addr_line,
            "vat_number": cs.vat_id or "",
            "iban":       cs.iban or "",
            "swift":      cs.swift or "",
            "bank_name":  cs.bank_name or "",
            "country":    cs.addr_country or "",
        }
    except Exception:
        return defaults


# ── Helper: header banner ──────────────────────────────────────────────────────

def _header_table(left_lines: list[str], title: str, right_lines: list[str]) -> Table:
    """Returns a dark-blue banner table with company | TITLE | ref info."""
    styles = getSampleStyleSheet()

    def _cell(lines, align):
        ps = ParagraphStyle("hdr", parent=styles["Normal"],
                            textColor=_WHITE, fontSize=8,
                            leading=11, alignment=align)
        return [Paragraph(l, ps) for l in lines]

    title_ps = ParagraphStyle("hdr_title", parent=styles["Normal"],
                              textColor=_WHITE, fontSize=13,
                              fontName=_FONT_BOLD, leading=16,
                              alignment=TA_CENTER)

    data = [[
        _cell(left_lines, TA_LEFT),
        Paragraph(title, title_ps),
        _cell(right_lines, TA_RIGHT),
    ]]
    tbl = Table(data, colWidths=[5.5 * cm, 8 * cm, 5.5 * cm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _DARK_BLUE),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING",   (0, 0), (0, -1), 10),
        ("RIGHTPADDING",  (-1, 0), (-1, -1), 10),
    ]))
    return tbl


def _data_table(headers: list[str], rows: list[list], col_widths: list[float]) -> Table:
    """Returns a styled data table."""
    all_rows = [headers] + rows
    tbl = Table(all_rows, colWidths=col_widths)
    tbl.setStyle(TableStyle([
        # Header row
        ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
        ("TEXTCOLOR",  (0, 0), (-1, 0), _DARK_BLUE),
        ("FONTNAME",   (0, 0), (-1, 0), _FONT_BOLD),
        ("FONTSIZE",   (0, 0), (-1, 0), 9),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING",    (0, 0), (-1, 0), 6),
        # Data rows
        ("FONTSIZE",   (0, 1), (-1, -1), 8),
        ("TOPPADDING", (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (-1, 0), (-1, -1), 6),
        # Grid
        ("GRID",       (0, 0), (-1, -1), 0.5, _LINE_COLOR),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return tbl


def _info_table(pairs: list[tuple[str, str]], col_widths=None) -> Table:
    """Simple two-column key-value table."""
    styles = getSampleStyleSheet()
    label_ps = ParagraphStyle("lbl", parent=styles["Normal"],
                              textColor=_MUTED, fontSize=8, leading=11)
    val_ps   = ParagraphStyle("val", parent=styles["Normal"],
                              fontSize=8, leading=11)
    data = [[Paragraph(k, label_ps), Paragraph(v or "—", val_ps)] for k, v in pairs]
    widths = col_widths or [5 * cm, 14 * cm]
    tbl = Table(data, colWidths=widths)
    tbl.setStyle(TableStyle([
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]))
    return tbl


def _para(text: str, style=None, fontSize=9, bold=False, color=None, align=TA_LEFT):
    styles = getSampleStyleSheet()
    base = style or styles["Normal"]
    ps = ParagraphStyle(
        "p",
        parent=base,
        fontSize=fontSize,
        fontName=_FONT_BOLD if bold else _FONT,
        textColor=color or _BLACK,
        leading=fontSize + 3,
        alignment=align,
    )
    return Paragraph(text or "", ps)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Packing List
# ══════════════════════════════════════════════════════════════════════════════

def generate_packing_list(order, overrides=None) -> BytesIO:
    """Generate a packing list PDF for the given SalesOrder."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
    )

    ds  = _get_doc_settings()
    ovr = overrides or {}
    company = _get_company()
    order_date_str = order.order_date.strftime("%d.%m.%Y") if order.order_date else "—"

    story = []

    # Header banner
    left = [
        f"<b>{company['name']}</b>",
        company.get("address", "") or "",
    ]
    right = [
        f"Order: <b>{order.order_number}</b>",
        f"Date: {order_date_str}",
    ]
    story.append(_header_table(left, "PACKING LIST", right))
    story.append(Spacer(1, 0.4 * cm))

    # Ship-to block
    ship_to_lines = [
        order.client or "—",
        order.addr_street or "",
        f"{order.addr_zip} {order.addr_city}".strip() if (order.addr_zip or order.addr_city) else "",
        order.addr_country or "",
    ]
    ship_to_text = "<br/>".join(l for l in ship_to_lines if l)
    story.append(_para("<b>SHIP TO:</b>", fontSize=9))
    story.append(_para(ship_to_text, fontSize=9))
    story.append(HRFlowable(width="100%", thickness=0.5, color=_LINE_COLOR,
                             spaceAfter=0.3 * cm, spaceBefore=0.3 * cm))

    # Lines table — description = category name
    from inventory.models import ProductCategory
    cat_names = dict(ProductCategory.objects.values_list("slug", "name"))

    # Override: show_prices — from GET param ('1'/'0') or DocumentSettings
    _sp = ovr.get('show_prices')
    show_prices  = (_sp == '1') if _sp is not None else ds.packing_list_show_prices
    footer_note  = ovr.get('footer_note', ds.packing_list_footer_note)
    currency_sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(order.currency or "EUR", "€")

    if show_prices:
        headers = ["SKU", "Description", "Qty", "Unit", "Unit Price", "Total"]
        col_widths = [3.0 * cm, 6.0 * cm, 1.5 * cm, 1.8 * cm, 2.5 * cm, 3.2 * cm]
    else:
        headers = ["SKU", "Description", "Qty", "Unit"]
        col_widths = [4.5 * cm, 9 * cm, 2 * cm, 3.5 * cm]

    rows = []
    for line in order.lines.select_related("product").all():
        sku = line.sku_raw or (line.product.sku if line.product else "—")
        if line.product and line.product.category:
            desc = cat_names.get(line.product.category, line.product.category)
        else:
            desc = (line.product.name_export or line.product.name if line.product else "") or ""
        qty = int(line.qty) if line.qty == int(line.qty) else str(line.qty)
        unit = _UNIT_LABEL.get(line.product.unit_type, "pcs") if line.product else "pcs"
        row = [sku, desc, str(qty), unit]
        if show_prices:
            unit_p  = line.unit_price
            total_p = line.total_price
            if not unit_p and total_p and line.qty:
                try:
                    unit_p = total_p / line.qty
                except Exception:
                    unit_p = None
            row.append(f"{currency_sym}{unit_p:.2f}" if unit_p else "—")
            row.append(f"{currency_sym}{total_p:.2f}" if total_p else "—")
        rows.append(row)

    if rows:
        tbl = _data_table(headers, rows, col_widths=col_widths)
        story.append(tbl)
    else:
        story.append(_para("No items.", fontSize=9, color=_MUTED))

    story.append(HRFlowable(width="100%", thickness=0.5, color=_LINE_COLOR,
                             spaceAfter=0.3 * cm, spaceBefore=0.4 * cm))

    # Tracking / courier footer
    footer_pairs = []
    if order.tracking_number:
        footer_pairs.append(("Tracking:", order.tracking_number))
    if order.shipping_courier:
        footer_pairs.append(("Courier:", order.shipping_courier))
    if footer_pairs:
        story.append(_info_table(footer_pairs, col_widths=[3 * cm, 16 * cm]))

    # Custom footer note (override or DocumentSettings)
    if footer_note:
        story.append(Spacer(1, 0.2 * cm))
        story.append(_para(footer_note, fontSize=8, color=_MUTED))

    doc.build(story)
    buf.seek(0)
    return buf


# ══════════════════════════════════════════════════════════════════════════════
# 2. Proforma Invoice
# ══════════════════════════════════════════════════════════════════════════════

def generate_proforma(order, overrides=None) -> BytesIO:
    """Generate a proforma invoice PDF for the given SalesOrder."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
    )

    ds  = _get_doc_settings()
    ovr = overrides or {}
    company = _get_company()
    order_date_str = order.order_date.strftime("%d.%m.%Y") if order.order_date else "—"
    pi_number = f"PI-{order.order_number}"
    currency_sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(order.currency or "EUR", "€")

    story = []

    # Header banner
    left = [
        f"<b>{company['name']}</b>",
        company.get("address", "") or "",
    ]
    right = [
        f"<b>{pi_number}</b>",
        order_date_str,
    ]
    story.append(_header_table(left, "PROFORMA INVOICE", right))
    story.append(Spacer(1, 0.4 * cm))

    # FROM / TO block as two-column table
    from_lines = [f"<b>FROM:</b>", company["name"]]
    if company.get("address"):
        from_lines.append(company["address"])
    if company.get("vat_number"):
        from_lines.append(f"VAT: {company['vat_number']}")
    if company.get("country"):
        from_lines.append(company["country"])

    to_addr = " ".join(filter(None, [
        order.addr_street,
        f"{order.addr_zip} {order.addr_city}".strip(),
        order.addr_country,
    ]))
    to_lines = [f"<b>TO:</b>", order.client or "—"]
    if to_addr:
        to_lines.append(to_addr)
    if order.email:
        to_lines.append(order.email)

    styles = getSampleStyleSheet()
    ps = ParagraphStyle("frt", parent=styles["Normal"], fontSize=8, leading=12)
    from_cell = [Paragraph(l, ps) for l in from_lines]
    to_cell   = [Paragraph(l, ps) for l in to_lines]
    addr_tbl = Table([[from_cell, to_cell]], colWidths=[9.5 * cm, 9.5 * cm])
    addr_tbl.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(addr_tbl)
    story.append(HRFlowable(width="100%", thickness=0.5, color=_LINE_COLOR,
                             spaceAfter=0.3 * cm, spaceBefore=0.3 * cm))

    # Line items table
    headers = ["#", "SKU / Description", "Qty", "Unit Price", "Total"]
    rows = []
    subtotal = Decimal("0")
    for idx, line in enumerate(order.lines.select_related("product").all(), start=1):
        sku  = line.sku_raw or (line.product.sku if line.product else "—")
        desc = (line.product.name_export or line.product.name if line.product else "") or ""
        label = f"{sku}<br/><font size='7' color='grey'>{desc}</font>" if desc else sku
        qty  = int(line.qty) if line.qty == int(line.qty) else float(line.qty)

        # Determine unit price
        unit_p = line.unit_price
        if not unit_p and line.total_price and line.qty:
            try:
                unit_p = line.total_price / line.qty
            except Exception:
                unit_p = None

        total_p = line.total_price or (unit_p * Decimal(str(line.qty)) if unit_p else None)
        if total_p:
            subtotal += total_p

        unit_str  = f"{currency_sym}{unit_p:.2f}"  if unit_p  else "—"
        total_str = f"{currency_sym}{total_p:.2f}" if total_p else "—"

        rows.append([str(idx), Paragraph(label, ParagraphStyle("d", fontSize=8, leading=11)),
                     str(qty), unit_str, total_str])

    if rows:
        tbl = _data_table(
            headers, rows,
            col_widths=[0.8 * cm, 9.5 * cm, 1.8 * cm, 2.8 * cm, 3.1 * cm],
        )
        story.append(tbl)
    else:
        story.append(_para("No items.", fontSize=9, color=_MUTED))

    story.append(Spacer(1, 0.3 * cm))

    # Totals block (right-aligned)
    shipping = order.shipping_cost or Decimal("0")
    total    = subtotal + shipping
    ship_sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(order.shipping_currency or order.currency or "EUR", "€")

    totals_data = [
        ["Subtotal:", f"{currency_sym}{subtotal:.2f}"],
        [f"Shipping ({ship_sym}):", f"{ship_sym}{shipping:.2f}"],
        [f"TOTAL {order.currency or 'EUR'}:", f"{currency_sym}{total:.2f}"],
    ]
    totals_style = ParagraphStyle("tot", fontSize=9)
    totals_rows = [
        [Paragraph(r[0], ParagraphStyle("tl", fontSize=8, textColor=_MUTED)),
         Paragraph(r[1], ParagraphStyle("tv", fontSize=9,
                                        fontName=_FONT_BOLD if i == 2 else _FONT))]
        for i, r in enumerate(totals_data)
    ]
    totals_tbl = Table(totals_rows, colWidths=[14 * cm, 5 * cm])
    totals_tbl.setStyle(TableStyle([
        ("ALIGN",         (1, 0), (1, -1), "RIGHT"),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEABOVE",     (0, 2), (-1, 2), 1, _LINE_COLOR),
    ]))
    story.append(totals_tbl)

    story.append(HRFlowable(width="100%", thickness=0.5, color=_LINE_COLOR,
                             spaceAfter=0.3 * cm, spaceBefore=0.4 * cm))

    # Bank info
    bank_pairs = []
    if company.get("iban"):
        bank_pairs.append(("IBAN:", company["iban"]))
    if company.get("swift"):
        bank_pairs.append(("BIC/SWIFT:", company["swift"]))
    if company.get("bank_name"):
        bank_pairs.append(("Bank:", company["bank_name"]))
    if bank_pairs:
        story.append(_info_table(bank_pairs, col_widths=[3 * cm, 16 * cm]))
        story.append(Spacer(1, 0.3 * cm))

    # Payment terms (override or DocumentSettings)
    payment_terms = ovr.get('payment_terms', ds.proforma_payment_terms)
    if payment_terms:
        story.append(_para(f"Payment terms: {payment_terms}", fontSize=8, color=_MUTED))
    story.append(_para(
        "This is a proforma invoice — not a VAT tax document.",
        fontSize=8, color=_MUTED,
    ))
    proforma_notes = ovr.get('notes', ds.proforma_notes)
    if proforma_notes:
        story.append(Spacer(1, 0.2 * cm))
        story.append(_para(proforma_notes, fontSize=8, color=_MUTED))

    doc.build(story)
    buf.seek(0)
    return buf


# ══════════════════════════════════════════════════════════════════════════════
# 3. Customs Declaration CN23
# ══════════════════════════════════════════════════════════════════════════════

def generate_customs(order, overrides=None) -> BytesIO:
    """Generate a CN23-style customs declaration PDF for the given SalesOrder."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
    )

    ds  = _get_doc_settings()
    ovr = overrides or {}
    company = _get_company()
    order_date_str = order.order_date.strftime("%d.%m.%Y") if order.order_date else "—"
    currency_sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(order.currency or "EUR", "€")

    story = []

    # Title
    story.append(_para(
        "CUSTOMS DECLARATION / ZOLLERKLÄRUNG  CN23",
        fontSize=13, bold=True, align=TA_CENTER,
    ))
    story.append(HRFlowable(width="100%", thickness=1.5, color=_DARK_BLUE,
                             spaceAfter=0.3 * cm, spaceBefore=0.2 * cm))

    # Sender / Recipient block
    sender_lines = [f"<b>SENDER / ABSENDER:</b>"]
    sender_lines.append(company["name"])
    if company.get("address"):
        sender_lines.append(company["address"])
    if company.get("country"):
        sender_lines.append(company["country"])

    recip_addr = " ".join(filter(None, [
        order.addr_street,
        f"{order.addr_zip} {order.addr_city}".strip(),
        order.addr_country,
    ]))
    recip_lines = [f"<b>RECIPIENT / EMPFÄNGER:</b>"]
    recip_lines.append(order.client or "—")
    if recip_addr:
        recip_lines.append(recip_addr)

    styles = getSampleStyleSheet()
    ps8 = ParagraphStyle("cn_ps", parent=styles["Normal"], fontSize=8, leading=12)
    sender_cell = [Paragraph(l, ps8) for l in sender_lines]
    recip_cell  = [Paragraph(l, ps8) for l in recip_lines]
    addr_tbl = Table([[sender_cell, recip_cell]], colWidths=[9.5 * cm, 9.5 * cm])
    addr_tbl.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("BOX",           (0, 0), (-1, -1), 0.5, _LINE_COLOR),
        ("INNERGRID",     (0, 0), (-1, -1), 0.5, _LINE_COLOR),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
    ]))
    story.append(addr_tbl)
    story.append(Spacer(1, 0.35 * cm))

    # Category of item (checkboxes) — quick-edit override → order.document_type → DocumentSettings
    doc_type = (
        ovr.get('declaration_type') or
        getattr(order, "document_type", "") or
        ds.customs_default_type or
        "SALE"
    )
    _chk = lambda flag: "☒" if flag else "☐"
    category_text = (
        f"{_chk(doc_type == 'SALE')} Sale/Verkauf    "
        f"{_chk(doc_type == 'SAMPLE')} Gift/Geschenk    "
        f"{_chk(doc_type == 'TRANSFER')} Sample/Muster    "
        f"{_chk(doc_type == 'WARRANTY')} Return/Rücksendung    "
        f"{_chk(doc_type == 'OTHER')} Other/Sonstiges"
    )
    story.append(_para(category_text, fontSize=9))
    story.append(HRFlowable(width="100%", thickness=0.5, color=_LINE_COLOR,
                             spaceAfter=0.3 * cm, spaceBefore=0.3 * cm))

    # Contents table — preload category customs data
    from inventory.models import ProductCategory
    cat_data = {
        c.slug: c
        for c in ProductCategory.objects.filter(
            slug__in=[
                l.product.category
                for l in order.lines.select_related("product").all()
                if l.product and l.product.category
            ]
        )
    }

    headers = ["HS-Code", "Description of Contents", "Qty", "Net Weight (g)", "Value", "Origin"]
    rows = []
    total_weight = 0
    total_value  = Decimal("0")

    for line in order.lines.select_related("product").all():
        cat = cat_data.get(line.product.category, None) if line.product else None
        # HS-Code: product first, then category, then "—"
        hs = (
            (line.product.hs_code if line.product else "") or
            (cat.customs_hs_code if cat else "") or
            "—"
        )
        # Description: product name_export → category DE description → category name → override/DocumentSettings fallback
        _reason = ovr.get('reason', ds.customs_reason) or "Gewerblich"
        desc = (
            (line.product.name_export if line.product else "") or
            (cat.customs_description_de if cat else "") or
            (cat.name if cat else "") or
            (line.product.name if line.product else line.sku_raw) or
            _reason
        )
        qty   = int(line.qty) if line.qty == int(line.qty) else float(line.qty)
        w_per = (line.product.net_weight_g if line.product else 0) or 0
        weight = int(w_per) * int(qty)
        total_weight += weight

        val = line.total_price or Decimal("0")
        if not val and line.unit_price and line.qty:
            try:
                val = line.unit_price * Decimal(str(line.qty))
            except Exception:
                val = Decimal("0")
        total_value += val

        origin = (
            (line.product.country_of_origin if line.product else "") or
            (cat.customs_country_of_origin if cat else "") or
            "DE"
        )
        rows.append([hs, desc, str(qty), str(weight) if weight else "—",
                     f"{currency_sym}{val:.2f}", origin])

    if rows:
        tbl = _data_table(
            headers, rows,
            col_widths=[2.5 * cm, 7 * cm, 1.5 * cm, 2.5 * cm, 2.5 * cm, 2 * cm],
        )
        story.append(tbl)
    else:
        story.append(_para("No items.", fontSize=9, color=_MUTED))

    story.append(HRFlowable(width="100%", thickness=0.5, color=_LINE_COLOR,
                             spaceAfter=0.3 * cm, spaceBefore=0.4 * cm))

    # Totals row
    totals_ps = ParagraphStyle("tot", fontSize=9)
    totals_data = [
        [Paragraph(f"<b>Total Net Weight: {total_weight} g</b>", totals_ps),
         Paragraph(f"<b>Total Declared Value: {currency_sym}{total_value:.2f}</b>", totals_ps)],
    ]
    totals_tbl = Table(totals_data, colWidths=[9.5 * cm, 9.5 * cm])
    totals_tbl.setStyle(TableStyle([
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(totals_tbl)
    story.append(Spacer(1, 0.4 * cm))

    # Certification text
    story.append(_para(
        "I certify that the particulars given in this declaration are correct "
        "and that this item does not contain any dangerous article.",
        fontSize=8, color=_MUTED,
    ))
    story.append(Spacer(1, 0.6 * cm))

    # Signature line
    sig_data = [
        [Paragraph(f"Date: {order_date_str}", ParagraphStyle("sl", fontSize=9)),
         Paragraph("Signature / Unterschrift:", ParagraphStyle("sl", fontSize=9))],
        [Paragraph("_" * 35, ParagraphStyle("sl", fontSize=9)),
         Paragraph("_" * 35, ParagraphStyle("sl", fontSize=9))],
    ]
    sig_tbl = Table(sig_data, colWidths=[9.5 * cm, 9.5 * cm])
    sig_tbl.setStyle(TableStyle([
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(sig_tbl)

    doc.build(story)
    buf.seek(0)
    return buf
