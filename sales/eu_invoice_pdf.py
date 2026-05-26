"""EU Invoice PDF generator — layout matches Invoice_10229 template."""
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
    HRFlowable, Image, KeepTogether,
)
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER

try:
    from sales.doc_generators import _FONT, _FONT_BOLD, _UNIT_LABEL
except Exception:
    _FONT, _FONT_BOLD = "Helvetica", "Helvetica-Bold"
    _UNIT_LABEL = {}


def _r2(v) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _eu_num(amount: Decimal) -> str:
    """Format with comma decimal separator: 79,99"""
    s = f"{amount:,.2f}"          # "79.99" or "1,234.56"
    # swap . and , (US→EU format)
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _p(text, size=9, bold=False, align=TA_LEFT, color=colors.black, leading=None):
    font = _FONT_BOLD if bold else _FONT
    ps = ParagraphStyle(
        "_p",
        fontName=font, fontSize=size,
        leading=leading or (size * 1.35),
        textColor=color, alignment=align,
        spaceAfter=0, spaceBefore=0,
    )
    return Paragraph(str(text) if text is not None else "", ps)


def _safe_img(path, width, height):
    if path and os.path.isfile(path):
        try:
            return Image(path, width=width, height=height, kind="proportional")
        except Exception:
            pass
    return None


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
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2.0 * cm, rightMargin=2.0 * cm,
        topMargin=1.5 * cm, bottomMargin=2.2 * cm,
    )
    page_w = A4[0] - 4.0 * cm   # usable width ≈ 17.1 cm

    story = []

    # ── Load company settings ─────────────────────────────────────────────────
    try:
        from accounting.models import CompanySettings
        cs = CompanySettings.get()
    except Exception:
        cs = None

    co_name   = (cs.legal_name or cs.name) if cs else "Seller"
    co_street = cs.addr_street if cs else ""
    co_city   = " ".join(filter(None, [cs.addr_zip if cs else "", cs.addr_city if cs else ""]))
    co_country = cs.addr_country if cs else ""
    co_vat    = cs.vat_id if cs else ""
    co_iban   = cs.iban if cs else ""
    co_swift  = cs.swift if cs else ""
    co_bank   = cs.bank_name if cs else ""
    co_email  = cs.email if cs else ""
    co_phone  = cs.phone if cs else ""
    co_fax    = getattr(cs, "fax", "") or ""
    co_mobile = getattr(cs, "mobile", "") or ""
    co_website = getattr(cs, "website", "") or ""
    co_eori   = getattr(cs, "eori", "") or ""
    co_tax_id = getattr(cs, "tax_id", "") or ""
    co_reg    = getattr(cs, "registration_court", "") or ""
    co_ceo    = getattr(cs, "ceo_name", "") or ""

    logo_img  = _safe_img(cs.logo.path if cs and cs.logo else None,    4.5*cm, 2.0*cm)
    sig_img   = _safe_img(cs.invoice_signature.path if cs and cs.invoice_signature else None, 3.5*cm, 1.8*cm)

    # ── Date helpers ──────────────────────────────────────────────────────────
    def _fd(d):
        if not d:
            return ""
        try:
            return d.strftime("%m/%d/%Y")
        except Exception:
            return str(d)

    currency       = order.currency or "USD"
    inv_date_str   = _fd(invoice_date)
    order_date_str = _fd(order.order_date)
    shipped_str    = _fd(order.shipped_at)

    # ═════════════════════════════════════════════════════════════════════════
    # 1. TOP HEADER: Invoice No. (left) | Logo + company block (right)
    # ═════════════════════════════════════════════════════════════════════════
    inv_no_cell = [_p(f"Invoice No.: {invoice_number}", size=10, bold=True)]

    # Company info block (right side)
    right_block = []
    if logo_img:
        right_block.append(logo_img)
    else:
        right_block.append(_p(co_name, size=13, bold=True))
    right_block.append(Spacer(1, 0.15*cm))
    right_block.append(_p(f"<b>{co_name}</b>", size=9, align=TA_RIGHT))
    if co_street:
        right_block.append(_p(co_street, size=9, align=TA_RIGHT))
    if co_city:
        right_block.append(_p(co_city, size=9, align=TA_RIGHT))
    if co_country:
        right_block.append(_p(co_country, size=9, align=TA_RIGHT))
    right_block.append(Spacer(1, 0.1*cm))
    if co_phone:
        right_block.append(_p(f"Tel.:  {co_phone}", size=8, align=TA_RIGHT))
    if co_fax:
        right_block.append(_p(f"Fax:  {co_fax}", size=8, align=TA_RIGHT))
    if co_mobile:
        right_block.append(_p(f"Mob:  {co_mobile}", size=8, align=TA_RIGHT))
    right_block.append(Spacer(1, 0.1*cm))
    if co_email:
        right_block.append(_p(f"E-Mail:   {co_email}", size=8, align=TA_RIGHT))
    if co_website:
        right_block.append(_p(f"Internet:  {co_website}", size=8, align=TA_RIGHT))

    header_tbl = Table(
        [[inv_no_cell, right_block]],
        colWidths=[page_w * 0.45, page_w * 0.55],
    )
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (-1, 0), (-1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 0.6*cm))

    # ═════════════════════════════════════════════════════════════════════════
    # 2. BILL-TO ADDRESS (left) — return-address line above
    # ═════════════════════════════════════════════════════════════════════════
    # Small return-address underlined text
    return_addr = ", ".join(filter(None, [co_name, co_street, co_city, co_country]))
    bill_name   = buyer_name or order.client or ""
    bill_lines  = [bill_name] + [l.strip() for l in buyer_address.splitlines() if l.strip()]

    addr_cell = []
    if return_addr:
        addr_cell.append(_p(f"<u>{return_addr}</u>", size=7,
                            color=colors.HexColor("#555555")))
        addr_cell.append(Spacer(1, 0.1*cm))
    for i, line in enumerate(bill_lines):
        addr_cell.append(_p(line, size=10 if i == 0 else 9,
                            bold=(i == 0)))

    addr_tbl = Table([[addr_cell, []]], colWidths=[page_w * 0.50, page_w * 0.50])
    addr_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (-1, 0), (-1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]))
    story.append(addr_tbl)
    story.append(Spacer(1, 0.8*cm))

    # ═════════════════════════════════════════════════════════════════════════
    # 3. INVOICE TITLE
    # ═════════════════════════════════════════════════════════════════════════
    story.append(_p("INVOICE", size=18, bold=True, align=TA_CENTER))
    story.append(Spacer(1, 0.6*cm))

    # ═════════════════════════════════════════════════════════════════════════
    # 4. REFERENCE BLOCK: Your… | Our…
    # ═════════════════════════════════════════════════════════════════════════
    left_refs = [
        _p(f"Your Order No.: <b>{order.order_number}</b>", size=9),
        _p(f"Your Order Date: {order_date_str}", size=9),
    ]
    if buyer_vat_id:
        left_refs.append(_p(f"<b>Your VAT ID: {buyer_vat_id}</b>", size=9))
    if shipped_str:
        left_refs.append(_p(f"Date of shipment: {shipped_str}", size=9))

    right_refs = [
        _p(f"Our Invoice No.: <b>{invoice_number}</b>", size=9),
        _p(f"Our Invoice Date: {inv_date_str}", size=9),
    ]
    if co_vat:
        right_refs.append(_p(f"<b>Our VAT ID: {co_vat}</b>", size=9))
    if co_eori:
        right_refs.append(_p(f"Our EORI: {co_eori}", size=9))

    ref_tbl = Table([[left_refs, right_refs]], colWidths=[page_w * 0.50, page_w * 0.50])
    ref_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (-1, 0), (-1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
    ]))
    story.append(ref_tbl)
    story.append(Spacer(1, 0.5*cm))

    # ═════════════════════════════════════════════════════════════════════════
    # 5. GREETING
    # ═════════════════════════════════════════════════════════════════════════
    story.append(_p("Dear Ladies and Gentlemen,", size=9))
    story.append(Spacer(1, 0.2*cm))
    story.append(_p("Herewith we would like to charge:", size=9))
    story.append(Spacer(1, 0.3*cm))

    # ═════════════════════════════════════════════════════════════════════════
    # 6. ITEMS TABLE
    # ═════════════════════════════════════════════════════════════════════════
    lines = list(order.lines.select_related("product").all())

    col_w = [1.2*cm, 8.5*cm, 2.5*cm, 2.5*cm, 2.4*cm]
    hdr_ps = ParagraphStyle("th", fontName=_FONT_BOLD, fontSize=9,
                             leading=12, alignment=TA_CENTER)
    headers = [
        Paragraph("Pos.", hdr_ps),
        Paragraph("Description", hdr_ps),
        Paragraph(f"Quantity,\nUnits", hdr_ps),
        Paragraph(f"Price/Unit,\n{currency}", hdr_ps),
        Paragraph(f"Amount,\n{currency}", hdr_ps),
    ]

    subtotal = Decimal("0")
    item_rows = []

    for i, line in enumerate(lines, 1):
        sku  = line.sku_raw or (line.product.sku if line.product else "")
        name = line.product.name if line.product else ""
        # Two-line description: SKU bold + product name
        if sku and name and sku != name:
            desc_text = f"<b>{sku}</b><br/>{name}"
        else:
            desc_text = f"<b>{sku or name}</b>"

        qty_val    = line.qty
        unit_price = line.unit_price or Decimal("0")
        if line.total_price:
            line_total = _r2(line.total_price)
        else:
            line_total = _r2(unit_price * qty_val)
        subtotal += line_total

        unit_label = "pcs"
        if line.product:
            unit_label = _UNIT_LABEL.get(getattr(line.product, "unit_type", ""), "pcs")

        item_rows.append([
            _p(str(i), size=9, align=TA_CENTER),
            Paragraph(desc_text, ParagraphStyle("d", fontName=_FONT, fontSize=9, leading=12)),
            _p(f"{qty_val:g}", size=9, align=TA_CENTER),
            _p(_eu_num(unit_price),   size=9, align=TA_RIGHT),
            _p(_eu_num(line_total),   size=9, align=TA_RIGHT),
        ])

    # Discount row
    discount_amount = _r2(subtotal * discount_pct / 100) if discount_pct else Decimal("0")
    if discount_amount:
        disc_label = f"Discount {discount_pct:.0f}%"
        item_rows.append([
            _p(str(len(lines) + 1), size=9, align=TA_CENTER),
            _p(disc_label, size=9),
            _p("", size=9), _p("", size=9),
            _p(f"- {_eu_num(discount_amount)}", size=9, align=TA_RIGHT),
        ])

    # Shipping row
    ship_cost    = _r2(order.shipping_cost or Decimal("0"))
    ship_pos     = len(lines) + (2 if discount_amount else 1)
    if ship_cost:
        item_rows.append([
            _p(str(ship_pos), size=9, align=TA_CENTER),
            _p("Shipping Charges", size=9),
            _p("1,0", size=9, align=TA_CENTER),
            _p(_eu_num(ship_cost), size=9, align=TA_RIGHT),
            _p(_eu_num(ship_cost), size=9, align=TA_RIGHT),
        ])

    # Totals rows
    net_goods   = _r2(subtotal - discount_amount)
    vat_base    = net_goods
    vat_amount  = _r2(vat_base * vat_rate / 100) if vat_rate else Decimal("0")
    grand_total = _r2(net_goods + vat_amount + ship_cost)

    _s = ParagraphStyle("ts", fontName=_FONT, fontSize=9, leading=12)
    _sb = ParagraphStyle("tsb", fontName=_FONT_BOLD, fontSize=9, leading=12)

    def _trow(label, amount, bold=False):
        ps = _sb if bold else _s
        return [
            Paragraph("", ps),
            Paragraph(label, ps),
            Paragraph("", ps),
            Paragraph("", ps),
            Paragraph(_eu_num(amount), ps if not bold else _sb),
        ]

    item_rows.append(_trow("Total Amount without VAT", net_goods + ship_cost))
    item_rows.append(_trow(f"VAT {vat_rate:.0f}%", vat_amount))
    item_rows.append(_trow("Total Amount with VAT", grand_total, bold=True))

    n_items = len(lines) + (1 if discount_amount else 0) + (1 if ship_cost else 0)
    n_total_rows = 3  # subtotal, vat, grand total

    all_rows  = [headers] + item_rows
    items_tbl = Table(all_rows, colWidths=col_w, repeatRows=1)

    n_data = len(all_rows)
    first_total_row = 1 + n_items + 1  # header + items + 1 (0-indexed)

    style_cmds = [
        # outer border
        ("BOX",       (0, 0), (-1, -1), 0.8, colors.black),
        # header row
        ("BOX",       (0, 0), (-1, 0), 1.0, colors.black),
        ("FONTNAME",  (0, 0), (-1, 0), _FONT_BOLD),
        ("FONTSIZE",  (0, 0), (-1, 0), 9),
        ("ALIGN",     (0, 0), (-1, 0), "CENTER"),
        ("VALIGN",    (0, 0), (-1, 0), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, 0), 5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        # data rows
        ("FONTSIZE",  (0, 1), (-1, -1), 9),
        ("TOPPADDING",    (0, 1), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (-1, 0), (-1, -1), 5),
        # inner grid for item rows
        ("INNERGRID", (0, 0), (-1, first_total_row - 1), 0.4, colors.black),
        # align amounts right
        ("ALIGN",     (2, 1), (2, -1), "CENTER"),
        ("ALIGN",     (3, 1), (4, -1), "RIGHT"),
        # total rows separator
        ("LINEABOVE", (0, first_total_row), (-1, first_total_row), 0.8, colors.black),
        ("LINEBELOW", (0, -1), (-1, -1), 0.8, colors.black),
        # grand total bold
        ("FONTNAME",  (0, -1), (-1, -1), _FONT_BOLD),
    ]
    items_tbl.setStyle(TableStyle(style_cmds))
    story.append(items_tbl)
    story.append(Spacer(1, 0.6*cm))

    # ═════════════════════════════════════════════════════════════════════════
    # 7. SHIPPED TO / FROM
    # ═════════════════════════════════════════════════════════════════════════
    ship_company = order.ship_company or ""
    ship_person  = order.ship_name or order.contact_name or ""
    ship_street  = order.addr_street or ""
    city_zip     = " ".join(filter(None, [order.addr_zip, order.addr_city]))
    ship_country = order.addr_country or ""

    to_cell = [_p("<b>Shipped to:</b>", size=9)]
    for line in filter(None, [ship_company, ship_person, ship_street,
                               f"{city_zip}, {ship_country}".strip(", ")]):
        to_cell.append(_p(line, size=9))
    if ship_vat_id:
        to_cell.append(_p(f"VAT ID: {ship_vat_id}", size=9))

    from_cell = [_p("<b>Shipped from:</b>", size=9)]
    for line in filter(None, [co_name, co_street, co_city, co_country]):
        from_cell.append(_p(line, size=9))
    if co_phone:
        from_cell.append(_p(f"Phone: {co_phone}", size=9))
    if co_vat:
        from_cell.append(_p(f"VAT ID: {co_vat}", size=9))

    ship_tbl = Table([[to_cell, from_cell]], colWidths=[page_w * 0.50, page_w * 0.50])
    ship_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (-1, 0), (-1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]))
    story.append(ship_tbl)
    story.append(Spacer(1, 0.7*cm))

    # ═════════════════════════════════════════════════════════════════════════
    # 8. SIGNATURE
    # ═════════════════════════════════════════════════════════════════════════
    sig_block = [_p("Sincerely yours,", size=9)]
    sig_block.append(Spacer(1, 0.2*cm))
    if sig_img:
        sig_block.append(sig_img)
    else:
        sig_block.append(Spacer(1, 1.2*cm))
    if co_ceo:
        sig_block.append(_p(co_ceo, size=9))
    story.append(KeepTogether(sig_block))

    # ═════════════════════════════════════════════════════════════════════════
    # 9. FOOTER
    # ═════════════════════════════════════════════════════════════════════════
    story.append(Spacer(1, 0.4*cm))
    story.append(HRFlowable(width="100%", thickness=0.8, color=colors.black,
                             spaceBefore=0, spaceAfter=3))

    footer_grey = colors.HexColor("#444444")
    # Line 1: Company name + VAT + IBAN
    f1_left  = f"{co_name}, Principal Office: {co_city}" if co_city else co_name
    f1_mid   = f"VAT ID:  {co_vat}" if co_vat else ""
    f1_right = f"IBAN:  {co_iban}" if co_iban else ""
    # Line 2: Registration + TAX + BIC
    f2_left  = f"Registration Court: {co_reg}" if co_reg else ""
    f2_mid   = f"TAX ID:  {co_tax_id}" if co_tax_id else ""
    f2_right = f"BIC / SWIFT:  {co_swift}" if co_swift else ""

    def _fp(txt):
        return _p(txt, size=7, color=footer_grey, align=TA_CENTER)

    footer_data = [
        [_fp(f1_left), _fp(f1_mid), _fp(f1_right)],
        [_fp(f2_left), _fp(f2_mid), _fp(f2_right)],
    ]
    footer_tbl = Table(footer_data, colWidths=[page_w * 0.38, page_w * 0.31, page_w * 0.31])
    footer_tbl.setStyle(TableStyle([
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (-1, 0), (-1, -1), 2),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]))
    story.append(footer_tbl)

    doc.build(story)
    return buf.getvalue()


def get_next_invoice_number() -> int:
    from django.db.models import Max
    from sales.models import SalesOrder
    result = SalesOrder.objects.aggregate(m=Max("eu_invoice_number"))["m"]
    return (result or 0) + 1
