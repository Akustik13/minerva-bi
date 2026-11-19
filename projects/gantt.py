"""Гант плану проєкту (вкладка «📅 План» пульта) → SVG без бібліотек (кольори — змінні тем Minerva).

Рядки: фаза (підсумкова смуга, базовий план тонкою лінією) і її кроки: смуга кроку чи ◆ віха, тонка смуга
базового плану під нею, червоний контур — критичний шлях, «+N» — зсув від базового плану, стрілки — залежності
«фініш → старт». Лінії: сьогодні, строк клієнту. editable: рядки фокусуються (tabindex) — клік / Enter відкриває
вікно кроку чи фази (data-step / data-phase, JS — projects/static/projects/pj.js: ще й перетягування смуг за
data-ppd / data-day0 і правий клік); підказка рядка — <title>. brief — «Коротко»: лише фази й віхи (◆).
"""
import uuid
from datetime import date, timedelta

from django.utils import timezone
from django.utils.html import escape

from .models import ProjectStep

W, LEFT, X0, RIGHT = 1000, 336, 344, 12
HEAD, RH = 40, 24
TITLE_MAX = 36
MONTHS = ["січ", "лют", "бер", "кві", "тра", "чер", "лип", "сер", "вер", "жов", "лис", "гру"]
SIDE_SHORT = {ProjectStep.WE: "ми", ProjectStep.CUSTOMER: "клієнт", ProjectStep.SUPPLIER: "постач.",
              ProjectStep.COLLEAGUE: "колега"}
STATUS_ICON = {ProjectStep.DONE: "✓", ProjectStep.DOING: "●", ProjectStep.WAITING: "⏳", ProjectStep.SKIPPED: "⤼",
               ProjectStep.TODO: "○"}


def _n(v) -> str:
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def _cut(text, n=TITLE_MAX) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _name(text, n) -> str:
    """Назва кроку чи фази: 🌐 інша мова інтерфейсу (і експорт) перекладає її лише повним збігом зі словником — назви
    з шаблонів плану («Прототип» → «Prototype»), свої — як написано (mv_translate.js, [data-mv-tr="exact"]);
    скорочену — за повною в data-mv-full."""
    full = " ".join(str(text or "").split())
    shown = _cut(full, n)
    cut = f' data-mv-cut="{len(shown)}"' if shown != full else ""
    return f'<tspan data-mv-tr="exact" data-mv-full="{escape(full)}"{cut}>{escape(shown)}</tspan>'


def _fmt(d) -> str:
    return d.strftime("%d.%m") if d else ""


def _gd(v):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def has_ghosts(gh) -> bool:
    """Є пропозиції звірки з нарадою, які видно на діаграмі."""
    gh = gh or {}
    return bool(gh.get("steps") or gh.get("added") or gh.get("phases") or gh.get("new_phases"))


def _mcls(g) -> str:
    """Клас кольору наради пропозиції (pj.css .pjm-1 … .pjm-5)."""
    m = (g or {}).get("m")
    return f" pjm-{int(m)}" if isinstance(m, int) and 1 <= m <= 9 else ""


def _ghost_rows(plan, ghosts, brief=False) -> list:
    """Рядки діаграми з пропозиціями звірки (projects/plan_review.ghosts): новий крок — одразу після кроку, за яким
    його поставили (інакше — у кінці своєї фази), нова фаза — в кінці з її кроками. ("phase" | "step" | "ghost" |
    "ghost_phase", об'єкт)"""
    gh = ghosts or {}
    added = [a for a in gh.get("added") or [] if not brief or a.get("kind") == ProjectStep.MILESTONE]
    step_ids = {c.step.pk for c in plan.steps}
    placed, rows = set(), []
    for pc in plan.phases:
        rows.append(("phase", pc))
        ph_pk = pc.phase.pk if pc.phase else None
        for c in pc.steps:
            if not brief or c.step.kind == ProjectStep.MILESTONE:
                rows.append(("step", c))
            for a in added:
                if a.get("after") == c.step.pk and a["change"] not in placed:
                    rows.append(("ghost", a))
                    placed.add(a["change"])
        for a in added:
            if (a["change"] not in placed and a.get("after") not in step_ids and not a.get("new_phase")
                    and a.get("phase") == ph_pk):
                rows.append(("ghost", a))
                placed.add(a["change"])
    for np in gh.get("new_phases") or []:
        rows.append(("ghost_phase", np))
        for a in added:
            if a.get("new_phase") == np["key"] and a["change"] not in placed:
                rows.append(("ghost", a))
                placed.add(a["change"])
    rows += [("ghost", a) for a in added if a["change"] not in placed]
    return rows


NW_DEFAULT, NW_MIN, NW_MAX = 196, 150, 560   # ширина назви кроку (від x 22) у колонці «КРОК»
WIN_MIN_DAYS = 7                             # найвужче вікно часу (🔍 масштаб)


def clean_view(raw) -> dict:
    """Вигляд діаграми Minerva з браузера: {"gw": "YYYY-MM-DD,YYYY-MM-DD" — період (🔍), "nw": ширина колонки «КРОК»}
    → {from, to, nw} лише з правильними значеннями (решта — як за замовчуванням)."""
    raw = raw or {}
    out = {}
    try:
        nw = int(str(raw.get("nw") or "").strip() or 0)
    except ValueError:
        nw = 0
    if nw:
        out["nw"] = min(max(nw, NW_MIN), NW_MAX)
    parts = str(raw.get("gw") or "").split(",")
    if len(parts) == 2:
        f, t = _gd(parts[0].strip()), _gd(parts[1].strip())
        if f and t and (t - f).days >= WIN_MIN_DAYS - 1:
            out["from"], out["to"] = f, t
    return out


def span(project, plan, today=None, ghosts=None) -> tuple:
    """Увесь проєкт на діаграмі: (перший день, день після останнього) — з запасом, не менше трьох тижнів."""
    today = today or timezone.localdate()
    gh = ghosts or {}
    dates = [today]
    for c in plan.steps:
        dates += [d for d in (c.start, c.end, c.step.baseline_start, c.step.baseline_end) if d]
    for g in list((gh.get("steps") or {}).values()) + list(gh.get("added") or []):
        dates += [d for d in (_gd(g.get("start")), _gd(g.get("end"))) if d]
    for pc in plan.phases:
        dates += [d for d in (pc.start, pc.end) if d]
    if project.due_date:
        dates.append(project.due_date)
    start, end = min(dates) - timedelta(days=2), max(dates) + timedelta(days=4)
    if (end - start).days < 21:
        end = start + timedelta(days=21)
    return start, end


def render(project, plan, today=None, editable=False, brief=False, ghosts=None, view=None) -> str:
    """ghosts — пропозиції звірки з нарадою (plan_review.ghosts): інші дати пунктиром іншим кольором, нові кроки й
    фази — рядками-привидами «＋», прибрати — перекреслено. Нічого з цього не можна тягнути: ✓ / ✕ — у звірці.
    view — clean_view: {from, to} — показати лише цей період (🔍 масштаб: смуги за його межами обрізано), {nw} —
    ширина колонки «КРОК» (довгі назви); ширша колонка — ширша й уся діаграма, шкала часу не стискається."""
    today = today or timezone.localdate()
    if not plan.has_plan:
        return ""
    gh = ghosts or {}
    view = view or {}
    full_start, full_end = span(project, plan, today, gh)
    start, end = full_start, full_end
    if view.get("from") and view.get("to"):
        start, end = view["from"], view["to"] + timedelta(days=1)
    nw = view.get("nw") or NW_DEFAULT
    who_x = 22 + nw + 4
    dates_x = who_x + 46
    left = dates_x + 68
    x0 = left + 8
    w = W + (nw - NW_DEFAULT)
    step_chars, phase_chars = max(int((nw - 4) / 6.6), 8), max(int((nw + 12) / 5.2), 10)
    days = (end - start).days
    ppd = (w - RIGHT - x0) / days

    def x(d):
        return x0 + (d - start).days * ppd

    rows = _ghost_rows(plan, gh, brief)      # ("phase", PhaseCalc) | ("step", StepCalc) | привиди пропозицій
    height = HEAD + len(rows) * RH + 8
    top_of = {}
    focus = ' tabindex="0" role="button"' if editable else ""
    clip = "pjgK" + uuid.uuid4().hex[:10]
    clip_on = f'<g clip-path="url(#{clip})">'
    windowed = (start, end) != (full_start, full_end)
    out = [f'<svg class="pjg" viewBox="0 0 {w} {height}" data-ppd="{ppd:.4f}" data-day0="{start.isoformat()}" '
           f'data-x0="{x0}" data-w="{w}" data-nw="{nw}" data-span0="{full_start.isoformat()}" '
           f'data-span1="{full_end.isoformat()}" data-end="{end.isoformat()}"{" data-win=1" if windowed else ""} '
           f'role="group" aria-label="'
           f'{escape(f"План проєкту {project.code}: {len(plan.steps)} кроків, прогноз до {_fmt(plan.end)}")}">',
           '<defs><marker id="pjgA" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" '
           'orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 z" class="pjg-ah"/></marker>'
           '<marker id="pjgAc" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" '
           'orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 z" class="pjg-ah-c"/></marker>'
           f'<clipPath id="{clip}"><rect x="{x0}" y="0" width="{w - x0}" height="{height}"/></clipPath></defs>']

    # Сітка: вихідні, тижні, місяці
    d = start
    while d < end:
        if d.weekday() >= 5:
            out.append(f'<rect x="{_n(x(d))}" y="{HEAD}" width="{_n(ppd)}" height="{len(rows) * RH}" class="pjg-we"/>')
        if d.weekday() == 0:
            out.append(f'<line x1="{_n(x(d))}" y1="{HEAD - 14}" x2="{_n(x(d))}" y2="{height - 8}" class="pjg-wk"/>')
            if ppd * 7 >= 34:
                out.append(f'<text x="{_n(x(d) + 2)}" y="{HEAD - 4}" class="pjg-wkt">{d:%d.%m}</text>')
        if d.day == 1 or d == start:
            out.append(f'<line x1="{_n(x(d))}" y1="2" x2="{_n(x(d))}" y2="{height - 8}" class="pjg-mon"/>')
            nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
            label = MONTHS[d.month - 1] + (f" {d.year}" if d.month == 1 or d == start else "")
            if x(min(nxt, end)) - x(d) > max(34, len(label) * 6.4 + 8):     # підпис не налазить на наступний
                out.append(f'<text x="{_n(x(d) + 4)}" y="13" class="pjg-mont">{label}</text>')
        d += timedelta(days=1)
    out.append(f'<line x1="0" y1="{HEAD}" x2="{w}" y2="{HEAD}" class="pjg-mon"/>')
    out.append(f'<line x1="{left}" y1="0" x2="{left}" y2="{height - 8}" class="pjg-mon"/>')
    for label, lx in (("КРОК", 10), ("ХТО", who_x), ("ПРОГНОЗ", dates_x)):
        out.append(f'<text x="{lx}" y="{HEAD - 6}" class="pjg-th">{label}</text>')
    # ↔ межа колонки «КРОК»: тягнути — ширша / вужча, подвійний клік — під найдовшу назву (pj.js)
    out.append(f'<rect x="{who_x - 9}" y="{HEAD - 20}" width="6" height="18" rx="2" class="pjg-colsz" data-pjg-nw="{nw}">'
               f'<title>Тягніть — ширша колонка «КРОК» · подвійний клік — під найдовшу назву</title></rect>')

    # Рядки
    for i, (kind, item) in enumerate(rows):
        top = HEAD + i * RH
        cy = top + RH / 2
        if kind == "ghost_phase":
            pre = f"{item['tag']} " if item.get("tag") else ""
            out.append(f'<g class="pjg-row pjg-ghost-row{_mcls(item)}" data-change="{item["change"]}"><title>'
                       f'{escape("Запропоновано з наради: нова фаза «" + item["name"] + "» — ✓ / ✕ у звірці плану")}</title>'
                       f'<rect x="0" y="{top}" width="{w}" height="{RH}" class="pjg-rowbg pjg-phbg pjg-ghost-bg"/>'
                       f'<text x="10" y="{_n(cy + 4)}" class="pjg-pname pjg-ghost-t">{escape(pre)}＋ {_name(item["name"], phase_chars - 4)}</text></g>')
            continue
        if kind == "ghost":
            a = item
            gs, ge = _gd(a.get("start")), _gd(a.get("end"))
            tip = f"Запропоновано з наради: «{a['title']}» {a.get('label', '')} — ✓ / ✕ у звірці плану"
            g = [f'<g class="pjg-row pjg-ghost-row{_mcls(a)}" data-change="{a["change"]}"><title>{escape(tip)}</title>',
                 f'<rect x="0" y="{top}" width="{w}" height="{RH}" class="pjg-rowbg pjg-ghost-bg"/>',
                 f'<text x="22" y="{_n(cy + 4)}" class="pjg-t pjg-ghost-t">＋ {_name(a["title"], step_chars)}</text>',
                 f'<text x="{dates_x}" y="{_n(cy + 4)}" class="pjg-dates pjg-ghost-t">'
                 f'{escape(a.get("label", "").replace("＋ ", ""))}</text>', clip_on]
            if a.get("kind") == ProjectStep.MILESTONE and ge:
                mx = x(ge) + ppd / 2
                g.append(f'<polygon points="{_n(mx)},{_n(cy - 7)} {_n(mx + 7)},{_n(cy)} {_n(mx)},{_n(cy + 7)} '
                         f'{_n(mx - 7)},{_n(cy)}" class="pjg-ghost"/>')
            elif gs and ge:
                g.append(f'<rect x="{_n(x(gs))}" y="{_n(cy - 5.5)}" width="{_n(max(x(ge + timedelta(days=1)) - x(gs), 3))}" '
                         f'height="11" rx="2.5" class="pjg-ghost"/>')
            g.append("</g></g>")
            out.append("".join(g))
            continue
        if kind == "phase":
            pc = item
            ph = pc.phase
            name = f"{ph.icon} {ph.name}".strip() if ph else "Без фази"
            tip = name + (f" · умова переходу: {ph.gate}" if ph and ph.gate else "") + \
                f" · {pc.done} з {len(pc.steps)} кроків" + (f" · {_fmt(pc.start)}–{_fmt(pc.end)}" if pc.start else "")
            attr = f' data-phase="{ph.pk}"{focus}' if ph else ""
            span_attr = (f' data-ph0="{pc.start.isoformat()}" data-ph1="{pc.end.isoformat()}"'
                         if pc.start and pc.end else "")
            pname = ((f"{escape(ph.icon)} " if ph.icon else "") +
                     _name(ph.name, phase_chars - 2 if ph.icon else phase_chars)) if ph else escape(name)
            g = [f'<g class="pjg-row pjg-phase pjg-{pc.status}"{attr}{span_attr}><title>{escape(tip)}</title>',
                 f'<rect x="0" y="{top}" width="{w}" height="{RH}" class="pjg-rowbg pjg-phbg"/>',
                 f'<text x="10" y="{_n(cy + 4)}" class="pjg-pname">{pname}</text>',
                 f'<text x="{dates_x}" y="{_n(cy + 4)}" class="pjg-dates">{_fmt(pc.start)}–{_fmt(pc.end)}</text>', clip_on]
            if pc.start and pc.end:
                g.append(f'<rect x="{_n(x(pc.start))}" y="{_n(cy - 4)}" width="{_n(max(x(pc.end + timedelta(days=1)) - x(pc.start), 2))}" '
                         f'height="8" rx="2" class="pjg-sum"/>')
            if pc.base_start and pc.base_end:
                g.append(f'<rect x="{_n(x(pc.base_start))}" y="{_n(cy + 6)}" width="{_n(max(x(pc.base_end + timedelta(days=1)) - x(pc.base_start), 2))}" '
                         f'height="2.5" class="pjg-base"/>')
            gp = (gh.get("phases") or {}).get(ph.pk) if ph else None
            if gp:
                text = (f"{gp['tag']} " if gp.get("tag") else "") + (
                    "− прибрати фазу" if gp.get("remove") else f"→ «{gp.get('name')}»" if gp.get("name") else "✎ зміна фази")
                g.append(f'<text x="{_n(w - RIGHT - 4)}" y="{_n(cy + 4)}" text-anchor="end" class="pjg-ghost-lab{_mcls(gp)}">'
                         f'{escape(text)}<title>Запропоновано з наради — ✓ / ✕ у звірці плану</title></text>')
            g.append("</g></g>")
            out.append("".join(g))
            continue
        c = item
        s = c.step
        top_of[s.pk] = (top, c)
        who = (s.assignee.get_full_name() or s.assignee.username) \
            if s.assignee_id and s.side in (ProjectStep.WE, ProjectStep.COLLEAGUE) else SIDE_SHORT.get(s.side, "")
        if s.party_id and not s.assignee_id:
            who = s.party.name
        # ім'я людини чи сторони — дані (🌐 переклад інтерфейсу й експорт іншою мовою їх не чіпають)
        data_attr = ' translate="no"' if s.party_id or (s.assignee_id and s.side in (ProjectStep.WE, ProjectStep.COLLEAGUE)) else ""

        tip = (f"{STATUS_ICON.get(c.status, '')} {s.title} · "
               f"{s.party.name if s.party_id else s.get_side_display()}"
               + (f" · {who}" if s.assignee_id else "") + f" · прогноз {_fmt(c.start)}–{_fmt(c.end)}"
               + (f" · план {_fmt(s.baseline_start)}–{_fmt(s.baseline_end)}" if s.baseline_end else "")
               + (f" · зсув {c.slip:+d} дн." if c.slip else "") + (" · критичний шлях" if c.critical else "")
               + (f"\n{c.note}" if c.note else ""))
        cls = f"pjg-{c.status or 'todo'}" + (" pjg-crit" if c.critical and c.status not in ("done", "skipped") else "")
        g = [f'<g class="pjg-row pjg-step {cls}" data-step="{s.pk}"{focus}><title>{escape(tip)}</title>',
             f'<rect x="0" y="{top}" width="{w}" height="{RH}" class="pjg-rowbg"/>',
             f'<text x="22" y="{_n(cy + 4)}" class="pjg-t">{STATUS_ICON.get(c.status, "")} {_name(s.title, step_chars)}</text>',
             f'<text x="{who_x}" y="{_n(cy + 4)}" class="pjg-who pjg-side-{s.side}"'
             f'{data_attr}>{escape(_cut(who, 9))}</text>',
             f'<text x="{dates_x}" y="{_n(cy + 4)}" class="pjg-dates">'
             f'{_fmt(c.start) if s.kind != ProjectStep.MILESTONE else ""}{"–" if s.kind != ProjectStep.MILESTONE else "◆ "}{_fmt(c.end)}</text>',
             clip_on]
        if s.baseline_start and s.baseline_end:
            if s.kind == ProjectStep.MILESTONE:
                bx = x(s.baseline_end) + ppd / 2
                g.append(f'<polygon points="{_n(bx)},{_n(cy + 2)} {_n(bx + 4)},{_n(cy + 6)} {_n(bx)},{_n(cy + 10)} '
                         f'{_n(bx - 4)},{_n(cy + 6)}" class="pjg-base-d"/>')
            else:
                g.append(f'<rect x="{_n(x(s.baseline_start))}" y="{_n(cy + 6)}" '
                         f'width="{_n(max(x(s.baseline_end + timedelta(days=1)) - x(s.baseline_start), 2))}" height="2.5" '
                         f'class="pjg-base"/>')
        if c.start and c.end:
            if s.kind == ProjectStep.MILESTONE:
                mx = x(c.end) + ppd / 2
                g.append(f'<polygon points="{_n(mx)},{_n(cy - 7)} {_n(mx + 7)},{_n(cy)} {_n(mx)},{_n(cy + 7)} '
                         f'{_n(mx - 7)},{_n(cy)}" class="pjg-dia"/>')
                bar_end = mx + 8
            else:
                bx, bw = x(c.start), max(x(c.end + timedelta(days=1)) - x(c.start), 3)
                g.append(f'<rect x="{_n(bx)}" y="{_n(cy - 5.5)}" width="{_n(bw)}" height="11" rx="2.5" class="pjg-bar"/>')
                bar_end = bx + bw
            if c.slip and c.slip > 0 and c.status not in ("done", "skipped"):
                g.append(f'<text x="{_n(bar_end + 4)}" y="{_n(cy + 4)}" class="pjg-slip">+{c.slip}</text>')
            elif c.note and s.trigger:
                g.append(f'<text x="{_n(bar_end + 4)}" y="{_n(cy + 4)}" class="pjg-auto">🔗</text>')
            # ⛓ залежність від кроку іншого проєкту (його збережений прогноз)
            ext = [(p, lag) for p, lag in c.preds if not hasattr(p, "step")]
            if ext:
                late, tips = False, []
                for p, lag in ext:
                    p_end = p.f_end or p.end
                    open_ = (p.eff_status or p.status) not in ("done", "skipped")
                    late = late or bool(open_ and p_end and s.start and p_end + timedelta(days=1 + lag) > s.start)
                    tips.append(f"⛓ чекає {p.project.code}: «{p.title}»" + (f" — прогноз {_fmt(p_end)}" if p_end else ""))
                sx = (x(c.end) + ppd / 2 - 9 if s.kind == ProjectStep.MILESTONE else x(c.start)) - 14
                g.append(f'<text x="{_n(sx)}" y="{_n(cy + 4)}" class="pjg-ext{" pjg-ext-late" if late else ""}">'
                         f'<title>{escape("; ".join(tips))}</title>⛓</text>')
        g += _ghost_on_step((gh.get("steps") or {}).get(s.pk), c, x, ppd, cy, w - RIGHT, x0)
        g.append("</g></g>")
        out.append("".join(g))

    # Залежності (у межах проєкту)
    out.append(clip_on)
    for sid, (top, c) in top_of.items():
        for pred, lag in c.preds:
            if not hasattr(pred, "step") or pred.step.pk not in top_of or not pred.end or not c.start:
                continue
            ptop, pc = top_of[pred.step.pk]
            if pc.step.kind == ProjectStep.MILESTONE:
                x1 = x(pc.end) + ppd / 2 + 7
            else:
                x1 = x(pc.end + timedelta(days=1))
            if c.step.kind == ProjectStep.MILESTONE:
                x2 = x(c.end) + ppd / 2 - 7
            else:
                x2 = x(c.start)
            y1, y2 = ptop + RH / 2, top + RH / 2
            if x2 - x1 >= 8:
                mx = min(x1 + 6, (x1 + x2) / 2)
                path = f"M{_n(x1)},{_n(y1)} H{_n(mx)} V{_n(y2)} H{_n(x2 - 1.5)}"
            else:
                mid = y1 + RH / 2 if y2 > y1 else y1 - RH / 2
                path = f"M{_n(x1)},{_n(y1)} h5 V{_n(mid)} H{_n(x2 - 7)} V{_n(y2)} H{_n(x2 - 1.5)}"
            crit = c.critical and pc.critical
            out.append(f'<path d="{path}" class="{"pjg-dep-c" if crit else "pjg-dep"}" '
                       f'marker-end="url(#{"pjgAc" if crit else "pjgA"})"/>')
    out.append("</g>")

    if start <= today < end:
        tx = x(today) + ppd / 2
        out.append(f'<line x1="{_n(tx)}" y1="{HEAD - 16}" x2="{_n(tx)}" y2="{height - 8}" class="pjg-today"/>')
    if project.due_date and start <= project.due_date < end:
        dx = x(project.due_date) + ppd / 2
        out.append(f'<line x1="{_n(dx)}" y1="{HEAD - 16}" x2="{_n(dx)}" y2="{height - 8}" class="pjg-due">'
                   f'<title>Строк клієнту {project.due_date:%d.%m.%Y}</title></line>')
    out.append("</svg>")
    return "".join(out)


def _ghost_on_step(g, c, x, ppd, cy, right=None, lo=None) -> list:
    """Пропозиція до наявного кроку: нові дати — пунктирна смуга іншим кольором, «прибрати» — перекреслено."""
    if not g:
        return []
    tip = f"Запропоновано з наради: {g.get('label', '')} — ✓ / ✕ у звірці плану"
    out = []
    if g.get("remove"):
        if c.start and c.end:
            x1, x2 = x(c.start), x(c.end + timedelta(days=1))
            out.append(f'<line x1="{_n(x1 - 2)}" y1="{_n(cy)}" x2="{_n(x2 + 2)}" y2="{_n(cy)}" class="pjg-ghost-del">'
                       f'<title>{escape(tip)}</title></line>')
        anchor = x(c.end + timedelta(days=1)) + 6 if c.end else 300
    else:
        gs, ge = _gd(g.get("start")), _gd(g.get("end"))
        anchor = None
        if g.get("kind") == ProjectStep.MILESTONE and ge:
            mx = x(ge) + ppd / 2
            out.append(f'<polygon points="{_n(mx)},{_n(cy - 7)} {_n(mx + 7)},{_n(cy)} {_n(mx)},{_n(cy + 7)} '
                       f'{_n(mx - 7)},{_n(cy)}" class="pjg-ghost"><title>{escape(tip)}</title></polygon>')
            anchor = mx + 10
        elif gs and ge:
            gx, gw = x(gs), max(x(ge + timedelta(days=1)) - x(gs), 3)
            out.append(f'<rect x="{_n(gx)}" y="{_n(cy - 7.5)}" width="{_n(gw)}" height="15" rx="3" class="pjg-ghost">'
                       f'<title>{escape(tip)}</title></rect>')
            anchor = gx + gw + 4
        if anchor is None:
            anchor = x(c.end + timedelta(days=1)) + 6 if c.end else 300
    anchor = max(anchor, (lo or X0) + 4)          # 🔍 пропозиція раніше за показаний період — підпис з краю
    out.append(f'<text x="{_n(min(anchor, (right or W - RIGHT) - 60))}" y="{_n(cy + 4)}" class="pjg-ghost-lab">'
               f'{escape(g.get("label", ""))}</text>')
    return [f'<g class="pjg-prop{_mcls(g)}">'] + out + ["</g>"]


# ── 📋 Класична діаграма Ганта (малює браузер — admin/projects/project/change_form.html) ──────────────────────────

def classic_data(project, plan, today=None, ghosts=None) -> dict:
    """Таблиця кроків з номерами (1, 1.1 …) + смуги: прогноз, базовий план, стан, критичний шлях, попередники.
    Рядки — у порядку плану (фаза → кроки); крок іншого проєкту в «Попередниках» — його код. ghosts — пропозиції
    звірки з нарадою: prop у рядку кроку / фази, нові — рядки ghost (pj.js малює пунктиром іншим кольором)."""
    today = today or timezone.localdate()
    rows, number = [], {}
    phase_no = 0
    for pc in plan.phases:
        if pc.phase is not None:
            phase_no += 1
            prefix = f"{phase_no}."
            rows.append({"type": "phase", "id": pc.phase.pk, "n": str(phase_no),
                         "title": f"{pc.phase.icon} {pc.phase.name}".strip(), "start": _iso(pc.start),
                         "end": _iso(pc.end), "status": pc.status, "done": pc.done, "total": len(pc.steps)})
        else:
            prefix = ""
            if pc.steps:
                rows.append({"type": "phase", "id": "", "n": "", "title": "без фази", "start": _iso(pc.start),
                             "end": _iso(pc.end), "status": pc.status, "done": pc.done, "total": len(pc.steps)})
        for j, c in enumerate(pc.steps, 1):
            number[c.step.pk] = f"{prefix}{j}"
    for pc in plan.phases:
        for c in pc.steps:
            s = c.step
            preds = []
            for pred, lag in c.preds:
                if hasattr(pred, "step"):
                    preds.append({"id": pred.step.pk, "n": number.get(pred.step.pk, ""), "lag": lag})
                else:
                    preds.append({"id": "", "n": f"{pred.project.code}", "lag": lag, "ext": True})
            rows.append({
                "type": "step", "id": s.pk, "phase": s.phase_id or "", "n": number.get(s.pk, ""), "title": s.title,
                "kind": s.kind, "side": SIDE_SHORT.get(s.side, s.side),
                "who": ((s.assignee.get_full_name() or s.assignee.username) if s.assignee_id else
                        (s.party.name if s.party_id else "")),
                "start": _iso(c.start), "end": _iso(c.end), "bstart": _iso(s.baseline_start),
                "bend": _iso(s.baseline_end), "status": c.status, "crit": bool(c.critical and c.status not in
                                                                              ("done", "skipped")),
                "slip": c.slip or 0, "preds": preds, "days": (c.end - c.start).days + 1 if c.start and c.end else 0,
                "note": c.note or ""})
    # кроки — одразу після своєї фази
    out, by_phase = [], {}
    for r in rows:
        if r["type"] == "step":
            by_phase.setdefault(r["phase"], []).append(r)
    for r in rows:
        if r["type"] == "phase":
            out.append(r)
            out.extend(by_phase.get(r["id"], []))
    extra = []
    if ghosts:
        out, extra = _classic_ghosts(out, ghosts)
    return {"rows": out, "today": today.isoformat(), "due": _iso(project.due_date),
            "props": (ghosts or {}).get("meetings") or [] if has_ghosts(ghosts) else [],
            "start": _iso(min([c.start for c in plan.steps if c.start] + extra or [today])),
            "end": _iso(max([c.end for c in plan.steps if c.end] + ([project.due_date] if project.due_date else [])
                            + extra + [today]))}


def _classic_ghosts(rows, gh) -> tuple:
    """Класична діаграма: prop у рядках кроків і фаз, нові кроки й фази — рядки з ghost (без номера й без
    перетягування). → (рядки, дати пропозицій для меж шкали)"""
    steps, phases, added = gh.get("steps") or {}, gh.get("phases") or {}, gh.get("added") or []
    note = "Запропоновано з наради — ✓ / ✕ у «📅 Звірити план з нарадою»"
    dates, placed = [], set()

    def ghost_row(a, phase=""):
        placed.add(a["change"])
        dates.extend(d for d in (_gd(a.get("start")), _gd(a.get("end"))) if d)
        days = ((_gd(a["end"]) - _gd(a["start"])).days + 1) if a.get("start") and a.get("end") else 0
        return {"type": "step", "ghost": True, "id": f"g{a['change']}", "phase": phase, "n": "＋", "m": a.get("m", 1),
                "title": a["title"], "kind": a.get("kind") or ProjectStep.TASK, "side": "", "who": "",
                "start": a.get("start") or a.get("end") or "", "end": a.get("end") or "", "status": "todo",
                "crit": False, "slip": 0, "preds": [], "days": days, "note": note, "label": a.get("label", "")}

    out = []
    step_ids = {r["id"] for r in rows if r["type"] == "step"}
    for i, r in enumerate(rows):
        r = dict(r)
        if r["type"] == "step" and r["id"] in steps:
            r["prop"] = steps[r["id"]]
            dates.extend(d for d in (_gd(r["prop"].get("start")), _gd(r["prop"].get("end"))) if d)
        elif r["type"] == "phase" and r["id"] in phases:
            r["prop"] = phases[r["id"]]
        out.append(r)
        if r["type"] == "step":
            out += [ghost_row(a, r["phase"]) for a in added if a.get("after") == r["id"] and a["change"] not in placed]
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        if nxt is None or nxt["type"] == "phase":              # кінець фази — її нові кроки без «після»
            phase_id = r["id"] if r["type"] == "phase" else r.get("phase")
            out += [ghost_row(a, phase_id or "") for a in added if a["change"] not in placed and not a.get("new_phase")
                    and a.get("after") not in step_ids and (a.get("phase") or "") == (phase_id or "")]
    for np in gh.get("new_phases") or []:
        out.append({"type": "phase", "ghost": True, "id": f"g{np['change']}", "n": "＋", "m": np.get("m", 1),
                    "title": (f"{np['tag']} " if np.get("tag") else "") + np["name"],
                    "start": "", "end": "", "status": "todo", "done": 0, "total": 0, "note": note})
        out += [ghost_row(a, f"g{np['change']}") for a in added if a.get("new_phase") == np["key"]
                and a["change"] not in placed]
    out += [ghost_row(a) for a in added if a["change"] not in placed]
    return out, dates


def _iso(d) -> str:
    return d.isoformat() if d else ""
