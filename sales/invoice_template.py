"""Шаблон рахунку (Rechnung, DOCX з тегами docxtpl) на бланку Commercial Invoice.

Як Angebot (offers/template_builder.py): увесь бланк лишається — колонтитули з логотипом і реквізитами, блок
продавця, печатка й підпис; тексти й теги CI замінено на рахунок: «Rechnungsempfänger» (адреса рахунку, USt-IdNr),
номер, дата, Leistungsdatum, строк оплати, Kundennummer, замовлення, вступ, таблиця Pos. | Beschreibung | Menge |
Einzelpreis | Gesamt (+ доставка), MwSt. і Gesamtbetrag, примітка про ПДВ, оплата, Bankverbindung, підписант.
Незнайомий бланк — простий шаблон без бланка. Змінні — sales.invoice_service.build_context.
"""
from copy import deepcopy

from offers.template_builder import (_clone, _has_drawing, _left, _plain_pos, _qn, _set_cell, _set_par,
                                     _set_width, _swap_runs, _swap_table, _wrap)

VERSION = "1"   # змінили побудову → кеш шаблону з бланка перебудовується

BLANK_RUNS = {
    "Ship to (Receiver/Buyer):": "{{ L.bill_to }}:",
    "{{ ship_to.company }}": "{{ bill.company }}",
    "{{ ship_to.street }}": "{{ bill.street }}",
    "{{ ship_to.city_line }}": "{{ bill.zip_city }}",
    "{{ ship_to.country }}": "{{ bill.country }}",
    "Contact": "{{ L.contact }}",
    " Person": "",
    "{{ ship_to.contact }}": "{{ bill.name }}",
    "{{ ship_to.phone }}": "{{ bill.vat_line }}",
    "{{ ship_to.email }}": "{{ bill.email }}",
    "Ship from (Sender/Seller):": "{{ L.seller }}:",
}
BODY_RUNS = {
    "Invoice": "{{ L.title }}",
    "Invoice No.: ": "{{ L.number }}: ", "{{ inv_number }}": "{{ number }}",
    "Invoice Date:": "{{ L.date }}:", "{{ inv_date }}": "{{ date }}",
    "Name: {{ signer_name }}": "{{ L.name }}: {{ signer }}",
    "Position: {{ signer_position }}": "{{ L.position }}: {{ signer_position }}",
}
HEADER_RUNS = {"Invoice No.: {{ inv_number }}": "{{ L.number }}: {{ number }}"}
# рядки під датою (та сама табуляція, що в «Invoice Date»): підпис → значення, лише якщо значення є
META_LINES = [("service_date", "L.service_date"), ("due_date", "L.due_date"), ("customer_number", "L.customer_no"),
              ("order_ref", "L.order"), ("customer_po", "L.po")]


def _items_table(table):
    """Таблиця позицій CI → рахунок: без «HS Code» і «Country of Origin»; рядки й підсумок — рахунку."""
    from docx.table import _Cell
    tbl = table._tbl
    header = [tc for tc in tbl.tr_lst[0].tc_lst]
    texts = ["".join(t.text or "" for t in tc.iter(_qn("w:t"))).lower() for tc in header]
    drop = [i for i, t in enumerate(texts) if "tariff" in t or "hs code" in t or "origin" in t]
    if any(len(tr.tc_lst) != len(header) for tr in tbl.tr_lst):
        drop = []
    cols = tbl.tblGrid.findall(_qn("w:gridCol"))
    widths = [int(c.get(_qn("w:w")) or 0) for c in cols]
    freed = sum(widths[i] for i in drop)
    for tr in tbl.tr_lst:
        tcs = tr.tc_lst
        for i in sorted(drop, reverse=True):
            tr.remove(tcs[i])
    for i in sorted(drop, reverse=True):
        tbl.tblGrid.remove(cols[i])
    widths = [w for i, w in enumerate(widths) if i not in drop]
    if len(widths) == 5:                        # Menge / Einzelpreis / Gesamt — щоб заголовки не ламались
        for i, want in ((2, 900), (3, 1400), (4, 1400)):
            extra = max(0, want - widths[i])
            if extra > freed:
                break
            widths[i], freed = widths[i] + extra, freed - extra
    if len(widths) > 1:
        widths[1] += freed                      # решта — «Beschreibung»
    for c, w in zip(tbl.tblGrid.findall(_qn("w:gridCol")), widths):
        c.set(_qn("w:w"), str(w))
    for tr in tbl.tr_lst:
        for tc, w in zip(tr.tc_lst, widths):
            _set_width(tc, w)
    rows = tbl.tr_lst

    def cells(tr):
        return [_Cell(tc, table) for tc in tr.tc_lst]

    def fill(tr, values):
        for cell, v in zip(cells(tr), values):
            if v is not None:
                _set_cell(cell, v)

    if len(cells(rows[0])) != 5:
        return False
    fill(rows[0], ["{{ H.pos }}", "{{ H.description }}", "{{ H.qty }}", "{{ H.unit }}", "{{ H.total }}"])
    marks = {i: "".join(t.text or "" for t in tr.iter(_qn("w:t"))) for i, tr in enumerate(rows)}
    loop = next((i for i, t in marks.items() if "{%tr for" in t), None)
    if loop is None:
        return False
    fill(rows[loop], ["{%tr for l in rows %}", "", "", "", ""])
    fill(rows[loop + 1], ["{{ l.pos }}", "{{ l.description }}", "{{ l.qty }}", "{{ l.unit_price }}", "{{ l.total }}"])
    _plain_pos(cells(rows[loop + 1])[0])
    ship = next((i for i, t in marks.items() if "has_shipping" in t), None)
    if ship is not None and ship + 1 < len(rows):
        fill(rows[ship + 1], ["{{ shipping_pos }}", "{{ L.shipping_row }}", "1", "{{ shipping_cost }}",
                              "{{ shipping_cost }}"])
        pos_cell = cells(rows[ship + 1])[0]
        _plain_pos(pos_cell)
        size = next((r.font.size for p in cells(rows[loop + 1])[0].paragraphs for r in p.runs if r.font.size), None)
        for p in pos_cell.paragraphs:
            for r in p.runs:
                r.font.size = size
    total = next((i for i, t in marks.items() if "total_amount" in t or "Total invoice" in t), None)
    if total is not None:
        fill(rows[total], ["", "{{ L.total_net }}", "", "", "{{ net_total }}"])
    return True


def _from_blank(blank):
    """Увесь бланк CI як рахунок; None — бланк незнайомий (немає двох таблиць / таблиці позицій)."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt
    from docx.text.paragraph import Paragraph
    doc = Document(str(blank))
    if len(doc.tables) < 2:
        return None
    _swap_table(doc.tables[0], BLANK_RUNS)
    for sec in doc.sections:
        for part in (sec.header, sec.first_page_header, sec.even_page_header):
            _swap_runs(part.paragraphs, HEADER_RUNS)
    for cell in doc.tables[0]._cells:
        _left(cell.paragraphs)
        for inner in cell.tables:
            for c in inner._cells:
                _left(c.paragraphs)
    items = doc.tables[1]
    paras = [Paragraph(el, doc._body) for el in doc.element.body if el.tag == _qn("w:p")]

    def find(prefix):
        return next((p for p in paras if p.text.strip().startswith(prefix)), None)
    number_p, date_p, billing_p = find("Invoice No."), find("Invoice Date"), find("Billing address")
    inco_p, reason_p = find("Terms of delivery"), find("Reason of export")
    declare_p, pay_p = find("I hereby declare"), find("Payment terms")
    if not _items_table(items):
        return None
    _swap_runs(paras, BODY_RUNS)
    if date_p is not None:                      # Leistungsdatum, строк, Kundennummer, замовлення — під датою
        anchor = date_p
        for key, label in META_LINES:
            el = deepcopy(date_p._p)
            anchor._p.addnext(el)
            line = Paragraph(el, doc._body)
            _swap_runs([line], {"{{ L.date }}:": "{{ " + label + " }}:", "{{ date }}": "{{ " + key + " }}"})
            _wrap(line, key)
            anchor = Paragraph(line._p.getnext(), doc._body)      # «{%p endif %}»
        for p in [number_p, date_p] + [Paragraph(e, doc._body) for e in doc.element.body if e.tag == _qn("w:p")]:
            if p is not None and any(t in p.text for t in ("{{ L.service_date }}", "{{ L.due_date }}",
                                                            "{{ L.customer_no }}", "{{ L.order }}", "{{ L.po }}",
                                                            "{{ L.number }}", "{{ L.date }}")):
                for pos in (4.2, 7.2):
                    p.paragraph_format.tab_stops.add_tab_stop(Cm(pos))
    if billing_p is not None:                   # «Billing address» → адреса доставки, якщо інша
        _set_par(billing_p, "{{ L.ship_to }}: {{ ship_line }}")
        _wrap(billing_p, "ship_line")
    gap = Paragraph(deepcopy(paras[0]._p), doc._body)   # вступ — над таблицею
    items._tbl.addprevious(gap._p)
    _set_par(gap, "{{ intro }}")
    for r in gap.runs:
        r.bold, r.font.size = False, None
    gap.alignment = None
    gap.paragraph_format.space_after = Pt(6)
    _wrap(gap, "intro")
    after = items._tbl.getnext()
    if after is not None and after.tag == _qn("w:p"):  # MwSt. і Gesamtbetrag — праворуч під таблицею
        anchor = Paragraph(after, doc._body)
        vat = _clone(anchor, "{{ L.vat }} {{ vat_rate }} %: {{ vat_total }} {{ currency }}", before=True)
        gross = _clone(anchor, "{{ L.gross }}: {{ gross_total }} {{ currency }}", before=True)
        vat.alignment = gross.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        for r in gross.runs:
            r.bold = True
    if inco_p is not None:                      # Incoterms → примітка про ПДВ
        _set_par(inco_p, "{{ vat_note }}")
        _wrap(inco_p, "vat_note")
    if reason_p is not None:
        reason_p._p.getparent().remove(reason_p._p)
    if declare_p is not None:                   # «I hereby declare…» → як оплатити
        _set_par(declare_p, "{{ payment_text }}")
        _wrap(declare_p, "payment_text")
    if pay_p is not None:                       # «Payment terms» → завершення, далі Bankverbindung
        _set_par(pay_p, "{{ outro }}")
        _wrap(pay_p, "outro")
        info = _clone(Paragraph(pay_p._p.getnext(), doc._body), "{%p if payment_info %}")
        head = _clone(_clone(info, ""), "{{ L.payment_info }}:")
        bank = _clone(head, "{{ payment_info }}")
        for r in bank.runs:
            r.bold = True
        _left([pay_p, head, bank])
        _clone(_clone(bank, "{%p endif %}"), "")
        start = inco_p if inco_p is not None else declare_p
        if start is not None:                   # порожні абзаци бланка між текстами — геть (без пропусків)
            el = start._p.getnext()
            while el is not None and el is not pay_p._p:
                nxt = el.getnext()
                if el.tag == _qn("w:p"):
                    p = Paragraph(el, doc._body)
                    if not p.text.strip() and not _has_drawing(p):
                        el.getparent().remove(el)
                el = nxt
    pos = find("Position:") or next((p for p in paras if "{{ signer_position }}" in p.text), None)
    if pos is not None:
        _wrap(pos, "signer_position")
    return doc


def _simple(path):
    """Без бланка: простий рахунок (реквізити — з налаштувань компанії в company_block)."""
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt, RGBColor

    doc = Document()
    sec = doc.sections[0]
    sec.page_height, sec.page_width = Cm(29.7), Cm(21.0)
    sec.left_margin = sec.right_margin = Cm(2.0)
    sec.top_margin, sec.bottom_margin = Cm(1.6), Cm(1.8)
    doc.styles["Normal"].font.name, doc.styles["Normal"].font.size = "Arial", Pt(9.5)

    def para(text="", *, size=None, bold=False, align=None, color=None, space_after=4):
        p = doc.add_paragraph()
        run = p.add_run(text)
        run.bold = bold
        if size:
            run.font.size = Pt(size)
        if color:
            run.font.color.rgb = RGBColor.from_string(color)
        if align:
            p.alignment = align
        p.paragraph_format.space_after = Pt(space_after)
        return p

    para("{{ company_block }}", size=8, color="555555", space_after=10)
    para("{{ L.bill_to }}", bold=True, space_after=1)
    para("{{ bill_block }}", space_after=10)
    para("{{ L.title }} {{ number }}", size=16, bold=True, space_after=2)
    para("{{ L.date }}: {{ date }}", color="333333", space_after=1)
    for key, label in META_LINES:
        para("{%p if " + key + " %}")
        para("{{ " + label + " }}: {{ " + key + " }}", color="333333", space_after=1)
        para("{%p endif %}")
    para("{%p if ship_line %}")
    para("{{ L.ship_to }}: {{ ship_line }}", color="333333")
    para("{%p endif %}")
    para("{%p if intro %}")
    para("{{ intro }}", space_after=8)
    para("{%p endif %}")
    table = doc.add_table(rows=4, cols=5)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, h in enumerate(["{{ H.pos }}", "{{ H.description }}", "{{ H.qty }}", "{{ H.unit }}", "{{ H.total }}"]):
        table.rows[0].cells[i].text = h
    table.rows[1].cells[0].text = "{%tr for l in all_rows %}"
    for i, v in enumerate(["{{ l.pos }}", "{{ l.description }}", "{{ l.qty }}", "{{ l.unit_price }}", "{{ l.total }}"]):
        table.rows[2].cells[i].text = v
        if i >= 2:
            table.rows[2].cells[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
    table.rows[3].cells[0].text = "{%tr endfor %}"
    table.autofit = False
    for row in table.rows:                      # Pos. | Beschreibung | Menge | Einzelpreis | Gesamt
        for cell, w in zip(row.cells, (1.1, 8.4, 2.0, 2.75, 2.75)):
            cell.width = Cm(w)
    right = WD_ALIGN_PARAGRAPH.RIGHT
    para("", space_after=4)
    para("{{ L.total_net }}: {{ net_total }} {{ currency }}", align=right, space_after=1)
    para("{{ L.vat }} {{ vat_rate }} %: {{ vat_total }} {{ currency }}", align=right, space_after=1)
    para("{{ L.gross }}: {{ gross_total }} {{ currency }}", align=right, bold=True, size=11, space_after=10)
    for key in ("vat_note", "payment_text", "outro"):
        para("{%p if " + key + " %}")
        para("{{ " + key + " }}", space_after=6)
        para("{%p endif %}")
    para("{%p if payment_info %}")
    para("{{ L.payment_info }}:", bold=True, space_after=1)
    para("{{ payment_info }}", space_after=8)
    para("{%p endif %}")
    para("{%p if signer %}")
    para("{{ signer }}")
    para("{%p endif %}")
    doc.save(str(path))


def build(path, blank=None):
    if blank:
        doc = _from_blank(blank)
        if doc is not None:
            doc.save(str(path))
            return
    _simple(path)
