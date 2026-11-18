"""Шаблон рахунку (Rechnung, DOCX з тегами docxtpl) — німецький рахунок-лист (як DIN 5008).

З бланка Commercial Invoice береться лише оформлення сторінки: формат і поля, логотип у верхньому колонтитулі
(«Rechnungsnummer: … · Page» на наступних сторінках) і лінія нижнього колонтитула; текст нижнього колонтитула —
три колонки з даних фірми ({{ F.c1 }} {{ F.c2 }} {{ F.c3 }}). Тіло будується тут:

- ліворуч — рядок відправника дрібно й підкреслено, адреса покупця (компанія жирним, «z. Hd. Herrn …»); праворуч —
  фірма, «Ansprechpartner», телефони, e-mail і сайт, St.-Nr. і USt-IdNr. (дані — «Бухгалтерія → Налаштування компанії»);
- «Rechnung», номер, дата, Kundennummer, Bestellnummer, Leistungsdatum, Zahlbar bis (лише заповнені);
- звертання, вступ ({order} жирним), таблиця Pos. | Beschreibung | Menge | Einzelpreis | Betrag з доставкою,
  Gesamtsumme netto, Umsatzsteuer і Gesamtsumme brutto в самій таблиці;
- Lieferadresse, примітка про ПДВ, оплата, банківські реквізити, «Mit freundlichen Grüßen», підписант, примітка
  дрібно («ohne Unterschrift gültig»). Печатки й підпису немає.

Змінні — sales.invoice_service.build_context. Без бланка — те саме на чистій сторінці A4.
"""
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_ROW_HEIGHT_RULE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt

from offers.template_builder import _qn, _swap_runs

VERSION = "2"   # змінили побудову → кеш шаблону з бланка перебудовується

HEADER_RUNS = {"Invoice No.: {{ inv_number }}": "{{ L.number }}: {{ number }}", "Page": "{{ L.page }}"}
TEXT_W = 16.0                      # ширина тексту, см (A4 з полями 2,5 см)
ADDR_W = (8.5, 2.3, 5.2)           # поле адреси DIN 5008 (8,5 см) | проміжок | дані фірми
ITEM_W = (0.75, 9.25, 1.6, 2.5, 1.9)
FOOT_W = (7.4, 4.0, 4.6)
BODY_PT, TABLE_PT, SMALL_PT, FOOT_PT = 11, 10, 9.5, 7.5
META_TAB = 4.5
SELLER_TABS = {"phones": 1.05, "web": 1.65, "tax": 1.95}   # Tel.: · E-Mail: / Internet: · St.-Nr.: / USt-IdNr.:
BANK_TAB, BANK_TAB_WIDE = 3.0, 3.4                          # «Kontoinhaber:» · «Account holder:»


def _para(container, text="", *, bold=False, size=None, align=None, before=0, after=0, tabs=(), underline=False,
          first=False, keep=False):
    """Абзац (у документі / комірці; first — перший абзац комірки) з одним run-ом."""
    p = container.paragraphs[0] if first else container.add_paragraph()
    pf = p.paragraph_format
    pf.space_before, pf.space_after, pf.line_spacing = Pt(before), Pt(after), 1.0
    if keep:   # тримається з наступним абзацом (прощання, підпис і примітка — разом на одній сторінці)
        pf.keep_with_next = True
    for pos in tabs:
        pf.tab_stops.add_tab_stop(Cm(pos))
    if align is not None:
        p.alignment = align
    if text:
        run = p.add_run(text)
        run.bold = bold or None
        run.underline = underline or None
        if size:
            run.font.size = Pt(size)
    return p


def _tag(container, tag, first=False):
    """Службовий абзац docxtpl ({%p … %}) — зникає під час формування."""
    return _para(container, tag, first=first)


def _cond(container, cond, *lines, **kw):
    """{%p if cond %} рядки {%p endif %}."""
    _tag(container, "{%p if " + cond + " %}")
    for text in lines:
        _para(container, text, **kw)
    _tag(container, "{%p endif %}")


def _no_margins(table):
    """Без внутрішніх відступів комірок — адреса й дані фірми рівно по полях сторінки."""
    tbl_pr = table._tbl.tblPr
    mar = tbl_pr.makeelement(_qn("w:tblCellMar"), {})
    for side in ("top", "left", "bottom", "right"):
        mar.append(mar.makeelement(_qn(f"w:{side}"), {_qn("w:w"): "0", _qn("w:type"): "dxa"}))
    tbl_pr.append(mar)


def _widths(table, widths):
    table.autofit = False
    tbl_pr = table._tbl.tblPr
    layout = tbl_pr.makeelement(_qn("w:tblLayout"), {_qn("w:type"): "fixed"})
    tbl_pr.append(layout)
    for i, w in enumerate(widths):
        table.columns[i].width = Cm(w)
        for cell in table.columns[i].cells:
            cell.width = Cm(w)


def _vertical(cell):
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_pr.append(tc_pr.makeelement(_qn("w:textDirection"), {_qn("w:val"): "btLr"}))


def _address(doc):
    table = doc.add_table(rows=1, cols=3)
    _no_margins(table)
    _widths(table, ADDR_W)
    left, right = table.cell(0, 0), table.cell(0, 2)
    _para(left, "{{ seller.return_line }}", size=7, underline=True, before=52, after=1, first=True)
    _para(left, "{{ bill.line1 }}", bold=True)
    for key in ("attn", "street", "zip_city", "country", "vat_line"):
        _cond(left, f"bill.{key}", "{{ bill." + key + " }}")
    _para(right, "{{ seller.name }}", bold=True, first=True)
    for key in ("street", "zip_city", "country"):
        _cond(right, f"seller.{key}", "{{ seller." + key + " }}")
    _cond(right, "signer", "")
    _tag(right, "{%p if signer %}")
    _para(right, "{{ L.contact }}:", bold=True)
    _para(right, "{{ signer }}")
    _tag(right, "{%p endif %}")
    for group in ("phones", "web", "tax"):        # Tel / Fax / Mob · E-Mail / Internet · St.-Nr. / USt-IdNr.
        _tag(right, "{%p if seller." + group + " %}")
        _para(right, "")
        _tag(right, "{%p for x in seller." + group + " %}")
        _para(right, "{{ x.label }}\t{{ x.value }}", tabs=(SELLER_TABS[group],))
        _tag(right, "{%p endfor %}")
        _tag(right, "{%p endif %}")


def _meta(doc):
    _para(doc, "{{ L.title }}", bold=True, size=20, align=WD_ALIGN_PARAGRAPH.CENTER, before=12, after=8)
    _para(doc, "{{ L.number }}:\t{{ number }}", bold=True, tabs=(META_TAB,))
    _para(doc, "{{ L.date }}:\t{{ date }}", bold=True, tabs=(META_TAB,))
    for key, label in (("customer_number", "L.customer_no"), ("order_ref", "L.order"), ("customer_po", "L.po"),
                       ("service_date", "L.service_date"), ("due_date", "L.due_date")):
        _cond(doc, key, "{{ " + label + " }}:\t{{ " + key + " }}", bold=True, tabs=(META_TAB,))
    _para(doc, "{{ salutation }}", before=12, after=8)
    _tag(doc, "{%p if has_intro %}")
    _para(doc, "{{r intro }}", after=6)
    _tag(doc, "{%p endif %}")


def _set(cell, text, *, bold=False, align=None, size=TABLE_PT):
    p = cell.paragraphs[0]
    p.paragraph_format.space_before = p.paragraph_format.space_after = Pt(1)
    if align is not None:
        p.alignment = align
    run = p.add_run(text)
    run.bold = bold or None
    run.font.size = Pt(size)
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER


def _items(doc):
    center, right = WD_ALIGN_PARAGRAPH.CENTER, WD_ALIGN_PARAGRAPH.RIGHT
    table = doc.add_table(rows=10, cols=5)
    try:
        table.style = "Table Grid"
    except KeyError:   # бланк без стилю «Table Grid» — рамки з таблиці не будуть, але документ формується
        pass
    _widths(table, ITEM_W)
    rows = table.rows
    head = rows[0]
    head.height, head.height_rule = Cm(1.3), WD_ROW_HEIGHT_RULE.AT_LEAST
    _set(head.cells[0], "{{ H.pos }}", bold=True, align=center)
    _vertical(head.cells[0])
    for i, key in enumerate(("description", "qty", "unit", "total"), 1):
        _set(head.cells[i], "{{ H." + key + " }}", bold=True, align=center)
    _set(rows[1].cells[0], "{%tr for l in rows %}")
    item = rows[2].cells
    _set(item[0], "{{ l.pos }}", align=center)
    _set(item[1], "{{ l.description }}")
    _set(item[2], "{{ l.qty }}", align=center)
    _set(item[3], "{{ l.unit_price }}", align=right)
    _set(item[4], "{{ l.total }}", align=right)
    _set(rows[3].cells[0], "{%tr endfor %}")
    _set(rows[4].cells[0], "{%tr if has_shipping %}")
    ship = rows[5].cells
    _set(ship[0], "{{ shipping_pos }}", align=center)
    _set(ship[1], "{{ L.shipping_row }}")
    _set(ship[2], "1", align=center)
    _set(ship[3], "{{ shipping_cost }}", align=right)
    _set(ship[4], "{{ shipping_cost }}", align=right)
    _set(rows[6].cells[0], "{%tr endif %}")
    for row, label, value, bold in ((rows[7], "{{ L.total_net }}", "{{ net_total }}", True),
                                    (rows[8], "{{ L.vat }} {{ vat_rate }} %", "{{ vat_total }}", False),
                                    (rows[9], "{{ L.gross }}", "{{ gross_total }}", True)):
        _set(row.cells[1], label, bold=bold)
        _set(row.cells[4], value, bold=bold, align=right)


def _closing(doc):
    _para(doc, "{{r ship_text }}", before=8)
    _cond(doc, "vat_note", "{{ vat_note }}", size=SMALL_PT, before=6)
    _cond(doc, "payment_text", "{{ payment_text }}", before=8)
    _tag(doc, "{%p if bank %}")
    _para(doc, "")
    _tag(doc, "{%p for b in bank %}")
    _tag(doc, "{%p if wide_bank %}")             # англійські підписи довші («Account holder:»)
    _para(doc, "{{ b.label }}\t{{ b.value }}", bold=True, tabs=(BANK_TAB_WIDE,))
    _tag(doc, "{%p else %}")
    _para(doc, "{{ b.label }}\t{{ b.value }}", bold=True, tabs=(BANK_TAB,))
    _tag(doc, "{%p endif %}")
    _tag(doc, "{%p endfor %}")
    _tag(doc, "{%p endif %}")
    _para(doc, "{{ L.regards }}", before=12, keep=True)
    _cond(doc, "signer", "{{ signer }}", keep=True)
    _cond(doc, "outro", "{{ outro }}", size=SMALL_PT, before=10)


def _footer_table(part):
    """Три колонки з даними фірми в нижньому колонтитулі ({{ F.c1 }} {{ F.c2 }} {{ F.c3 }})."""
    table = part.add_table(rows=1, cols=3, width=Cm(TEXT_W))
    _no_margins(table)
    _widths(table, FOOT_W)
    for i, cell in enumerate(table.rows[0].cells):
        p = cell.paragraphs[0]
        p.paragraph_format.space_before = p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.line_spacing = 1.0
        run = p.add_run("{{ F.c" + str(i + 1) + " }}")
        run.font.size = Pt(FOOT_PT)
    end = part.add_paragraph()                  # колонтитул закінчується абзацом (так хоче Word)
    end.paragraph_format.space_after = Pt(0)
    end.add_run("").font.size = Pt(2)


def _footers(doc):
    """Текст нижнього колонтитула бланка (англійською, вписаний руками) → колонки з даних фірми; лінія (малюнок у
    першому абзаці) лишається."""
    for sec in doc.sections:
        for part in (sec.first_page_footer, sec.footer):
            if part.is_linked_to_previous:
                continue
            for p in list(part.paragraphs):
                has_drawing = any(True for _ in p._p.iter(_qn("w:drawing"))) or any(True for _ in p._p.iter(_qn("w:pict")))
                if p.text.strip() and not has_drawing:
                    p._p.getparent().remove(p._p)
                elif p.text.strip():   # текст разом із лінією — лише текст геть
                    for r in p.runs:
                        r.text = ""
            for t in list(part.tables):
                t._tbl.getparent().remove(t._tbl)
            _footer_table(part)


def _clear_body(doc):
    body = doc.element.body
    for el in list(body):
        if not el.tag.endswith("}sectPr"):
            body.remove(el)


def _body(doc):
    style = doc.styles["Normal"]
    style.font.size = Pt(BODY_PT)
    _address(doc)
    _meta(doc)
    _items(doc)
    _closing(doc)


def build(path, blank=None):
    from docx import Document
    if blank:
        doc = Document(str(blank))
        _clear_body(doc)
        for sec in doc.sections:
            for part in (sec.header, sec.first_page_header, sec.even_page_header):
                _swap_runs(part.paragraphs, HEADER_RUNS)
        _footers(doc)
    else:
        doc = Document()
        sec = doc.sections[0]
        sec.page_height, sec.page_width = Cm(29.7), Cm(21.0)
        sec.left_margin = sec.right_margin = Cm(2.5)
        sec.top_margin, sec.bottom_margin = Cm(1.5), Cm(2.0)
        doc.styles["Normal"].font.name = "Arial"
        _footer_table(sec.footer)
    _body(doc)
    doc.save(str(path))
