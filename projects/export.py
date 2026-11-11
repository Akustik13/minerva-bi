"""📤 Діаграми проєкту для клієнта: PDF (діаграма як на екрані, для плану — ще таблиця кроків), PNG, Excel (план по
тижнях) і лист з цими файлами (чернетка у власній скриньці → /email/compose/?draft=, нічого не надсилається само).

Картинку діаграми робить браузер (projects/static/projects/pj_export.js) — тут лише перевірка PNG, PDF (reportlab,
шрифт з кирилицею — як sales/doc_generators), Excel (openpyxl), назви файлів і текст листа мовою документа.
Маршрут — projects/views.py export_view (право перегляду проєкту; лист — лише з власної скриньки людини).
"""
import io
from datetime import date, timedelta
from html import escape as _esc

from django.utils import timezone

from .models import ProjectStep

WHATS = {
    "plan": {"uk": "План проєкту", "de": "Projektplan", "en": "Project plan", "file": "Projektplan"},
    "timeline": {"uk": "Шкала проєкту", "de": "Projektverlauf", "en": "Project timeline", "file": "Projektverlauf"},
    "chain": {"uk": "Сторони проєкту й ланцюжок постачання", "de": "Projektbeteiligte und Lieferkette",
              "en": "Project parties and supply chain", "file": "Projektbeteiligte"},
}
LANGS = ("de", "en", "uk")
L = {
    "de": {"asof": "Stand", "customer": "Kunde", "phase": "Phase", "step": "Schritt", "who": "Wer", "start": "Beginn",
           "end": "Ende", "days": "Tage", "status": "Status", "nophase": "Ohne Phase", "page": "Seite",
           "steps_title": "Schritte und Termine", "milestone": "Meilenstein", "week": "KW",
           "status_map": {"todo": "geplant", "doing": "in Arbeit", "waiting": "wartet", "done": "erledigt",
                          "skipped": "entfällt"},
           "side_map": {"we": "wir", "customer": "Kunde", "supplier": "Lieferant", "colleague": "Kollege"},
           "subject": "{what} {code} · {name} (Stand {date})",
           "body": "Guten Tag,\n\nanbei erhalten Sie: {what} zum Projekt {code} · {name} (Stand {date}).\n\n"},
    "en": {"asof": "as of", "customer": "Customer", "phase": "Phase", "step": "Step", "who": "Who", "start": "Start",
           "end": "End", "days": "Days", "status": "Status", "nophase": "No phase", "page": "Page",
           "steps_title": "Steps and dates", "milestone": "Milestone", "week": "CW",
           "status_map": {"todo": "planned", "doing": "in progress", "waiting": "waiting", "done": "done",
                          "skipped": "skipped"},
           "side_map": {"we": "we", "customer": "customer", "supplier": "supplier", "colleague": "colleague"},
           "subject": "{what} {code} · {name} (as of {date})",
           "body": "Hello,\n\nplease find attached the {what_lc} for project {code} · {name} (as of {date}).\n\n"},
    "uk": {"asof": "станом на", "customer": "Клієнт", "phase": "Фаза", "step": "Крок", "who": "Хто",
           "start": "Початок", "end": "Кінець", "days": "Днів", "status": "Стан", "nophase": "Без фази",
           "page": "Сторінка", "steps_title": "Кроки й дати", "milestone": "Віха", "week": "тиж.",
           "status_map": {"todo": "заплановано", "doing": "в роботі", "waiting": "чекаємо", "done": "виконано",
                          "skipped": "пропущено"},
           "side_map": {"we": "ми", "customer": "клієнт", "supplier": "постачальник", "colleague": "колега"},
           "subject": "{what} {code} · {name} (станом на {date})",
           "body": "Добрий день!\n\nНадсилаю {what_lc} {code} · {name} (станом на {date}).\n\n"},
}
IMAGE_MAX = 15 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_WEEKS = 104
STATUS_FILL = {"done": "9BD39B", "doing": "79AEC8", "waiting": "F2C14E", "todo": "BFD7EA", "skipped": "DDDDDD"}


class ExportError(Exception):
    pass


def check_png(data: bytes) -> bytes:
    if not data:
        raise ExportError("Немає картинки діаграми")
    if len(data) > IMAGE_MAX:
        raise ExportError("Картинка завелика — зменште масштаб діаграми")
    if not data.startswith(PNG_MAGIC):
        raise ExportError("Картинка діаграми пошкоджена")
    return data


def _company() -> str:
    from .parties import company_name
    return company_name()


def _date_text(d, lang) -> str:
    return d.isoformat() if lang == "en" else d.strftime("%d.%m.%Y")


def zip_name(project, what, today=None) -> str:
    return file_name(project, what, "zip", today)


def zip_files(files) -> bytes:
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data, _ctype in files:
            zf.writestr(name, data)
    return buf.getvalue()


def file_name(project, what, ext, today=None) -> str:
    today = today or timezone.localdate()
    return f"{project.code}_{WHATS[what]['file']}_{today.isoformat()}.{ext}"


def rows(project, lang) -> list:
    """Кроки плану з прогнозом (кеш schedule.persist — оновлює overview.collect) для таблиці PDF і Excel."""
    from . import overview
    overview.collect(project)                                   # свіжий прогноз кроків
    t = L[lang]
    out = []
    phases = {p.pk: p for p in project.phases.all()}
    steps = list(ProjectStep.objects.filter(project=project).select_related("phase", "party", "assignee")
                 .order_by("phase__order", "phase_id", "order", "pk"))
    company = _company()
    for s in steps:
        start, end = s.f_start or s.start, s.f_end or s.end
        if s.kind == ProjectStep.MILESTONE:
            start = end = end or start
        if s.party_id:
            who = s.party.name
        elif s.side == ProjectStep.WE:
            who = company
        else:
            who = t["side_map"].get(s.side, s.side)
        status = s.eff_status or s.status
        out.append({"phase": phases[s.phase_id].name if s.phase_id in phases else t["nophase"],
                    "phase_id": s.phase_id or 0, "title": s.title, "milestone": s.kind == ProjectStep.MILESTONE,
                    "who": who, "start": start, "end": end,
                    "days": (end - start).days + 1 if start and end and s.kind != ProjectStep.MILESTONE else 0,
                    "status": status, "status_text": t["status_map"].get(status, status)})
    return out


# ── PDF ───────────────────────────────────────────────────────────────────────

def _fonts():
    from sales.doc_generators import _register_unicode_fonts
    return _register_unicode_fonts()


def _keep(font_name):
    """Лише символи, які є в шрифті (емодзі — ні: у PDF були б квадратики)."""
    from reportlab.pdfbase import pdfmetrics
    try:
        face = pdfmetrics.getFont(font_name).face
        glyphs = getattr(face, "charToGlyph", None)
    except Exception:  # noqa: BLE001
        glyphs = None

    def clean(text):
        text = str(text or "")
        if glyphs is None:
            return "".join(ch for ch in text if ord(ch) < 256)
        return " ".join("".join(ch for ch in text if ch.isspace() or ord(ch) in glyphs).split())
    return clean


def pdf(project, what, lang, png, table=False, today=None) -> bytes:
    from PIL import Image as PILImage
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A3, A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    today = today or timezone.localdate()
    t = L[lang]
    font, bold = _fonts()
    clean = _keep(font)
    diamond = next((c for c in ("◆", "♦", "»") if clean(c)), "")       # віха: у шрифті Arial «◆» немає
    w = h = 0
    if png:
        with PILImage.open(io.BytesIO(png)) as im:
            w, h = im.size
    page = landscape(A3) if w > 3000 and w / max(h, 1) > 2.2 else landscape(A4)
    margin = 12 * mm
    buf = io.BytesIO()
    company = clean(_company())
    title = clean(f"{project.code} · {project.name}")
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=margin, rightMargin=margin, topMargin=margin,
                            bottomMargin=margin + 4 * mm, title=title, author=company)
    h1 = ParagraphStyle("h1", fontName=bold, fontSize=15, leading=19)
    sub = ParagraphStyle("sub", fontName=font, fontSize=9.5, leading=12, textColor=colors.HexColor("#57606a"))
    cell = ParagraphStyle("cell", fontName=font, fontSize=8.5, leading=10.5)
    cellb = ParagraphStyle("cellb", fontName=bold, fontSize=8.5, leading=10.5)
    line = [WHATS[what][lang], f"{t['asof']} {_date_text(today, lang)}"]
    if project.customer_id:
        from .portfolio import customer_name
        line.append(f"{t['customer']}: {customer_name(project.customer)}")
    story = [Paragraph(_esc(title), h1), Paragraph(_esc(clean(" · ".join(line))), sub), Spacer(1, 4 * mm)]
    if png:
        avail_w = page[0] - 2 * margin
        avail_h = page[1] - 2 * margin - 22 * mm
        ratio = min(avail_w / w, avail_h / h)
        story.append(Image(io.BytesIO(png), width=w * ratio, height=h * ratio))
    if table and what == "plan":
        data = rows(project, lang)
        if data:
            if png:
                story.append(PageBreak())
            story.append(Paragraph(_esc(clean(t["steps_title"])), h1))
            story.append(Spacer(1, 3 * mm))
            head = [t["step"], t["who"], t["start"], t["end"], t["days"], t["status"]]
            body = [[Paragraph(_esc(clean(x)), cellb) for x in head]]
            styles = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d0d7de")),
                      ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f6")),
                      ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
            last_phase = None
            for r in data:
                if r["phase_id"] != last_phase:
                    last_phase = r["phase_id"]
                    body.append([Paragraph(_esc(clean(r["phase"])), cellb), "", "", "", "", ""])
                    styles += [("SPAN", (0, len(body) - 1), (-1, len(body) - 1)),
                               ("BACKGROUND", (0, len(body) - 1), (-1, len(body) - 1), colors.HexColor("#f6f8fa"))]
                name = (f"{diamond} " if r["milestone"] and diamond else "") + r["title"]
                body.append([Paragraph(_esc(clean(name)), cell), Paragraph(_esc(clean(r["who"])), cell),
                             _date_text(r["start"], lang) if r["start"] and not r["milestone"] else "",
                             _date_text(r["end"], lang) if r["end"] else "",
                             str(r["days"] or ""), Paragraph(_esc(clean(r["status_text"])), cell)])
                fill = STATUS_FILL.get(r["status"])
                if fill:
                    styles.append(("BACKGROUND", (5, len(body) - 1), (5, len(body) - 1), colors.HexColor("#" + fill)))
            avail = page[0] - 2 * margin
            tbl = Table(body, colWidths=[avail * 0.42, avail * 0.2, avail * 0.1, avail * 0.1, avail * 0.06,
                                         avail * 0.12], repeatRows=1)
            styles += [("FONTNAME", (0, 0), (-1, -1), font), ("FONTSIZE", (0, 0), (-1, -1), 8.5)]
            tbl.setStyle(TableStyle(styles))
            story.append(tbl)

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.setFillColor(colors.HexColor("#6e7781"))
        canvas.drawString(margin, margin - 2 * mm, company)
        canvas.drawRightString(page[0] - margin, margin - 2 * mm, f"{clean(t['page'])} {canvas.getPageNumber()}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


# ── Excel ─────────────────────────────────────────────────────────────────────

def xlsx(project, lang, today=None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    today = today or timezone.localdate()
    t = L[lang]
    data = rows(project, lang)
    wb = Workbook()
    ws = wb.active
    ws.title = WHATS["plan"]["file"]
    ws["A1"] = f"{project.code} · {project.name}"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"{WHATS['plan'][lang]} · {t['asof']} {_date_text(today, lang)}"
    ws["A2"].font = Font(color="57606A")
    head = [t["phase"], t["step"], t["who"], t["start"], t["end"], t["days"], t["status"]]
    starts = [r["start"] for r in data if r["start"]] + [r["end"] for r in data if r["end"]]
    weeks = []
    if starts:
        first = min(starts) - timedelta(days=min(starts).weekday())
        last = max(starts)
        d = first
        while d <= last and len(weeks) < MAX_WEEKS:
            weeks.append(d)
            d += timedelta(days=7)
    hr = 4
    thin = Side(style="thin", color="D0D7DE")
    for i, label in enumerate(head, 1):
        c = ws.cell(row=hr, column=i, value=label)
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="EEF2F6")
        c.border = Border(bottom=thin)
    base = len(head) + 1
    for j, wk in enumerate(weeks):
        col = base + j
        top = ws.cell(row=hr - 1, column=col, value=wk.strftime("%d.%m") if lang != "en" else wk.strftime("%m-%d"))
        top.alignment = Alignment(text_rotation=90, horizontal="center")
        top.font = Font(size=8, color="57606A")
        c = ws.cell(row=hr, column=col, value=f"{t['week']}{wk.isocalendar()[1]}")
        c.font = Font(bold=True, size=8)
        c.alignment = Alignment(horizontal="center")
        c.fill = PatternFill("solid", fgColor="EEF2F6")
        ws.column_dimensions[get_column_letter(col)].width = 5
    r = hr + 1
    last_phase = None
    for row in data:
        if row["phase_id"] != last_phase:
            last_phase = row["phase_id"]
            c = ws.cell(row=r, column=1, value=row["phase"])
            c.font = Font(bold=True)
            for col in range(1, base + len(weeks)):
                ws.cell(row=r, column=col).fill = PatternFill("solid", fgColor="F6F8FA")
            r += 1
        vals = ["", ("◆ " if row["milestone"] else "") + row["title"], row["who"],
                row["start"] if not row["milestone"] else None, row["end"], row["days"] or None, row["status_text"]]
        for i, v in enumerate(vals, 1):
            c = ws.cell(row=r, column=i, value=v)
            if isinstance(v, date):
                c.number_format = "DD.MM.YYYY" if lang != "en" else "YYYY-MM-DD"
        fill = PatternFill("solid", fgColor=STATUS_FILL.get(row["status"], "BFD7EA"))
        for j, wk in enumerate(weeks):
            wend = wk + timedelta(days=6)
            cell = ws.cell(row=r, column=base + j)
            if row["milestone"] and row["end"] and wk <= row["end"] <= wend:
                cell.value = "◆"
                cell.alignment = Alignment(horizontal="center")
                cell.font = Font(bold=True, color="CF222E")
            elif row["start"] and row["end"] and not row["milestone"] and row["start"] <= wend and row["end"] >= wk:
                cell.fill = fill
            if wk <= today <= wend:
                cell.border = Border(left=Side(style="thin", color="CF222E"))
        r += 1
    widths = [22, 44, 20, 12, 12, 7, 14]
    for i, wdt in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = wdt
    ws.freeze_panes = ws.cell(row=hr + 1, column=base)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Лист ──────────────────────────────────────────────────────────────────────

def recipients(project) -> list:
    """Кому за замовчуванням: учасники-клієнти проєкту, інакше email клієнта CRM (людина змінить у листі)."""
    emails = list(project.participants.filter(role="customer").values_list("email", flat=True)[:3])
    if not emails and project.customer_id and project.customer.email:
        emails = [project.customer.email]
    return [e for e in emails if e]


def mail(user, project, what, lang, files, today=None) -> str:
    """Чернетка у власній скриньці з файлами → адреса форми листа. files: [(name, bytes, content_type)]."""
    from email_assistant import attachments
    from email_assistant.models import EmailAccount, EmailDraft
    account = EmailAccount.objects.filter(user=user, is_active=True).order_by("-is_primary").first()
    if account is None:
        raise ExportError("У Minerva немає вашої поштової скриньки — файли можна завантажити й надіслати самому")
    today = today or timezone.localdate()
    t = L[lang]
    label = WHATS[what][lang]
    fmt = {"what": label, "what_lc": label if lang == "de" else label[0].lower() + label[1:], "code": project.code,
           "name": project.name, "date": _date_text(today, lang)}
    items = [attachments.store_file(name, data, ctype) for name, data, ctype in files]
    draft = EmailDraft.objects.create(account=account, to_emails=recipients(project),
                                      subject=t["subject"].format(**fmt)[:500], body=t["body"].format(**fmt),
                                      attachments=items, source=EmailDraft.SOURCE_MANUAL)
    return f"/email/compose/?draft={draft.pk}"
