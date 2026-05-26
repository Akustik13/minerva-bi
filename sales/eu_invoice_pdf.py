"""EU Invoice PDF generator for DigiKey Marketplace and other EU buyers."""
from __future__ import annotations

import os
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, Image,
)
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER


# ── Reuse font registration from doc_generators ───────────────────────────────
try:
    from sales.doc_generators import _FONT, _FONT_BOLD, _UNIT_LABEL
except Exception:
    _FONT, _FONT_BOLD = "Helvetica", "Helvetica-Bold"
    _UNIT_LABEL = {}


# ── Colours ───────────────────────────────────────────────────────────────────
_DARK   = colors.HexColor("#1a237e")
_GREY   = colors.HexColor("#546e7a")
_LIGHT  = colors.HexColor("#e8eaf6")
_SUBTLE = colors.HexColor("#f5f5f5")
_WHITE  = colors.white
_BLACK  = colors.black


def _r2(v) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _fmt(amount: Decimal, currency: str = "") -> str:
    prefix = f"{currency} " if currency else ""
    return f"{prefix}{amount:,.2f}"


def generate_eu_invoice(
    order,
    *,
    invoice_number: int,
    invoice_date,
    buyer_name: str = "",
    buyer_address: str = "",
    buyer_vat_id: str = "",
    ship_vat_id: str = "",
    discount_pct: Decimal = Decimal("0"),
    vat_rate: Decimal = Decimal("19"),
) -> bytes:
    """Generate a professional EU Invoice PDF and return as bytes."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=1.8 * cm, rightMargin=1.8 * cm,
        topMargin=1.5 * cm, bottomMargin=2 * cm,
    )
    page_w = A4[0] - 3.6 * cm

    def _p(text, size=9, bold=False, align=TA_LEFT, color=_BLACK, leading=None):
        font = _FONT_BOLD if bold else _FONT
        ps = ParagraphStyle(
            "_p",
            fontName=font, fontSize=size,
            leading=leading or (size * 1.35),
            textColor=color, alignment=align,
            spaceAfter=0, spaceBefore=0,
        )
        return Paragraph(str(text) if text is not None else "", ps)

    def _cell(paragraphs):
        """Wrap a list of mixed Paragraph/Spacer/Image in a simple 1-col table cell."""
        return paragraphs  # ReportLab accepts a list in a table cell

    story = []

    # ── Load company settings ─────────────────────────────────────────────────
    try:
        from accounting.models import CompanySettings
        cs = CompanySettings.get()
    except Exception:
        cs = None

    co_name    = (cs.legal_name or cs.name) if cs else "Seller"
    co_addr_parts = []
    if cs:
        if cs.addr_street:
            co_addr_parts.append(cs.addr_street)
        city_line = " ".join(filter(None, [cs.addr_zip, cs.addr_city]))
        if city_line:
            co_addr_parts.append(city_line)
        if cs.addr_country:
            co_addr_parts.append(cs.addr_country)
    co_vat     = cs.vat_id if cs else ""
    co_iban    = cs.iban if cs else ""
    co_swift   = cs.swift if cs else ""
    co_bank    = cs.bank_name if cs else ""
    co_email   = cs.email if cs else ""
    co_phone   = cs.phone if cs else ""

    logo_path  = None
    sig_path   = None
    stamp_path = None
    if cs:
        if cs.logo:
            try:
                logo_path = cs.logo.path
                if not os.path.isfile(logo_path):
                    logo_path = None
            except Exception:
                logo_path = None
        if cs.invoice_signature:
            try:
                sig_path = cs.invoice_signature.path
                if not os.path.isfile(sig_path):
                    sig_path = None
            except Exception:
                sig_path = None
        if cs.invoice_stamp:
            try:
                stamp_path = cs.invoice_stamp.path
                if not os.path.isfile(stamp_path):
                    stamp_path = None
            except Exception:
                stamp_path = None

    # ── Date formatting ───────────────────────────────────────────────────────
    def _fmt_date(d):
        if not d:
            return ""
        try:
            return d.strftime("%d.%m.%Y")
        except Exception:
            return str(d)

    inv_date_str   = _fmt_date(invoice_date)
    order_date_str = _fmt_date(order.order_date)
    shipped_str    = _fmt_date(order.shipped_at)
    currency       = order.currency or "EUR"

    # ── 1. HEADER ─────────────────────────────────────────────────────────────
    if logo_path:
        logo_cell = Image(logo_path, width=4.5 * cm, height=2 * cm, kind="proportional")
    else:
        logo_cell = _cell([
            _p(co_name, size=12, bold=True, color=_DARK),
        ])

    right_meta = [
        _p(f"Invoice No.: {invoice_number}", size=9, bold=True, align=TA_RIGHT, color=_DARK),
        _p(f"Invoice Date: {inv_date_str}", size=8, align=TA_RIGHT),
        _p(f"Order No.: {order.order_number}", size=8, align=TA_RIGHT),
    ]
    if order_date_str:
        right_meta.append(_p(f"Order Date: {order_date_str}", size=8, align=TA_RIGHT))
    if shipped_str:
        right_meta.append(_p(f"Date of Shipment: {shipped_str}", size=8, align=TA_RIGHT))

    header_data = [[logo_cell, _p("INVOICE", size=18, bold=True, align=TA_CENTER, color=_DARK), right_meta]]
    header_tbl = Table(header_data, colWidths=[5.5 * cm, 6 * cm, 6.5 * cm])
    header_tbl.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(header_tbl)
    story.append(HRFlowable(width="100%", thickness=2, color=_DARK, spaceAfter=10))

    # ── 2. BILL TO / SELLER INFO ──────────────────────────────────────────────
    bill_name = buyer_name or order.client or ""
    bill_addr = buyer_address or ""

    bill_to_content = [_p("Bill To:", size=8, bold=True, color=_DARK)]
    if bill_name:
        bill_to_content.append(_p(bill_name, size=9, bold=True))
    for line in bill_addr.splitlines():
        if line.strip():
            bill_to_content.append(_p(line.strip(), size=8))
    if buyer_vat_id:
        bill_to_content.append(_p(f"VAT ID: {buyer_vat_id}", size=8))

    seller_content = [_p("From:", size=8, bold=True, color=_DARK)]
    seller_content.append(_p(co_name, size=9, bold=True))
    for line in co_addr_parts:
        seller_content.append(_p(line, size=8))
    if co_vat:
        seller_content.append(_p(f"VAT: {co_vat}", size=8))
    if co_email:
        seller_content.append(_p(co_email, size=8))
    if co_phone:
        seller_content.append(_p(co_phone, size=8))

    addr_tbl = Table(
        [[bill_to_content, seller_content]],
        colWidths=[page_w * 0.55, page_w * 0.45],
    )
    addr_tbl.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
    ]))
    story.append(addr_tbl)

    # ── 3. SHIP TO ────────────────────────────────────────────────────────────
    ship_company = order.ship_company or ""
    ship_person  = order.ship_name or order.contact_name or ""
    ship_street  = order.addr_street or ""
    city_zip     = " ".join(filter(None, [order.addr_zip, order.addr_city]))
    ship_country = order.addr_country or ""

    if ship_company or ship_street:
        ship_content = [_p("Shipped To:", size=8, bold=True, color=_DARK)]
        if ship_company:
            ship_content.append(_p(ship_company, size=8, bold=True))
        if ship_person and ship_person != ship_company:
            ship_content.append(_p(ship_person, size=8))
        if ship_street:
            ship_content.append(_p(ship_street, size=8))
        addr_line2 = ", ".join(filter(None, [city_zip, ship_country]))
        if addr_line2:
            ship_content.append(_p(addr_line2, size=8))
        if ship_vat_id:
            ship_content.append(_p(f"VAT ID: {ship_vat_id}", size=8))

        ship_tbl = Table(
            [[ship_content, []]],
            colWidths=[page_w * 0.55, page_w * 0.45],
        )
        ship_tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING",    (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ]))
        story.append(ship_tbl)

    story.append(HRFlowable(width="100%", thickness=0.5, color=_GREY, spaceBefore=4, spaceAfter=8))

    # ── 4. LINE ITEMS ─────────────────────────────────────────────────────────
    lines = list(order.lines.select_related("product").all())
    col_widths = [0.8 * cm, 7.8 * cm, 1.8 * cm, 1.5 * cm, 2.8 * cm, 3.3 * cm]

    headers = [
        _p("#",              size=8, bold=True, color=_DARK, align=TA_CENTER),
        _p("Description",    size=8, bold=True, color=_DARK),
        _p("Qty",            size=8, bold=True, color=_DARK, align=TA_RIGHT),
        _p("Unit",           size=8, bold=True, color=_DARK, align=TA_CENTER),
        _p("Unit Price",     size=8, bold=True, color=_DARK, align=TA_RIGHT),
        _p(f"Amount ({currency})", size=8, bold=True, color=_DARK, align=TA_RIGHT),
    ]

    rows = []
    subtotal = Decimal("0")

    for i, line in enumerate(lines, 1):
        sku  = line.sku_raw or (line.product.sku if line.product else "")
        name = line.product.name if line.product else ""
        if sku and name and sku != name:
            desc = f"{sku} — {name}"
        else:
            desc = name or sku or ""

        qty        = line.qty
        unit_price = line.unit_price or Decimal("0")
        if line.total_price:
            line_total = _r2(line.total_price)
        else:
            line_total = _r2(unit_price * qty)
        subtotal += line_total

        unit_label = "pcs"
        if line.product:
            unit_label = _UNIT_LABEL.get(
                getattr(line.product, "unit_type", ""), "pcs"
            )

        rows.append([
            _p(str(i),                  size=8, align=TA_CENTER),
            _p(desc,                    size=8),
            _p(f"{qty:g}",              size=8, align=TA_RIGHT),
            _p(unit_label,              size=8, align=TA_CENTER),
            _p(f"{unit_price:,.4f}",    size=8, align=TA_RIGHT),
            _p(f"{line_total:,.2f}",    size=8, align=TA_RIGHT),
        ])

    items_tbl = Table([headers] + rows, colWidths=col_widths, repeatRows=1)
    row_bgs = [(_SUBTLE if idx % 2 == 0 else _WHITE) for idx in range(len(rows))]

    style_cmds = [
        ("BACKGROUND",    (0, 0), (-1, 0), _LIGHT),
        ("TEXTCOLOR",     (0, 0), (-1, 0), _DARK),
        ("FONTNAME",      (0, 0), (-1, 0), _FONT_BOLD),
        ("FONTSIZE",      (0, 0), (-1, 0), 8),
        ("TOPPADDING",    (0, 0), (-1, 0), 5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        ("FONTSIZE",      (0, 1), (-1, -1), 8),
        ("TOPPADDING",    (0, 1), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (-1, 0), (-1, -1), 5),
        ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#bdbdbd")),
        ("ALIGN",         (2, 1), (2, -1), "RIGHT"),
        ("ALIGN",         (3, 1), (3, -1), "CENTER"),
        ("ALIGN",         (4, 1), (5, -1), "RIGHT"),
    ]
    for idx, bg in enumerate(row_bgs):
        style_cmds.append(("BACKGROUND", (0, idx + 1), (-1, idx + 1), bg))
    items_tbl.setStyle(TableStyle(style_cmds))
    story.append(items_tbl)
    story.append(Spacer(1, 0.3 * cm))

    # ── 5. TOTALS ─────────────────────────────────────────────────────────────
    discount_amount = _r2(subtotal * discount_pct / 100) if discount_pct else Decimal("0")
    net_goods       = _r2(subtotal - discount_amount)
    ship_cost       = _r2(order.shipping_cost or Decimal("0"))
    ship_currency   = order.shipping_currency or currency
    vat_base        = net_goods
    vat_amount      = _r2(vat_base * vat_rate / 100) if vat_rate else Decimal("0")
    grand_total     = _r2(net_goods + vat_amount + ship_cost)

    totals_data = []

    def _trow(label, amount, curr=None, bold=False, color=_BLACK):
        c = curr or currency
        return [
            _p(label, size=9, bold=bold, align=TA_RIGHT, color=color),
            _p(f"{c} {amount:,.2f}", size=9, bold=bold, align=TA_RIGHT, color=color),
        ]

    totals_data.append(_trow("Subtotal:", subtotal))
    if discount_amount:
        totals_data.append(_trow(
            f"Discount ({discount_pct:.0f}%):",
            -discount_amount,
            color=colors.HexColor("#c62828"),
        ))
    if ship_cost:
        totals_data.append(_trow("Shipping:", ship_cost, curr=ship_currency))
    totals_data.append(_trow("VAT Base:", vat_base))
    totals_data.append(_trow(f"VAT ({vat_rate:.0f}%):", vat_amount))
    totals_data.append(_trow("TOTAL:", grand_total, bold=True, color=_DARK))

    totals_col = page_w - 4.6 * cm
    totals_tbl = Table(totals_data, colWidths=[totals_col, 4.6 * cm])
    totals_style = [
        ("ALIGN",         (0, 0), (-1, -1), "RIGHT"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LINEABOVE",     (0, -1), (-1, -1), 1, _DARK),
        ("TOPPADDING",    (0, -1), (-1, -1), 6),
    ]
    totals_tbl.setStyle(TableStyle(totals_style))
    story.append(totals_tbl)
    story.append(HRFlowable(width="100%", thickness=0.5, color=_GREY, spaceBefore=8, spaceAfter=8))

    # ── 6. PAYMENT & SIGNATURE ────────────────────────────────────────────────
    pay_content = [_p("Payment Details:", size=8, bold=True, color=_DARK)]
    if co_iban:
        pay_content.append(_p(f"IBAN:      {co_iban}", size=8))
    if co_swift:
        pay_content.append(_p(f"SWIFT/BIC: {co_swift}", size=8))
    if co_bank:
        pay_content.append(_p(f"Bank:      {co_bank}", size=8))
    pay_content.append(Spacer(1, 0.15 * cm))
    pay_content.append(_p(
        f"Payment reference: Invoice {invoice_number} / Order {order.order_number}",
        size=8,
    ))

    sig_content = []
    if sig_path:
        sig_content.append(Image(sig_path, width=3.2 * cm, height=1.6 * cm, kind="proportional"))
    sig_content.append(_p("Authorized Signature", size=7, color=_GREY, align=TA_CENTER))

    stamp_content = []
    if stamp_path:
        stamp_content.append(Image(stamp_path, width=2.8 * cm, height=2.8 * cm, kind="proportional"))

    bottom_data = [[pay_content, sig_content, stamp_content]]
    bottom_col_w = page_w - 7.2 * cm
    bottom_tbl = Table(bottom_data, colWidths=[bottom_col_w, 4.2 * cm, 3 * cm])
    bottom_tbl.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "BOTTOM"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
    ]))
    story.append(bottom_tbl)

    # ── 7. FOOTER ─────────────────────────────────────────────────────────────
    footer_parts = [co_name]
    co_addr_str = ", ".join(co_addr_parts)
    if co_addr_str:
        footer_parts.append(co_addr_str)
    if co_vat:
        footer_parts.append(f"VAT: {co_vat}")
    contacts = " | ".join(filter(None, [co_email, co_phone]))
    if contacts:
        footer_parts.append(contacts)

    story.append(HRFlowable(width="100%", thickness=0.5, color=_GREY, spaceBefore=6, spaceAfter=3))
    story.append(_p(" · ".join(footer_parts), size=7, color=_GREY, align=TA_CENTER))

    doc.build(story)
    return buf.getvalue()


def get_next_invoice_number() -> int:
    """Return max(eu_invoice_number) + 1 across all SalesOrders."""
    from django.db.models import Max
    from sales.models import SalesOrder
    result = SalesOrder.objects.aggregate(m=Max("eu_invoice_number"))["m"]
    return (result or 0) + 1
