"""
DigiKey Marketplace Packing List PDF generator.
Replicates the official DigiKey packing list format using live API order data.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)


# ── Font ──────────────────────────────────────────────────────────────────────

def _init_fonts():
    candidates = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
        (r"C:\Windows\Fonts\DejaVuSans.ttf", r"C:\Windows\Fonts\DejaVuSans-Bold.ttf"),
        (r"C:\Windows\Fonts\arial.ttf",      r"C:\Windows\Fonts\arialbd.ttf"),
    ]
    for reg, bold in candidates:
        if os.path.isfile(reg) and os.path.isfile(bold):
            try:
                pdfmetrics.registerFont(TTFont("DKFont",     reg))
                pdfmetrics.registerFont(TTFont("DKFont-Bold", bold))
                return "DKFont", "DKFont-Bold"
            except Exception:
                pass
    return "Helvetica", "Helvetica-Bold"


_FONT, _FONT_BOLD = _init_fonts()


# ── Rotated label flowable (for "Sold To:" / "Ship To:" vertical text) ────────

class _RotatedLabel(Flowable):
    def __init__(self, text, w, h, font_size=7):
        super().__init__()
        self._w = w
        self._h = h
        self._text = text
        self._fs = font_size

    def wrap(self, *args):
        return self._w, self._h

    def draw(self):
        c = self.canv
        c.saveState()
        c.setFont(_FONT_BOLD, self._fs)
        c.translate(self._w / 2, self._h / 2)
        c.rotate(90)
        c.drawCentredString(0, -self._fs / 3, self._text)
        c.restoreState()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ps(size=8, bold=False, align=TA_LEFT, color=colors.black):
    return ParagraphStyle(
        f"dk{size}{'b' if bold else ''}",
        parent=getSampleStyleSheet()["Normal"],
        fontSize=size,
        fontName=_FONT_BOLD if bold else _FONT,
        textColor=color,
        alignment=align,
        leading=size + 3,
    )


def _p(text, size=8, bold=False, align=TA_LEFT):
    return Paragraph(str(text or ""), _ps(size, bold, align))


def _fmt_address(addr: dict) -> list[str]:
    """Format a DigiKey address dict → list of uppercase lines."""
    if not addr:
        return []
    lines = []
    company = (addr.get("companyName") or "").strip()
    first   = (addr.get("firstName") or "").strip()
    last    = (addr.get("lastName") or "").strip()
    name    = f"{first} {last}".strip()
    if company:
        lines.append(company.upper())
    if name:
        lines.append(name.upper())
    for key in ("street1", "street2"):
        val = (addr.get(key) or "").strip()
        if val:
            lines.append(val.upper())
    city    = (addr.get("city") or "").strip().upper()
    state   = (addr.get("state") or "").strip().upper()
    postal  = (addr.get("postalCode") or "").strip()
    country = (addr.get("countryCode") or "").strip().upper()
    city_parts = [p for p in [city, state] if p]
    city_line  = ", ".join(city_parts)
    if postal:
        city_line = f"{city_line} {postal}".strip() if city_line else postal
    if city_line:
        lines.append(city_line)
    if country:
        lines.append(country)
    return lines


def _parse_date(s: str) -> str:
    """ISO datetime → 'DD-Mon-YYYY' like DigiKey."""
    if not s:
        return "—"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%d-%b-%Y")
    except Exception:
        return s[:10] if s else "—"


def _get_additional_field(fields: list, code: str) -> str:
    for f in fields or []:
        if f.get("code") == code:
            return f.get("value") or ""
    return ""


# ── Main generator ────────────────────────────────────────────────────────────

def generate_digikey_packing_list(api_order: dict, supplier: dict) -> BytesIO:
    """
    Generate a DigiKey-style packing list PDF.

    api_order : raw dict from GET /Sales/Marketplace2/Orders/v1/orders (single order)
    supplier  : {'name': str, 'street': str, 'city_zip': str, 'country': str}
                → our company address from AccountingSettings
    """
    buf = BytesIO()
    PAGE_SIZE = landscape(A4)          # 29.7 × 21 cm horizontal
    W, _ = PAGE_SIZE
    MARGIN  = 1.5 * cm
    PAGE_W  = W - 2 * MARGIN          # ≈ 26.7 cm

    doc = SimpleDocTemplate(
        buf, pagesize=PAGE_SIZE,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
    )

    story = []

    # ── Order-level data ──────────────────────────────────────────────────────
    customer      = api_order.get("customer") or {}
    add_fields    = api_order.get("additionalFields") or []
    order_details = api_order.get("orderDetails") or []

    bill_addr = customer.get("billingAddress") or {}
    ship_addr = customer.get("shippingAddress") or bill_addr

    shipping_method = (
        api_order.get("shippingMethodName") or
        api_order.get("shippingMethodCode") or
        api_order.get("shippingMethodLabel") or ""
    ).upper()

    salesorder_id = api_order.get("businessId") or api_order.get("id") or "—"
    po_number     = _get_additional_field(add_fields, "customer-purchase-order-number")
    order_date    = _parse_date(api_order.get("createDateUtc") or "")
    doc_date      = date.today().strftime("%d-%b-%Y")
    shippable     = len(order_details)

    # ── 1. Header: supplier address (left) | shipping method (right) ──────────
    sup_lines = []
    if supplier.get("name"):
        sup_lines.append(Paragraph(f"<b>{supplier['name']}</b>", _ps(9, bold=True)))
    for key in ("street", "city_zip", "country"):
        val = (supplier.get(key) or "").strip()
        if val:
            sup_lines.append(Paragraph(val.upper(), _ps(9)))

    hdr_data = [[
        sup_lines,
        _p(f"Shipping: {shipping_method}" if shipping_method else "", 9, bold=True, align=TA_RIGHT),
    ]]
    hdr_tbl = Table(hdr_data, colWidths=[PAGE_W * 0.55, PAGE_W * 0.45])
    hdr_tbl.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(hdr_tbl)
    story.append(Spacer(1, 0.8 * cm))

    # ── 2. Three-column row: [Sold To box] [Ship To box] [Order info] ─────────
    BOX_H   = 3.6 * cm
    BOX_W   = 7.5 * cm
    INFO_W  = PAGE_W - 2 * BOX_W - 0.8 * cm
    LABEL_W = 0.45 * cm

    def _addr_box(label_text, addr_lines):
        label_fl = _RotatedLabel(label_text, LABEL_W, BOX_H - 0.3 * cm)
        addr_para = Paragraph(
            "<br/>".join(addr_lines) if addr_lines else "—",
            _ps(8),
        )
        inner = Table(
            [[label_fl, addr_para]],
            colWidths=[LABEL_W, BOX_W - LABEL_W],
            rowHeights=[BOX_H],
        )
        inner.setStyle(TableStyle([
            ("BOX",           (0, 0), (-1, -1), 0.75, colors.black),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING",   (0, 0), (0,  -1), 0),
            ("RIGHTPADDING",  (0, 0), (0,  -1), 0),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING",   (1, 0), (1,  -1), 6),
            ("RIGHTPADDING",  (1, 0), (1,  -1), 6),
            ("LINEBEFORE",    (1, 0), (1,  -1), 0.3, colors.HexColor("#bbbbbb")),
        ]))
        return inner

    sold_to_box = _addr_box("Sold To:", _fmt_address(bill_addr))
    ship_to_box = _addr_box("Ship To:", _fmt_address(ship_addr))

    def _kv(label, value):
        return Paragraph(f"<b>{label}</b> {value or ''}", _ps(8))

    info_content = [
        _kv("Salesorder:", salesorder_id),
        _kv("Customer PO:", po_number),
        _kv("Order Date:", order_date),
        _kv("Document Date:", doc_date),
        _kv("Shippable Items:", str(shippable)),
    ]

    three_col = Table(
        [[sold_to_box, ship_to_box, info_content]],
        colWidths=[BOX_W, BOX_W, INFO_W],
        rowHeights=[BOX_H],
    )
    three_col.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING",   (1, 0), (1,  -1), 6),
        ("LEFTPADDING",   (2, 0), (2,  -1), 10),
    ]))
    story.append(three_col)
    story.append(Spacer(1, 0.5 * cm))

    # ── 3. Line items table ───────────────────────────────────────────────────
    ORD_W  = 1.8 * cm
    LINE_W = 1.8 * cm
    DESC_W = PAGE_W - ORD_W - LINE_W - 1.8 * cm
    SHIP_W = 1.8 * cm

    BLACK = colors.black
    GREY  = colors.HexColor("#f5f5f5")

    hdr = [
        _p("Ordered",              8, bold=True, align=TA_CENTER),
        _p("Line Item",            8, bold=True, align=TA_CENTER),
        _p("Item Number/Description", 8, bold=True),
        _p("Shipped",              8, bold=True, align=TA_CENTER),
    ]
    tbl_data = [hdr]

    for idx, line in enumerate(order_details, 1):
        qty_ord  = line.get("quantity") or 0
        qty_ship = line.get("adjustedQuantity") or qty_ord

        part_num = line.get("productPartNumber") or line.get("supplierSku") or "—"
        desc     = line.get("productDescription") or line.get("offerDescription") or ""
        mfg_pn   = line.get("manufacturerPartNumber") or ""
        mfg      = line.get("manufacturer") or ""

        # ECCN and HTSUS are not available in Marketplace API
        eccn  = ""
        htsus = ""

        desc_html = (
            f"<b>PART:</b> {part_num}"
            + (f"&nbsp;&nbsp;&nbsp;&nbsp;<b>DESC:</b> {desc}" if desc else "")
            + (f"&nbsp;&nbsp;&nbsp;&nbsp;<b>ECCN:</b> {eccn}" if eccn else "")
            + "<br/>"
            + (f"<b>MFG#:</b> {mfg_pn}" if mfg_pn else "")
            + (f"&nbsp;&nbsp;&nbsp;&nbsp;<b>HTSUS:</b> {htsus}" if htsus else "")
            + "<br/><b>CUST REF#:</b>"
            + (f"<br/><b>MFG:</b> {mfg}" if mfg else "")
        )

        tbl_data.append([
            _p(str(qty_ord),  8, align=TA_CENTER),
            _p(str(idx),      8, align=TA_CENTER),
            Paragraph(desc_html, _ps(8)),
            _p(str(qty_ship), 8, align=TA_CENTER),
        ])

    items_tbl = Table(
        tbl_data,
        colWidths=[ORD_W, LINE_W, DESC_W, SHIP_W],
    )
    n = len(tbl_data)
    style = TableStyle([
        ("GRID",          (0, 0), (-1, -1), 0.5, BLACK),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("FONTNAME",      (0, 0), (-1,  0), _FONT_BOLD),
    ])
    # Alternating row background for data rows
    for i in range(1, n):
        if i % 2 == 0:
            style.add("BACKGROUND", (0, i), (-1, i), GREY)
    items_tbl.setStyle(style)
    story.append(items_tbl)

    doc.build(story)
    buf.seek(0)
    return buf
