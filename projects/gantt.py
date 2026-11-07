"""Гант плану проєкту (вкладка «📅 План» пульта) → SVG без бібліотек (кольори — змінні тем Minerva).

Рядки: фаза (підсумкова смуга, базовий план тонкою лінією) і її кроки: смуга кроку чи ◆ віха, тонка смуга
базового плану під нею, червоний контур — критичний шлях, «+N» — зсув від базового плану, стрілки — залежності
«фініш → старт». Лінії: сьогодні, строк клієнту. editable: рядки фокусуються (tabindex) — клік / Enter відкриває
вікно кроку чи фази (data-step / data-phase, JS — change_form.html); підказка рядка — <title>.
"""
from datetime import timedelta

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


def _fmt(d) -> str:
    return d.strftime("%d.%m") if d else ""


def render(project, plan, today=None, editable=False) -> str:
    today = today or timezone.localdate()
    if not plan.has_plan:
        return ""
    dates = [today]
    for c in plan.steps:
        dates += [d for d in (c.start, c.end, c.step.baseline_start, c.step.baseline_end) if d]
    for pc in plan.phases:
        dates += [d for d in (pc.start, pc.end) if d]
    if project.due_date:
        dates.append(project.due_date)
    start, end = min(dates) - timedelta(days=2), max(dates) + timedelta(days=4)
    if (end - start).days < 21:
        end = start + timedelta(days=21)
    days = (end - start).days
    ppd = (W - RIGHT - X0) / days

    def x(d):
        return X0 + (d - start).days * ppd

    rows = []                      # ("phase", PhaseCalc) | ("step", StepCalc)
    for pc in plan.phases:
        rows.append(("phase", pc))
        rows += [("step", c) for c in pc.steps]
    height = HEAD + len(rows) * RH + 8
    top_of = {}
    focus = ' tabindex="0" role="button"' if editable else ""
    out = [f'<svg class="pjg" viewBox="0 0 {W} {height}" role="group" aria-label="'
           f'{escape(f"План проєкту {project.code}: {len(plan.steps)} кроків, прогноз до {_fmt(plan.end)}")}">',
           '<defs><marker id="pjgA" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" '
           'orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 z" class="pjg-ah"/></marker>'
           '<marker id="pjgAc" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" '
           'orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 z" class="pjg-ah-c"/></marker></defs>']

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
            if x(min(nxt, end)) - x(d) > 34:
                label = MONTHS[d.month - 1] + (f" {d.year}" if d.month == 1 or d == start else "")
                out.append(f'<text x="{_n(x(d) + 4)}" y="13" class="pjg-mont">{label}</text>')
        d += timedelta(days=1)
    out.append(f'<line x1="0" y1="{HEAD}" x2="{W}" y2="{HEAD}" class="pjg-mon"/>')
    out.append(f'<line x1="{LEFT}" y1="0" x2="{LEFT}" y2="{height - 8}" class="pjg-mon"/>')
    for label, lx in (("КРОК", 10), ("ХТО", 222), ("ПРОГНОЗ", 268)):
        out.append(f'<text x="{lx}" y="{HEAD - 6}" class="pjg-th">{label}</text>')

    # Рядки
    for i, (kind, item) in enumerate(rows):
        top = HEAD + i * RH
        cy = top + RH / 2
        if kind == "phase":
            pc = item
            ph = pc.phase
            name = f"{ph.icon} {ph.name}".strip() if ph else "Без фази"
            tip = name + (f" · умова переходу: {ph.gate}" if ph and ph.gate else "") + \
                f" · {pc.done} з {len(pc.steps)} кроків" + (f" · {_fmt(pc.start)}–{_fmt(pc.end)}" if pc.start else "")
            attr = f' data-phase="{ph.pk}"{focus}' if ph else ""
            g = [f'<g class="pjg-row pjg-phase pjg-{pc.status}"{attr}><title>{escape(tip)}</title>',
                 f'<rect x="0" y="{top}" width="{W}" height="{RH}" class="pjg-rowbg pjg-phbg"/>',
                 f'<text x="10" y="{_n(cy + 4)}" class="pjg-pname">{escape(_cut(name, 40))}</text>',
                 f'<text x="268" y="{_n(cy + 4)}" class="pjg-dates">{_fmt(pc.start)}–{_fmt(pc.end)}</text>']
            if pc.start and pc.end:
                g.append(f'<rect x="{_n(x(pc.start))}" y="{_n(cy - 4)}" width="{_n(max(x(pc.end + timedelta(days=1)) - x(pc.start), 2))}" '
                         f'height="8" rx="2" class="pjg-sum"/>')
            if pc.base_start and pc.base_end:
                g.append(f'<rect x="{_n(x(pc.base_start))}" y="{_n(cy + 6)}" width="{_n(max(x(pc.base_end + timedelta(days=1)) - x(pc.base_start), 2))}" '
                         f'height="2.5" class="pjg-base"/>')
            g.append("</g>")
            out.append("".join(g))
            continue
        c = item
        s = c.step
        top_of[s.pk] = (top, c)
        who = (s.assignee.get_full_name() or s.assignee.username) \
            if s.assignee_id and s.side in (ProjectStep.WE, ProjectStep.COLLEAGUE) else SIDE_SHORT.get(s.side, "")
        tip = (f"{STATUS_ICON.get(c.status, '')} {s.title} · {s.get_side_display()}"
               + (f" · {who}" if s.assignee_id else "") + f" · прогноз {_fmt(c.start)}–{_fmt(c.end)}"
               + (f" · план {_fmt(s.baseline_start)}–{_fmt(s.baseline_end)}" if s.baseline_end else "")
               + (f" · зсув {c.slip:+d} дн." if c.slip else "") + (" · критичний шлях" if c.critical else "")
               + (f"\n{c.note}" if c.note else ""))
        cls = f"pjg-{c.status or 'todo'}" + (" pjg-crit" if c.critical and c.status not in ("done", "skipped") else "")
        g = [f'<g class="pjg-row pjg-step {cls}" data-step="{s.pk}"{focus}><title>{escape(tip)}</title>',
             f'<rect x="0" y="{top}" width="{W}" height="{RH}" class="pjg-rowbg"/>',
             f'<text x="22" y="{_n(cy + 4)}" class="pjg-t">{STATUS_ICON.get(c.status, "")} {escape(_cut(s.title))}</text>',
             f'<text x="222" y="{_n(cy + 4)}" class="pjg-who pjg-side-{s.side}">{escape(_cut(who, 9))}</text>',
             f'<text x="268" y="{_n(cy + 4)}" class="pjg-dates">'
             f'{_fmt(c.start) if s.kind != ProjectStep.MILESTONE else ""}{"–" if s.kind != ProjectStep.MILESTONE else "◆ "}{_fmt(c.end)}</text>']
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
        g.append("</g>")
        out.append("".join(g))

    # Залежності (у межах проєкту)
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

    if start <= today <= end:
        tx = x(today) + ppd / 2
        out.append(f'<line x1="{_n(tx)}" y1="{HEAD - 16}" x2="{_n(tx)}" y2="{height - 8}" class="pjg-today"/>')
    if project.due_date and start <= project.due_date <= end:
        dx = x(project.due_date) + ppd / 2
        out.append(f'<line x1="{_n(dx)}" y1="{HEAD - 16}" x2="{_n(dx)}" y2="{height - 8}" class="pjg-due">'
                   f'<title>Строк клієнту {project.due_date:%d.%m.%Y}</title></line>')
    out.append("</svg>")
    return "".join(out)
