"""Малювання портфеля (projects/portfolio.py) → SVG без бібліотек, кольори — змінні тем Minerva (CSS у portfolio.html).

mini(row, rng)          — міні-шкала рядка таблиці: фази смугами, віхи ◆, сьогодні, строк клієнту (червона риска)
timeline(groups, rng)   — шкала всіх проєктів: рядок на проєкт, групи, ⛓ залежності між проєктами
relation_map(data)      — карта зв'язків: клієнти → проєкти → постачальники й виробники (і 📦 товари, яких бракує)
Тексти людини — через escape; клік: рядок / вузол — посилання (target _top) або data-* для JS сторінки.
"""
from datetime import timedelta

from django.utils.html import escape

from .models import ProjectPhase, ProjectStep

MONTHS = ["січ", "лют", "бер", "кві", "тра", "чер", "лип", "сер", "вер", "жов", "лис", "гру"]
PHASE_CLS = {ProjectPhase.DONE: "pf-done", ProjectPhase.ACTIVE: "pf-active", ProjectPhase.PLANNED: "pf-plan"}
MILE_CLS = {ProjectStep.DONE: "pf-done", ProjectStep.SKIPPED: "pf-plan"}


def _n(v) -> str:
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def _cut(text, n) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


class _Scale:
    def __init__(self, rng, x0, x1):
        self.start, self.end = rng
        self.x0, self.x1 = x0, x1
        self.ppd = (x1 - x0) / max((self.end - self.start).days, 1)

    def x(self, d):
        d = min(max(d, self.start), self.end)
        return self.x0 + (d - self.start).days * self.ppd

    def inside(self, d):
        return d is not None and self.start <= d <= self.end


def _phase_bars(row, sc, cy, h, title_prefix=""):
    out = []
    for ph in row.phases:
        if not ph["start"] or not ph["end"] or ph["end"] < sc.start or ph["start"] > sc.end:
            continue
        x1, x2 = sc.x(ph["start"]), sc.x(ph["end"] + timedelta(days=1))
        late = row.stuck and ph["current"]
        cls = PHASE_CLS.get(ph["status"], "pf-plan") + (" pf-late" if late else "") + (" pf-cur" if ph["current"] else "")
        tip = (f"{title_prefix}{ph['icon']} {ph['name']}".strip() + f" · {ph['start']:%d.%m}–{ph['end']:%d.%m}"
               + f" · кроків {ph['done']} з {ph['steps']}" + (f" · умова переходу: {ph['gate']}" if ph["gate"] else ""))
        out.append(f'<rect x="{_n(x1)}" y="{_n(cy - h / 2)}" width="{_n(max(x2 - x1 - 1, 2))}" height="{_n(h)}" '
                   f'rx="2" class="{cls}"><title>{escape(tip)}</title></rect>')
    for m in row.milestones:
        if not sc.inside(m["date"]):
            continue
        mx = sc.x(m["date"]) + sc.ppd / 2
        cls = MILE_CLS.get(m["status"], "pf-mile") + (" pf-late" if (m["slip"] or 0) > 0 and m["status"] not in
                                                      (ProjectStep.DONE, ProjectStep.SKIPPED) else "")
        r = h / 2 + 1.5
        tip = f"◆ {m['title']} · {m['date']:%d.%m}" + (f" · зсув +{m['slip']} дн." if (m["slip"] or 0) > 0 else "")
        out.append(f'<polygon points="{_n(mx)},{_n(cy - r)} {_n(mx + r)},{_n(cy)} {_n(mx)},{_n(cy + r)} '
                   f'{_n(mx - r)},{_n(cy)}" class="pf-dia {cls}"><title>{escape(tip)}</title></polygon>')
    return out


def _marks(row, sc, top, bottom, today):
    out = []
    if sc.inside(today):
        tx = sc.x(today) + sc.ppd / 2
        out.append(f'<line x1="{_n(tx)}" y1="{_n(top)}" x2="{_n(tx)}" y2="{_n(bottom)}" class="pf-today"/>')
    due = row.project.due_date
    if sc.inside(due):
        dx = sc.x(due) + sc.ppd / 2
        out.append(f'<line x1="{_n(dx)}" y1="{_n(top)}" x2="{_n(dx)}" y2="{_n(bottom)}" class="pf-due">'
                   f'<title>{escape(f"Строк клієнту {due:%d.%m.%Y}")}</title></line>')
    return out


def mini(row, rng, today, width=300, height=24) -> str:
    """Міні-шкала рядка (той самий період для всіх рядків — rng)."""
    if not row.phases and not row.project.due_date:
        return ""
    sc = _Scale(rng, 2, width - 2)
    cy = height / 2
    body = [f'<line x1="2" y1="{_n(cy)}" x2="{width - 2}" y2="{_n(cy)}" class="pf-axis"/>']
    body += _phase_bars(row, sc, cy, 9)
    body += _marks(row, sc, 1, height - 1, today)
    label = f"{row.project.code}: {row.phase_text or 'без плану'}"
    return (f'<svg class="pf-mini" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">'
            + "".join(body) + "</svg>")


def axis_head(rng, width, x0, x1, y=12) -> str:
    """Підписи місяців над міні-шкалами (той самий масштаб, що mini)."""
    sc = _Scale(rng, x0, x1)
    out = []
    d = rng[0].replace(day=1)
    while d <= rng[1]:
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        a, b = sc.x(max(d, rng[0])), sc.x(min(nxt, rng[1]))
        if b - a > 22:
            name = MONTHS[d.month - 1] + (f" {d.year}" if d.month == 1 else "")
            out.append(f'<text x="{_n((a + b) / 2)}" y="{y}" text-anchor="middle" class="pf-mon">{name}</text>')
        if d > rng[0]:
            out.append(f'<line x1="{_n(a)}" y1="0" x2="{_n(a)}" y2="18" class="pf-grid"/>')
        d = nxt
    return f'<svg class="pf-mini pf-head" viewBox="0 0 {width} 18" aria-hidden="true">' + "".join(out) + "</svg>"


# ── 🗓 шкала всіх проєктів ───────────────────────────────────────────────────

W, LEFT, RIGHT, HEAD, RH = 1100, 300, 14, 34, 26


def timeline(groups, rng, today, deps=()) -> str:
    """Рядок на проєкт (заголовок групи — окремим рядком): фази, віхи, строк клієнту, ⛓ залежності між рядками."""
    rows = []
    for title, items in groups:
        if title:
            rows.append(("group", title))
        rows += [("row", r) for r in items]
    if not rows:
        return ""
    height = HEAD + len(rows) * RH + 8
    sc = _Scale(rng, LEFT + 8, W - RIGHT)
    count = sum(1 for kind, _item in rows if kind == "row")
    out = [f'<svg class="pf-tl" viewBox="0 0 {W} {height}" role="img" aria-label="'
           f'{escape(f"Шкала проєктів: {count}")}">',
           '<defs><marker id="pfA" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" '
           'orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 z" class="pf-ah"/></marker>'
           '<marker id="pfAl" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" '
           'orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 z" class="pf-ah-l"/></marker></defs>']
    d = rng[0].replace(day=1)
    while d <= rng[1]:
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        a, b = sc.x(max(d, rng[0])), sc.x(min(nxt, rng[1]))
        if d > rng[0]:
            out.append(f'<line x1="{_n(a)}" y1="4" x2="{_n(a)}" y2="{height - 4}" class="pf-grid"/>')
        if b - a > 26:
            name = MONTHS[d.month - 1] + (f" {d.year}" if d.month == 1 or d <= rng[0] else "")
            out.append(f'<text x="{_n(a + 4)}" y="16" class="pf-mon">{name}</text>')
        d = nxt
    w = rng[0] + timedelta(days=(7 - rng[0].weekday()) % 7)
    while w <= rng[1] and sc.ppd * 7 >= 30:
        out.append(f'<text x="{_n(sc.x(w) + 2)}" y="29" class="pf-wk">{w:%d.%m}</text>')
        w += timedelta(days=7)
    out.append(f'<line x1="0" y1="{HEAD}" x2="{W}" y2="{HEAD}" class="pf-grid"/>')
    centers = {}
    for i, (kind, item) in enumerate(rows):
        top = HEAD + i * RH
        cy = top + RH / 2
        if kind == "group":
            out.append(f'<rect x="0" y="{top}" width="{W}" height="{RH}" class="pf-grp"/>'
                       f'<text x="10" y="{_n(cy + 4)}" class="pf-grp-t">{escape(_cut(item, 60))}</text>')
            continue
        r = item
        p = r.project
        centers[p.pk] = cy
        label = f"{r.dot} {p.code} · {_cut(p.name, 30)}"
        tip = f"{p.code} · {p.name} · {r.phase_text or 'без плану'} · {r.reason}"
        bars = _phase_bars(r, sc, cy, 12)
        if not r.phases:
            bars.append(f'<text x="{_n(sc.x0 + 4)}" y="{_n(cy + 4)}" class="pf-none">план не складено</text>')
        if r.forecast and sc.inside(r.forecast) and r.phases:
            fx = sc.x(r.forecast + timedelta(days=1))
            bars.append(f'<text x="{_n(fx + 4)}" y="{_n(cy + 4)}" class="pf-fc{" pf-late" if r.reason and ">" in r.reason else ""}">'
                        f'{r.forecast:%d.%m}</text>')
        out.append(f'<g class="pf-row" data-href="/admin/projects/project/{p.pk}/change/#pj-plan">'
                   f'<title>{escape(tip)}</title><rect x="0" y="{top}" width="{W}" height="{RH}" class="pf-rowbg"/>'
                   f'<text x="10" y="{_n(cy + 4)}" class="pf-name">{escape(label)}</text>'
                   + "".join(bars) + "".join(_marks(r, sc, top + 3, top + RH - 3, None)) + "</g>")
    if sc.inside(today):
        tx = sc.x(today) + sc.ppd / 2
        out.append(f'<line x1="{_n(tx)}" y1="{HEAD - 6}" x2="{_n(tx)}" y2="{height - 4}" class="pf-today"/>')
    for d in deps:
        a, b = centers.get(d["pred"].project_id), centers.get(d["succ"].project_id)
        pe = d["pred"].f_end or d["pred"].end
        ss = d["succ"].f_start or d["succ"].start
        if a is None or b is None or not pe or not ss:
            continue
        x1, x2 = sc.x(pe + timedelta(days=1)), sc.x(ss)
        mx = max(x1 + 6, min(x2 - 6, x1 + 14))
        cls, mark = ("pf-dep-l", "pfAl") if d["late"] else ("pf-dep", "pfA")
        out.append(f'<path d="M{_n(x1)},{_n(a)} H{_n(mx)} V{_n(b)} H{_n(max(x2 - 1.5, mx + 2))}" class="{cls}" '
                   f'marker-end="url(#{mark})"><title>{escape(d["pred"].title + " → " + d["succ"].title)}'
                   f'</title></path>')
    out.append("</svg>")
    return "".join(out)


# ── 🔗 карта зв'язків ─────────────────────────────────────────────────────────

MW, NODE_H, GAP = 1100, 46, 22
COL = {"c": (30, 230), "p": (420, 680), "s": (860, 1070)}


def relation_map(data) -> str:
    """Три колонки; порядок постачальників і товарів — за середнім місцем їхніх проєктів (менше перетинів)."""
    customers = sorted(data["customers"], key=lambda c: c["name"].lower())
    cpos = {c["id"]: i for i, c in enumerate(customers)}
    projects = sorted(data["projects"], key=lambda r: (cpos.get(f"c{r.project.customer_id}", 999), r.project.code))
    ppos = {f"p{r.project.pk}": i for i, r in enumerate(projects)}
    right = data["suppliers"] + data["products"]
    for node in right:
        node["pos"] = [ppos[e["a"]] for e in data["edges"] if e["b"] == node["id"] and e["a"] in ppos]
    right.sort(key=lambda n: (sum(n["pos"]) / len(n["pos"]) if n["pos"] else 999, n["name"].lower()))
    n = max(len(customers), len(projects), len(right), 1)
    height = 40 + n * (NODE_H + GAP) + 40
    ys = {}

    def place(items, key, col):
        total = len(items) * (NODE_H + GAP)
        top = 40 + (n * (NODE_H + GAP) - total) / 2
        for i, it in enumerate(items):
            ys[key(it)] = (col, top + i * (NODE_H + GAP))
    place(customers, lambda c: c["id"], "c")
    place(projects, lambda r: f"p{r.project.pk}", "p")
    place(right, lambda s: s["id"], "s")

    out = [f'<svg class="pf-map" viewBox="0 0 {MW} {_n(height)}" role="img" aria-label="'
           f'{escape(f"Карта зв’язків: {len(customers)} клієнтів, {len(projects)} проєктів, {len(right)} постачальників і товарів")}">',
           '<defs><marker id="pfM" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" '
           'orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 z" class="pf-ah-l"/></marker></defs>',
           f'<text x="{COL["c"][0]}" y="22" class="pf-th">КЛІЄНТИ</text>',
           f'<text x="{COL["p"][0]}" y="22" class="pf-th">ПРОЄКТИ</text>',
           f'<text x="{COL["s"][0]}" y="22" class="pf-th">ПОСТАЧАЛЬНИКИ, ВИРОБНИКИ, ТОВАРИ</text>']
    for e in data["edges"]:
        if e["a"] not in ys or e["b"] not in ys:
            continue
        (ca, ya), (cb, yb) = ys[e["a"]], ys[e["b"]]
        ya, yb = ya + NODE_H / 2, yb + NODE_H / 2
        if e["kind"] == "dep":
            x = COL["p"][0] + 30
            bend = COL["p"][0] - 26
            cls = "pf-m-dep" + (" pf-m-late" if e.get("late") else "")
            out.append(f'<path d="M{x},{_n(ya + NODE_H / 2 - 2)} C{bend},{_n(ya + 40)} {bend},{_n(yb - 40)} '
                       f'{x},{_n(yb - NODE_H / 2 + 2)}" class="{cls}" marker-end="url(#pfM)" data-e="{e["a"]} {e["b"]}" data-k="dep">'
                       f'<title>{escape("⛓ " + (e.get("label") or ""))}</title></path>')
            continue
        x1, x2 = COL[ca][1], COL[cb][0]
        mid = (x1 + x2) / 2
        cls = {"customer": "pf-m-edge", "supply": "pf-m-edge", "plan": "pf-m-edge pf-m-dash",
               "short": "pf-m-short", "no": "pf-m-edge pf-m-no"}.get(e["kind"], "pf-m-edge")
        tip = ", ".join(e.get("labels") or [])
        out.append(f'<path d="M{x1},{_n(ya)} C{_n(mid)},{_n(ya)} {_n(mid)},{_n(yb)} {x2},{_n(yb)}" class="{cls}" '
                   f'data-e="{e["a"]} {e["b"]}" data-k="{e["kind"]}">'
                   + (f"<title>{escape(tip)}</title>" if tip else "") + "</path>")

    def node(nid, x, y, w, title, sub, cls, url, sub_cls="pf-m-sub"):
        return (f'<g class="pf-m-node {cls}" data-n="{nid}" tabindex="0" role="button"'
                + (f' data-href="{escape(url)}"' if url else "") + ">"
                f'<title>{escape(title + (" · " + sub if sub else ""))}</title>'
                f'<rect x="{x}" y="{_n(y)}" width="{w}" height="{NODE_H}" rx="8" class="pf-m-box"/>'
                f'<text x="{x + 12}" y="{_n(y + 19)}" class="pf-m-t">{escape(_cut(title, 30))}</text>'
                f'<text x="{x + 12}" y="{_n(y + 35)}" class="{sub_cls}">{escape(_cut(sub, 36))}</text></g>')

    for c in customers:
        _col, y = ys[c["id"]]
        word = "проєкт" if c["projects"] == 1 else ("проєкти" if 2 <= c["projects"] <= 4 else "проєктів")
        out.append(node(c["id"], COL["c"][0], y, COL["c"][1] - COL["c"][0], c["name"], f"{c['projects']} {word}",
                        "pf-m-c", c["url"]))
    for r in projects:
        nid = f"p{r.project.pk}"
        _col, y = ys[nid]
        sub = (r.phase_text or "без плану") + (f" · {r.reason}" if r.reason and r.reason not in ("за планом",) else "")
        cls = "pf-m-p" + {"bad": " pf-m-bad", "warn": " pf-m-warn"}.get(r.health, "")
        out.append(node(nid, COL["p"][0], y, COL["p"][1] - COL["p"][0], f"{r.dot} {r.project.code} · {r.project.name}",
                        sub, cls, f"/admin/projects/project/{r.project.pk}/change/"))
    for s in right:
        _col, y = ys[s["id"]]
        late = s.get("late") or s["id"].startswith("g")
        out.append(node(s["id"], COL["s"][0], y, COL["s"][1] - COL["s"][0], s["name"], ", ".join(s["notes"]),
                        "pf-m-s" + (" pf-m-warn" if late else ""), s["url"],
                        "pf-m-sub pf-m-bad-t" if late else "pf-m-sub"))
    out.append("</svg>")
    return "".join(out)
