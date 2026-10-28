"""Шаблон Angebot (DOCX з тегами docxtpl).

На бланку Commercial Invoice (`shipping.services.commercial_invoice_service.active_template_path` — стандартний
або завантажений власний): колонтитули з логотипом і реквізитами, таблиця адрес (ліворуч — клієнт замість
«Ship to», праворуч — продавець) лишаються, решта сторінки — текст і таблиця Angebot. Без бланка — простий шаблон
(`python manage.py build_offer_template`). Змінні — `offers.document.build_context`.
"""
VERSION = "1"   # змінили побудову → кеш шаблону з бланка перебудовується

# текст у run-ах бланка CI → тег Angebot (форматування run-а лишається)
BLANK_RUNS = {
    "Ship to (Receiver/Buyer):": "{{ L.customer }}:",
    "{{ ship_to.company }}": "{{ customer_name }}",
    "{{ ship_to.street }}": "{{ billing_address }}",
    "{{ ship_to.city_line }}": "",
    "{{ ship_to.country }}": "",
    "Contact": "{{ L.contact }}",
    " Person": "",
    "{{ ship_to.contact }}": "{{ contact_name }}",
    "{{ ship_to.phone }}": "",
    "{{ ship_to.email }}": "{{ contact_email }}",
    "Ship from (Sender/Seller):": "{{ L.seller }}:",
}
HEADER_RUNS = {"Invoice No.: {{ inv_number }}": "{{ L.number }}: {{ number }}"}


def _swap_runs(paragraphs, mapping):
    """Замінити текст run-ів за словником; абзаци, що стали порожніми через заміну, — прибрати."""
    for p in list(paragraphs):
        touched = False
        for r in p.runs:
            if r.text in mapping:
                r.text, touched = mapping[r.text], True
        if touched and not p.text.strip():
            p._p.getparent().remove(p._p)


def _swap_table(table, mapping):
    """Те саме у всіх комірках таблиці, включно з вкладеними (блок продавця на бланку — вкладена таблиця)."""
    for cell in table._cells:
        _swap_runs(cell.paragraphs, mapping)
        for inner in cell.tables:
            _swap_table(inner, mapping)


def _from_blank(blank):
    """Бланк CI: колонтитули й таблиця адрес лишаються, решта тіла документа прибирається."""
    from docx import Document
    doc = Document(str(blank))
    body = doc.element.body
    first = doc.tables[0]._tbl if doc.tables else None
    keep = True
    for el in list(body):
        if el.tag.endswith("}sectPr"):
            continue
        if first is not None and el is first:
            keep = False
            continue
        if not keep or first is None:
            body.remove(el)
    if first is not None:
        _swap_table(doc.tables[0], BLANK_RUNS)
    for sec in doc.sections:
        for part in (sec.header, sec.first_page_header, sec.even_page_header):
            _swap_runs(part.paragraphs, HEADER_RUNS)
    return doc


def build(path, blank=None):
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt, RGBColor

    if blank:
        doc = _from_blank(blank)
    else:
        doc = Document()
        sec = doc.sections[0]
        sec.page_height, sec.page_width = Cm(29.7), Cm(21.0)
        sec.left_margin = sec.right_margin = Cm(2.0)
        sec.top_margin, sec.bottom_margin = Cm(1.6), Cm(1.8)
        style = doc.styles["Normal"]
        style.font.name, style.font.size = "Arial", Pt(9.5)

    def para(text="", *, size=None, bold=False, align=None, color=None, space_after=4):
        p = doc.add_paragraph()
        run = p.add_run(text)        # один run — теги docxtpl не розриваються
        run.bold = bold
        if size:
            run.font.size = Pt(size)
        if color:
            run.font.color.rgb = RGBColor.from_string(color)
        if align:
            p.alignment = align
        p.paragraph_format.space_after = Pt(space_after)
        return p

    if blank:
        para("")
        para("{{ L.title }}", size=16, bold=True, space_after=2)
        para("{{ L.number }}: {{ number }}    {{ L.date }}: {{ date }}    {{ L.valid_until }}: {{ valid_until }}",
             color="333333")
        para("{%p if project %}")
        para("{{ L.project }}: {{ project }}", color="333333")
        para("{%p endif %}")
        para("")
    else:
        para("{{ company_block }}", size=8, color="555555", space_after=10)
        para("{{ L.title }} {{ number }}", size=16, bold=True, space_after=2)
        para("{{ L.date }}: {{ date }}    {{ L.valid_until }}: {{ valid_until }}", color="333333")
        para("{%p if project %}")
        para("{{ L.project }}: {{ project }}", color="333333")
        para("{%p endif %}")
        para("")
        para("{{ L.customer }}", bold=True, space_after=1)
        para("{{ customer_name }}", space_after=1)
        para("{{ billing_address }}", space_after=1)
        para("{%p if contact_name %}")
        para("{{ L.contact }}: {{ contact_name }}")
        para("{%p endif %}")
        para("")
    para("{{ L.greeting }}", space_after=2)
    para("{{ intro_text }}", space_after=8)

    headers = ["{{ L.pos }}", "{{ L.sku }}", "{{ L.description }}", "{{ L.qty }}", "{{ L.unit_price }}",
               "{{ L.discount }}", "{{ L.total }}"]
    widths = [Cm(1.0), Cm(3.2), Cm(5.8), Cm(1.8), Cm(2.3), Cm(1.5), Cm(2.4)]
    table = doc.add_table(rows=4, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    cells_row = ["{{ l.pos }}", "{{ l.sku }}",
                 "{{ l.description }}{% if l.lead_time %} · {{ L.lead_time }}: {{ l.lead_time }}{% endif %}",
                 "{{ l.qty }}", "{{ l.unit_price }}", "{{ l.discount }}", "{{ l.total }}"]
    for i, h in enumerate(headers):
        c = table.rows[0].cells[i]
        c.text = h
        c.paragraphs[0].runs[0].bold = True
        c.width = widths[i]
    table.rows[1].cells[0].text = "{%tr for l in lines %}"
    for i, v in enumerate(cells_row):
        c = table.rows[2].cells[i]
        c.text = v
        c.width = widths[i]
        if i >= 3:
            c.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
    table.rows[3].cells[0].text = "{%tr endfor %}"

    para("", space_after=6)
    right = WD_ALIGN_PARAGRAPH.RIGHT
    para("{%p if has_shipping %}")
    para("{{ L.shipping }}: {{ shipping_cost }} {{ currency }}", align=right, space_after=1)
    para("{%p endif %}")
    para("{{ L.net }}: {{ net_total }} {{ currency }}", align=right, space_after=1)
    para("{{ L.vat }} {{ vat_rate }} %: {{ vat_total }} {{ currency }}", align=right, space_after=1)
    para("{{ L.gross }}: {{ gross_total }} {{ currency }}", align=right, bold=True, size=11, space_after=10)
    para("{%p if payment_terms %}")
    para("{{ L.payment }}: {{ payment_terms }}", space_after=2)
    para("{%p endif %}")
    para("{%p if delivery_terms %}")
    para("{{ L.delivery }}: {{ delivery_terms }}", space_after=8)
    para("{%p endif %}")
    para("{{ outro_text }}", space_after=8)
    para("{{ L.closing }}", space_after=2)
    para("{%p if signer %}")
    para("{{ signer }}")
    para("{%p endif %}")

    if not blank:
        footer = doc.sections[0].footer.paragraphs[0]
        run = footer.add_run("{{ footer_text }}")
        run.font.size = Pt(7.5)
        run.font.color.rgb = RGBColor.from_string("666666")
    doc.save(str(path))
