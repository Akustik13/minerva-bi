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
# 📅 Пропозиції звірки плану з нарадою (projects/plan_review.py) — окремим розділом PDF, аркушем Excel і рядком листа
P = {
    "de": {"title": "Vorgeschlagene Änderungen aus der Besprechung", "titles": "Vorgeschlagene Änderungen aus den Besprechungen",
           "meeting": "Besprechung", "step": "Schritt / Phase", "change": "Änderung", "why": "Begründung",
           "dates": "Termine", "status": "Status", "name": "Name", "who": "wer", "phase": "Phase", "after": "nach",
           "new_step": "neuer Schritt", "new_ms": "neuer Meilenstein", "remove": "entfällt", "new_phase": "neue Phase",
           "phase_remove": "Phase entfällt", "sheet": "Änderungen",
           "note": "Gestrichelt im Diagramm: Vorschläge aus der Besprechung vom {days} — noch nicht übernommen.",
           "mail": "Gestrichelt markierte Termine im Diagramm sind Vorschläge aus unserer Besprechung vom {days} — noch nicht verbindlich.\n\n"},
    "en": {"title": "Proposed changes from the meeting", "titles": "Proposed changes from the meetings",
           "meeting": "Meeting", "step": "Step / phase", "change": "Change", "why": "Reason",
           "dates": "dates", "status": "status", "name": "name", "who": "who", "phase": "phase", "after": "after",
           "new_step": "new step", "new_ms": "new milestone", "remove": "dropped", "new_phase": "new phase",
           "phase_remove": "phase dropped", "sheet": "Changes",
           "note": "Dashed in the chart: proposals from the meeting on {days} — not applied yet.",
           "mail": "Dates shown dashed in the chart are proposals from our meeting on {days} — not final yet.\n\n"},
    "uk": {"title": "Запропоновані зміни з наради", "titles": "Запропоновані зміни з нарад",
           "meeting": "Нарада", "step": "Крок / фаза", "change": "Зміна", "why": "Чому",
           "dates": "дати", "status": "стан", "name": "назва", "who": "хто робить", "phase": "фаза", "after": "після",
           "new_step": "новий крок", "new_ms": "нова віха", "remove": "прибрати", "new_phase": "нова фаза",
           "phase_remove": "прибрати фазу", "sheet": "Зміни",
           "note": "Пунктиром на діаграмі — пропозиції з наради {days}, ще не застосовані.",
           "mail": "Пунктиром на діаграмі позначено пропозиції з нашої наради {days} — ще не остаточно.\n\n"},
}
MEETING_HEX = {1: "8250DF", 2: "0B8C96", 3: "BC4C00", 4: "BF3989", 5: "8A6A2E"}     # як pj.css --pjr-m1…m5 (світла)
MEETING_TINT = {1: "EFE7FB", 2: "DFF2F3", 3: "F8E6D9", 4: "F6E1EE", 5: "EFE8DD"}
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


# ── 🌐 Назви кроків і фаз іншою мовою, ніж план (AI, core/ai_texts) ─────────────
NAMES_TTL = 30 * 24 * 3600          # переклад тих самих назв тією самою мовою й моделлю — з кешу, без оплати
NAME_MAX = 300
CYR = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюяъыэё"


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def plan_names(project) -> list:
    """Назви фаз і кроків плану (без повторів, у порядку плану) — що перекладати."""
    out, seen = [], set()
    phases = list(project.phases.order_by("order", "pk").values_list("name", flat=True))
    steps = list(ProjectStep.objects.filter(project=project).order_by("phase__order", "phase_id", "order", "pk")
                 .values_list("title", flat=True))
    for name in phases + steps:
        name = _norm(name)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def names_script(project) -> str:
    """Якою абеткою написано план: cyr — переважно кирилицею, lat — латиницею, '' — немає назв."""
    names = plan_names(project)
    if not names:
        return ""
    cyr = sum(1 for n in names if any(ch in CYR for ch in n.lower()))
    return "cyr" if cyr * 2 >= len(names) else "lat"


def translate_names(project, user, lang, model="") -> dict:
    """{назва: переклад} усіх фаз і кроків мовою lang (core/ai_texts, модель — model). Той самий набір назв, мова й
    модель — з кешу core.shared_cache (NAMES_TTL), без нового запиту до AI."""
    import hashlib
    import json

    from core import ai_texts
    from core.shared_cache import cache
    if lang not in LANGS:
        raise ExportError("Виберіть мову документа")
    model = model if model in ai_texts.MODELS else ai_texts.DEFAULT_MODEL
    names = plan_names(project)
    if not names:
        return {}
    digest = hashlib.sha1(json.dumps(names, ensure_ascii=False).encode("utf-8")).hexdigest()[:20]
    key = f"pj-export-names:{project.pk}:{lang}:{model}:{digest}"
    hit = cache.get(key)
    if isinstance(hit, dict):
        return hit
    try:
        done = ai_texts.translate_list(user, names, lang, model,
                                       about="These are names of phases and steps of a project plan (Gantt chart).")
    except ai_texts.TranslateError as e:
        raise ExportError(f"{e} — або експортуйте без перекладу назв") from None
    out = {n: t[:NAME_MAX] for n, t in zip(names, done) if t and t != n}
    cache.set(key, out, NAMES_TTL)
    return out


def clean_names(project, raw) -> dict:
    """Переклад назв з форми експорту (JSON) — лише справжні назви цього плану, рядки до NAME_MAX."""
    import json
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    known = set(plan_names(project))
    return {_norm(k): _norm(v)[:NAME_MAX] for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and _norm(k) in known and _norm(v)}


def names_in(lang, names=None):
    """Назва кроку чи фази мовою документа — лише повним збігом зі словником інтерфейсу (назви з шаблонів плану:
    «Прототип» → «Prototype»), свої — як написано; так само, як сторінка й діаграма ([data-mv-tr="exact"])."""
    from core.ui_text import catalog
    from core.ui_text.text import edges
    try:
        exact = catalog.dictionary(lang)["exact"] if lang in catalog.TARGETS else {}
    except Exception:
        exact = {}

    def name(text):
        got = (names or {}).get(_norm(text))              # 🌐 переклад AI з вікна експорту — першим
        if got:
            return got
        pre, core, suf = edges(text or "")
        got = exact.get(core)
        return f"{pre}{got}{suf}" if got else (text or "")
    return name


def rows(project, lang, names=None) -> list:
    """Кроки плану з прогнозом (кеш schedule.persist — оновлює overview.collect) для таблиці PDF і Excel."""
    from . import overview
    overview.collect(project)                                   # свіжий прогноз кроків
    t = L[lang]
    name = names_in(lang, names)
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
        out.append({"phase": name(phases[s.phase_id].name) if s.phase_id in phases else t["nophase"],
                    "phase_id": s.phase_id or 0, "title": name(s.title), "milestone": s.kind == ProjectStep.MILESTONE,
                    "who": who, "start": start, "end": end,
                    "days": (end - start).days + 1 if start and end and s.kind != ProjectStep.MILESTONE else 0,
                    "status": status, "status_text": t["status_map"].get(status, status)})
    return out


def proposals(project, user, lang, names=None, quotes=False) -> dict | None:
    """Зміни звірки з нарадою, що чекають ✓ (лише звірка цієї людини — наради особисті) мовою документа:
    {meetings [{tag, m, day}], days — «18.11.2026» / «17.11.2026, 18.11.2026», rows [{tag, m, day, title, change, why,
    quotes}]}; немає — None. quotes — ще й тези наради (за замовчуванням ні: лише «чому»)."""
    from . import plan_review
    from .models import PlanReview
    review = plan_review.active(project, user)
    if review is None or review.status != PlanReview.READY:
        return None
    pending = [c for c in review.changes.all() if c.status in plan_review.OPEN_STATES]
    if not pending:
        return None
    p, t, name = P[lang], L[lang], names_in(lang, names)
    objs, marks = plan_review.meeting_marks(review)
    titles = plan_review._names(review)
    refs = review.refs or {}
    rows = []
    for ch in pending:
        m, tag = plan_review.change_mark(ch, review, objs, marks)
        mt = plan_review.change_meeting(ch, review, objs)
        f, b = (ch.data or {}).get("set") or {}, ch.before or {}
        title = b.get("title") or b.get("name") or f.get("title") or f.get("name") or titles.get(ch.target, "")
        rows.append({"tag": tag, "m": m, "day": mt.day if mt else None, "title": name(title),
                     "change": _change_text(ch, f, b, lang, titles, name), "why": ch.why or "",
                     "quotes": [refs[k]["text"] for k in ch.sources or [] if k in refs] if quotes else []})
    meetings = [{"tag": marks[x.pk][1], "m": marks[x.pk][0], "day": x.day} for x in objs.values()]
    return {"meetings": meetings, "rows": rows, "several": len(meetings) > 1,
            "days": ", ".join(_date_text(x["day"], lang) for x in meetings)}


def _span_text(start, end, kind, lang) -> str:
    def d(v):
        try:
            return _date_text(date.fromisoformat(str(v)), lang)
        except (TypeError, ValueError):
            return "—"
    if kind == ProjectStep.MILESTONE:
        return "◆ " + d(end or start)
    return f"{d(start)} – {d(end)}"


def _change_text(ch, f, b, lang, titles, name) -> str:
    from . import plan_review
    p, t = P[lang], L[lang]
    parts = []
    if ch.op == "update":
        kind = f.get("kind", b.get("kind"))
        if any(k in f for k in ("start", "end", "duration")):
            s, e, _days = plan_review._dates(
                {k: v for k, v in f.items() if k in ("start", "end", "duration")},
                date.fromisoformat(b["start"]) if b.get("start") else None,
                date.fromisoformat(b["end"]) if b.get("end") else None, b.get("duration") or 1, kind)
            parts.append(f"{p['dates']} {_span_text(b.get('start'), b.get('end'), kind, lang)} → "
                         f"{_span_text(s.isoformat() if s else '', e.isoformat() if e else '', kind, lang)}")
        if "status" in f:
            parts.append(f"{p['status']} {t['status_map'].get(b.get('status'), b.get('status') or '—')} → "
                         f"{t['status_map'].get(f['status'], f['status'])}")
        if "title" in f and f["title"] != b.get("title"):
            parts.append(f"{p['name']} → «{f['title']}»")
        if "side" in f:
            parts.append(f"{p['who']} → {t['side_map'].get(f['side'], f['side'])}")
        if "phase" in f:
            parts.append(f"{p['phase']} → {name(titles.get(f['phase'], '—'))}")
        if "after" in f and f["after"]:
            parts.append(f"{p['after']}: " + ", ".join(f"«{name(titles.get(a, a))}»" for a in f["after"]))
    elif ch.op == "add":
        kind = f.get("kind", ProjectStep.TASK)
        s, e, _days = plan_review._dates(f, None, None, f.get("duration") or 1, kind)
        parts.append((p["new_ms"] if kind == ProjectStep.MILESTONE else p["new_step"]) + " "
                     + _span_text(s.isoformat() if s else "", e.isoformat() if e else "", kind, lang))
        if f.get("phase"):
            parts.append(f"{p['phase']}: {name(titles.get(f['phase'], f['phase']))}")
    elif ch.op == "remove":
        parts.append(p["remove"])
    elif ch.op == "add_phase":
        parts.append(p["new_phase"])
    elif ch.op == "update_phase":
        parts += [f"{p['name']} → «{f['name']}»"] if f.get("name") else []
    elif ch.op == "remove_phase":
        parts.append(p["phase_remove"])
    return " · ".join(parts)


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


def pdf(project, what, lang, png, table=False, today=None, titled=False, names=None, props=None) -> bytes:
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
    head = [Paragraph(_esc(title), h1), Paragraph(_esc(clean(" · ".join(line))), sub), Spacer(1, 4 * mm)]
    story = [] if (titled and png) else list(head)       # заголовок і легенда вже в картинці
    if png:
        frame_pad = 12 + 1                         # поля рамки сторінки (6 pt з кожного боку) + запас округлення —
        avail_w = page[0] - 2 * margin - frame_pad  # інакше висока діаграма «too large on page» (LayoutError)
        avail_h = page[1] - 2 * margin - (4 * mm if titled else 22 * mm) - frame_pad
        ratio = min(avail_w / w, avail_h / h)
        story.append(Image(io.BytesIO(png), width=w * ratio, height=h * ratio))
    if table and what == "plan":
        data = rows(project, lang, names)
        if data:
            if png:
                story.append(PageBreak())
            if titled and png:
                story += head
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
    if props and what == "plan":                       # 📅 пропозиції з наради — окремою сторінкою
        pl = P[lang]
        if story:
            story.append(PageBreak())
        story.append(Paragraph(_esc(clean(pl["titles"] if props["several"] else pl["title"])), h1))
        story.append(Paragraph(_esc(clean(pl["note"].format(days=props["days"]))), sub))
        story.append(Spacer(1, 3 * mm))
        small = ParagraphStyle("q", fontName=font, fontSize=7.5, leading=9.5, textColor=colors.HexColor("#57606a"))
        body = [[Paragraph(_esc(clean(x)), cellb) for x in (pl["meeting"], pl["step"], pl["change"], pl["why"])]]
        styles = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d0d7de")),
                  ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f6")), ("VALIGN", (0, 0), (-1, -1), "TOP")]
        for r in props["rows"]:
            tagged = ParagraphStyle(f"m{r['m']}", parent=cellb, textColor=colors.HexColor("#" + MEETING_HEX.get(r["m"], "8250DF")))
            why = [Paragraph(_esc(clean(r["why"])), cell)] if r["why"] else []
            why += [Paragraph(_esc(clean(f"„{q}“")), small) for q in r["quotes"]]
            body.append([Paragraph(_esc(clean(" ".join(x for x in (r["tag"], _date_text(r["day"], lang) if r["day"] else "") if x))), tagged),
                         Paragraph(_esc(clean(r["title"])), cell), Paragraph(_esc(clean(r["change"])), cell), why or ""])
            styles.append(("BACKGROUND", (0, len(body) - 1), (0, len(body) - 1),
                           colors.HexColor("#" + MEETING_TINT.get(r["m"], "EFE7FB"))))
        avail = page[0] - 2 * margin
        tbl = Table(body, colWidths=[avail * 0.1, avail * 0.26, avail * 0.32, avail * 0.32], repeatRows=1)
        tbl.setStyle(TableStyle(styles + [("FONTNAME", (0, 0), (-1, -1), font), ("FONTSIZE", (0, 0), (-1, -1), 8.5)]))
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

def xlsx(project, lang, today=None, names=None, props=None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    today = today or timezone.localdate()
    t = L[lang]
    data = rows(project, lang, names)
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
    if props:                                          # 📅 пропозиції з наради — окремий аркуш
        pl = P[lang]
        ch = wb.create_sheet(pl["sheet"])
        ch["A1"] = pl["titles"] if props["several"] else pl["title"]
        ch["A1"].font = Font(bold=True, size=13)
        ch["A2"] = pl["note"].format(days=props["days"])
        ch["A2"].font = Font(color="57606A")
        for i, label in enumerate((pl["meeting"], pl["step"], pl["change"], pl["why"]), 1):
            c = ch.cell(row=4, column=i, value=label)
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="EEF2F6")
        for n, r in enumerate(props["rows"], 5):
            tag = ch.cell(row=n, column=1, value=" ".join(x for x in (r["tag"], _date_text(r["day"], lang) if r["day"] else "") if x))
            tag.font = Font(bold=True, color=MEETING_HEX.get(r["m"], "8250DF"))
            tag.fill = PatternFill("solid", fgColor=MEETING_TINT.get(r["m"], "EFE7FB"))
            ch.cell(row=n, column=2, value=r["title"])
            ch.cell(row=n, column=3, value=r["change"])
            ch.cell(row=n, column=4, value="\n".join([r["why"]] + [f"„{q}“" for q in r["quotes"]]).strip())
            for col in range(1, 5):
                ch.cell(row=n, column=col).alignment = Alignment(vertical="top", wrap_text=True)
        for col, wdt in zip("ABCD", (14, 40, 50, 60)):
            ch.column_dimensions[col].width = wdt
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


def mail(user, project, what, lang, files, today=None, props=None) -> str:
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
    body = t["body"].format(**fmt) + (P[lang]["mail"].format(days=props["days"]) if props else "")
    draft = EmailDraft.objects.create(account=account, to_emails=recipients(project),
                                      subject=t["subject"].format(**fmt)[:500], body=body,
                                      attachments=items, source=EmailDraft.SOURCE_MANUAL)
    return f"/email/compose/?draft={draft.pk}"
