"""
accounting/views.py — PDF генерація рахунків через ReportLab.
"""
from decimal import Decimal
from io import BytesIO

from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404

from .models import Invoice, CompanySettings


@staff_member_required
def invoice_pdf(request, pk):
    """Генерує PDF для рахунку-фактури."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
        )
        from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
    except ImportError:
        return HttpResponse(
            "ReportLab не встановлено. Виконайте: pip install reportlab",
            content_type="text/plain", status=500
        )

    invoice = get_object_or_404(Invoice, pk=pk)
    cfg = CompanySettings.get()

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm,
    )

    styles = getSampleStyleSheet()
    style_normal = styles["Normal"]
    style_normal.fontSize = 9
    style_normal.leading = 13

    style_h1 = ParagraphStyle(
        "H1", parent=styles["Heading1"],
        fontSize=20, spaceAfter=4, textColor=colors.HexColor("#1a237e"),
    )
    style_h2 = ParagraphStyle(
        "H2", parent=styles["Heading2"],
        fontSize=11, spaceAfter=2,
    )
    style_right = ParagraphStyle(
        "Right", parent=style_normal, alignment=TA_RIGHT
    )
    style_label = ParagraphStyle(
        "Label", parent=style_normal,
        textColor=colors.HexColor("#607d8b"), fontSize=8,
    )
    style_total = ParagraphStyle(
        "Total", parent=style_normal,
        fontSize=11, fontName="Helvetica-Bold",
    )

    elements = []

    # ── Шапка: назва компанії + номер рахунку ─────────────────────────────────
    header_data = [
        [
            Paragraph(f"<b>{cfg.name}</b>", style_h1),
            Paragraph(
                f"<b>INVOICE</b><br/>"
                f"<font size=14 color='#1a237e'>{invoice.number}</font>",
                style_right
            ),
        ]
    ]
    header_table = Table(header_data, colWidths=["60%", "40%"])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
    ]))
    elements.append(header_table)
    elements.append(HRFlowable(width="100%", thickness=2,
                               color=colors.HexColor("#1a237e")))
    elements.append(Spacer(1, 0.4*cm))

    # ── Дві колонки: продавець | покупець ─────────────────────────────────────
    seller_lines = [cfg.name]
    if cfg.legal_name:
        seller_lines.append(cfg.legal_name)
    if cfg.addr_street:
        seller_lines.append(cfg.addr_street)
    if cfg.addr_city or cfg.addr_zip:
        seller_lines.append(f"{cfg.addr_zip} {cfg.addr_city}".strip())
    if cfg.addr_country:
        seller_lines.append(cfg.addr_country)
    if cfg.vat_id:
        seller_lines.append(f"VAT: {cfg.vat_id}")
    if cfg.email:
        seller_lines.append(cfg.email)
    if cfg.phone:
        seller_lines.append(cfg.phone)

    buyer_lines = []
    if invoice.client_name:
        buyer_lines.append(f"<b>{invoice.client_name}</b>")
    if invoice.client_addr:
        buyer_lines.extend(invoice.client_addr.splitlines())
    if invoice.client_vat:
        buyer_lines.append(f"VAT: {invoice.client_vat}")

    seller_text = "<br/>".join(seller_lines)
    buyer_text  = "<br/>".join(buyer_lines) if buyer_lines else "—"

    parties_data = [
        [
            Paragraph("<b>FROM</b>", style_label),
            Paragraph("<b>TO</b>", style_label),
        ],
        [
            Paragraph(seller_text, style_normal),
            Paragraph(buyer_text, style_normal),
        ],
    ]
    parties_table = Table(parties_data, colWidths=["50%", "50%"])
    parties_table.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ]))
    elements.append(parties_table)
    elements.append(Spacer(1, 0.4*cm))

    # ── Дати ──────────────────────────────────────────────────────────────────
    dates_items = [
        ("Issue date", str(invoice.issue_date)),
    ]
    if invoice.service_date:
        dates_items.append(("Service date (Leistungsdatum)", str(invoice.service_date)))
    if invoice.due_date:
        dates_items.append(("Due date", str(invoice.due_date)))
    dates_items.append(("Currency", invoice.currency))

    dates_data = [[Paragraph(f"<b>{k}:</b> {v}", style_normal)] for k, v in dates_items]
    dates_table = Table(dates_data, colWidths=["100%"])
    dates_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#f5f7fa")),
        ("BOX", (0,0), (-1,-1), 0.5, colors.HexColor("#cfd8dc")),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
        ("RIGHTPADDING", (0,0), (-1,-1), 8),
        ("TOPPADDING", (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ]))
    elements.append(dates_table)
    elements.append(Spacer(1, 0.5*cm))

    # ── Таблиця рядків ────────────────────────────────────────────────────────
    col_headers = ["#", "Description", "Qty", "Unit", "Unit Price", "Disc %", "Total"]
    line_rows = [col_headers]

    lines = list(invoice.lines.select_related("product").all())
    for i, line in enumerate(lines, 1):
        line_rows.append([
            str(i),
            line.description,
            str(line.quantity.normalize()),
            line.unit or "шт",
            f"{line.unit_price:.2f}",
            f"{line.discount:.1f}%" if line.discount else "—",
            f"{line.line_total:.2f}",
        ])

    col_widths = [0.8*cm, None, 1.8*cm, 1.5*cm, 2.4*cm, 1.8*cm, 2.4*cm]
    lines_table = Table(line_rows, colWidths=col_widths, repeatRows=1)
    lines_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1a237e")),
        ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
        ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",   (0,0), (-1,0), 9),
        ("ALIGN",      (0,0), (-1,-1), "CENTER"),
        ("ALIGN",      (1,0), (1,-1), "LEFT"),   # Description — ліворуч
        ("ALIGN",      (-1,0), (-1,-1), "RIGHT"), # Total — праворуч
        ("FONTSIZE",   (0,1), (-1,-1), 9),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f5f7fa")]),
        ("GRID",       (0,0), (-1,-1), 0.5, colors.HexColor("#cfd8dc")),
        ("TOPPADDING", (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",  (0,0), (-1,-1), 6),
        ("RIGHTPADDING", (0,0), (-1,-1), 6),
    ]))
    elements.append(lines_table)
    elements.append(Spacer(1, 0.5*cm))

    # ── Підсумки ──────────────────────────────────────────────────────────────
    subtotal   = invoice.subtotal
    vat_amount = invoice.vat_amount
    total      = invoice.total
    paid       = invoice.paid_amount
    balance    = invoice.balance_due

    totals_rows = [
        ["Subtotal:", f"{subtotal:.2f} {invoice.currency}"],
    ]
    if invoice.vat_rate:
        totals_rows.append([f"VAT ({invoice.vat_rate}%):", f"{vat_amount:.2f} {invoice.currency}"])
    totals_rows.append(["TOTAL:", f"{total:.2f} {invoice.currency}"])
    if paid > 0:
        totals_rows.append(["Paid:", f"{paid:.2f} {invoice.currency}"])
        totals_rows.append(["Balance due:", f"{balance:.2f} {invoice.currency}"])

    totals_data = [[
        "",
        Table(
            [[Paragraph(r[0], style_right), Paragraph(r[1], style_right)]
             for r in totals_rows],
            colWidths=["50%", "50%"]
        )
    ]]
    totals_table = Table(totals_data, colWidths=["50%", "50%"])
    elements.append(totals_table)
    elements.append(Spacer(1, 0.8*cm))

    # ── Банківські реквізити ───────────────────────────────────────────────────
    if cfg.iban:
        bank_parts = [f"<b>Bank transfer details:</b>"]
        if cfg.bank_name:
            bank_parts.append(f"Bank: {cfg.bank_name}")
        bank_parts.append(f"IBAN: {cfg.iban}")
        if cfg.swift:
            bank_parts.append(f"SWIFT/BIC: {cfg.swift}")
        elements.append(Paragraph("<br/>".join(bank_parts), style_normal))
        elements.append(Spacer(1, 0.4*cm))

    # ── Примітки ───────────────────────────────────────────────────────────────
    if invoice.notes:
        elements.append(HRFlowable(width="100%", thickness=0.5,
                                   color=colors.HexColor("#cfd8dc")))
        elements.append(Spacer(1, 0.2*cm))
        elements.append(Paragraph(f"<b>Notes:</b> {invoice.notes}", style_normal))

    doc.build(elements)

    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="Invoice-{invoice.number}.pdf"'
    )
    response.write(buffer.getvalue())
    return response
