"""📊 Портфель проєктів (/admin/projects/project/portfolio/): таблиця з міні-шкалами, шкала всіх проєктів, дошка
фаз, карта зв'язків і «⚡ Впливи» (impact.py). Лише читає: колір і «потребує уваги» — кеш ProjectState (state.states
рахує застаріле), фази й кроки — кеш плану (schedule.persist: f_start / f_end / eff_status / critical / slip_days).
Ним же користуються блоки «📁 Проєкти» в картках (panels.py) і панель праворуч (side_panel.py).
Малювання — portfolio_draw.py, сторінка — ProjectAdmin.portfolio_view.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db.models import F, Q

from . import state
from .models import Project, ProjectLink, ProjectParticipant, ProjectPhase, ProjectStep, StepDependency
from .schedule import planned_end

VIEWS = ("table", "timeline", "board", "map")
GROUPS = {"customer": "клієнт", "owner": "відповідальний", "health": "стан", "none": "без групування"}
HEALTH_FILTERS = ("bad", "warn", "ok", "noplan")
HEALTH_ORDER = {"bad": 0, "warn": 1, "ok": 2, "none": 3, "closed": 4}
HEALTH_DOT = {"bad": "🔴", "warn": "🟡", "ok": "🟢", "none": "⚪", "closed": "⚪"}
CLOSED = (ProjectStep.DONE, ProjectStep.SKIPPED)
LIMIT = 200
PAST_DAYS, FUTURE_DAYS = 120, 240          # межі спільної шкали від сьогодні
JLC_NAME = "JLCPCB"


def _status(step) -> str:
    return step.eff_status or step.status


def _end(step):
    return step.f_end or step.end


def _start(step):
    return step.f_start or step.start


def customer_name(c) -> str:
    return (c.company or c.name) if c is not None else ""


@dataclass
class Row:
    project: Project
    health: str = "none"                  # bad / warn / ok / closed / none (ще не рахувався)
    reason: str = ""                      # коротко: «26.12 > 11.12», «+6 дн. від базового плану», «за планом»
    risks: int = 0
    items: list = field(default_factory=list)       # «потребує уваги» (тема чужого листа — без тексту)
    phases: list = field(default_factory=list)      # [{id, name, icon, status, start, end, gate, current, steps, done}]
    phase_index: int = 0
    phase_total: int = 0
    phase_name: str = ""
    phase_icon: str = ""
    steps: int = 0
    done: int = 0
    forecast: date | None = None
    base_end: date | None = None
    slip: int | None = None
    next: dict | None = None              # {title, end, critical, late, who}
    waiting: str = ""
    milestones: list = field(default_factory=list)  # [{date, title, status, slip}]
    in_phase_days: int | None = None
    stuck: bool = False
    risk_counts: dict = field(default_factory=dict)  # {"red": n, "yellow": n, "green": n} — відкриті й «стежимо»
    status: object = None                 # останній тижневий статус людини (ProjectUpdate)

    @property
    def dot(self):
        return HEALTH_DOT.get(self.health, "⚪")

    @property
    def has_plan(self):
        return bool(self.steps or self.phases)

    @property
    def phase_text(self):
        if not self.phase_total:
            return ""
        return f"{self.phase_index}/{self.phase_total} · {self.phase_icon} {self.phase_name}".replace("  ", " ")


def load(projects, viewer=None, today=None, refresh=True) -> list:
    """Рядки портфеля (кілька запитів на всі проєкти разом). refresh — перерахувати застарілий кеш стану."""
    today = today or date.today()
    projects = list(projects)
    ids = [p.pk for p in projects]
    if not ids:
        return []
    if refresh:
        states = state.states([p for p in projects if p.is_open])
    else:
        from .models import ProjectState
        states = {s.project_id: s for s in ProjectState.objects.filter(project_id__in=ids)}
    phases = defaultdict(list)
    for ph in ProjectPhase.objects.filter(project_id__in=ids).order_by("project_id", "order", "pk"):
        phases[ph.project_id].append(ph)
    steps = defaultdict(list)
    for s in ProjectStep.objects.filter(project_id__in=ids).select_related("assignee"):
        steps[s.project_id].append(s)
    step_ids = [s.pk for lst in steps.values() for s in lst]
    suppliers = defaultdict(set)              # крок → постачальники прив'язаних закупівель / JLC
    for row in (ProjectStep.links.through.objects.filter(projectstep_id__in=step_ids)
                .select_related("projectlink__purchase_order__supplier")):
        link = row.projectlink
        if link.purchase_order_id and link.purchase_order.supplier_id:
            suppliers[row.projectstep_id].add(link.purchase_order.supplier.name)
        elif link.jlc_order_id:
            suppliers[row.projectstep_id].add(JLC_NAME)
    external = defaultdict(list)              # крок → незавершені кроки інших проєктів, від яких він залежить
    for d in (StepDependency.objects.filter(successor_id__in=step_ids)
              .exclude(predecessor__project_id=F("successor__project_id")).select_related("predecessor__project")):
        if _status(d.predecessor) not in CLOSED:
            external[d.successor_id].append(d.predecessor)
    from . import risk_rules
    from .models import ProjectUpdate
    counts = risk_rules.counts(ids)
    statuses = {}
    for u in (ProjectUpdate.objects.filter(project_id__in=ids, source=ProjectUpdate.MANUAL)
              .order_by("project_id", "-created_at", "-pk")):
        statuses.setdefault(u.project_id, u)
    rows = [_row(p, phases[p.pk], steps[p.pk], states.get(p.pk), viewer, today, suppliers, external)
            for p in projects]
    for r in rows:
        r.risk_counts = counts.get(r.project.pk, {})
        r.status = statuses.get(r.project.pk)
    return rows


def _row(p, phases, steps, st, viewer, today, suppliers, external) -> Row:
    r = Row(project=p)
    if not p.is_open:
        r.health = "closed"
    elif st is not None and st.health:
        r.health = st.health
    if st is not None and p.is_open:
        r.items = state.items_for(st, viewer)
        r.risks = st.high + st.medium
    pos = {ph.pk: i for i, ph in enumerate(phases)}
    steps = sorted(steps, key=lambda s: (pos.get(s.phase_id, len(phases)), s.order, s.pk))
    r.steps, r.done = len(steps), sum(1 for s in steps if _status(s) in CLOSED)
    by_phase = defaultdict(list)
    for s in steps:
        by_phase[s.phase_id].append(s)
    current = None
    for i, ph in enumerate(phases):
        ss = by_phase.get(ph.pk, [])
        starts = [d for d in (_start(s) for s in ss) if d]
        ends = [d for d in (_end(s) for s in ss) if d]
        plans = [d for d in (planned_end(s) for s in ss) if d]
        item = {"id": ph.pk, "name": ph.name, "icon": ph.icon, "gate": ph.gate,
                "status": ph.eff_status or ProjectPhase.PLANNED, "current": False,
                "start": ph.f_start or (min(starts) if starts else ph.start),
                "end": ph.f_end or (max(ends) if ends else ph.end),
                "planned_end": max(plans) if plans else None,
                "steps": len(ss), "done": sum(1 for s in ss if _status(s) in CLOSED)}
        r.phases.append(item)
        if current is None and item["status"] != ProjectPhase.DONE:
            current = item
    if r.phases:
        current = current or r.phases[-1]
        current["current"] = True
        r.phase_total = len(r.phases)
        r.phase_index = r.phases.index(current) + 1
        r.phase_name, r.phase_icon = current["name"], current["icon"]
        if current["start"] and current["start"] <= today and current["status"] != ProjectPhase.DONE:
            r.in_phase_days = (today - current["start"]).days
        r.stuck = bool(current["status"] != ProjectPhase.DONE and current["planned_end"]
                       and current["planned_end"] < today)
    ends = [d for d in (_end(s) for s in steps) if d]
    r.forecast = max(ends) if ends else None
    bases = [s.baseline_end for s in steps if s.baseline_end]
    r.base_end = max(bases) if bases else None
    r.slip = (r.forecast - r.base_end).days if r.forecast and r.base_end else None
    ours = [s for s in steps if _status(s) not in CLOSED and s.side in (ProjectStep.WE, ProjectStep.COLLEAGUE)
            and s.kind == ProjectStep.TASK]
    if ours:
        s = min(ours, key=lambda x: (_end(x) or date.max, x.order, x.pk))
        plan_end = planned_end(s)
        r.next = {"title": s.title, "end": _end(s), "critical": s.critical, "late": bool(plan_end and plan_end < today),
                  "who": (s.assignee.get_full_name() or s.assignee.get_username()) if s.assignee_id else ""}
    r.waiting = _waiting(steps, today, suppliers, external)
    r.milestones = [{"date": _end(s), "title": s.title, "status": _status(s), "slip": s.slip_days}
                    for s in steps if s.kind == ProjectStep.MILESTONE and _end(s)]
    r.reason = _reason(r, p)
    return r


def _waiting(steps, today, suppliers, external) -> str:
    """На кого чекаємо: інший проєкт (залежність) → клієнт / постачальник, чий крок уже йде → ми."""
    for s in steps:                    # крок уже мав початись, але чекає крок іншого проєкту
        planned = s.start or s.baseline_start or _start(s)
        if external.get(s.pk) and _status(s) not in CLOSED and planned and planned <= today:
            return f"проєкт {external[s.pk][0].project.code}"
    active = [s for s in steps if _status(s) not in CLOSED and _start(s) and _start(s) <= today]
    theirs = [s for s in active if s.side in (ProjectStep.CUSTOMER, ProjectStep.SUPPLIER)]
    if theirs:
        s = min(theirs, key=lambda x: (_start(x), x.order))
        days = (today - _start(s)).days
        tail = f" · {days} дн." if days > 0 else ""
        if s.side == ProjectStep.SUPPLIER:
            names = sorted(suppliers.get(s.pk) or [])
            return (f"постачальник {', '.join(names)}" if names else "постачальник") + tail
        return "клієнт" + tail
    if any(s.side in (ProjectStep.WE, ProjectStep.COLLEAGUE) for s in active):
        return "ми"
    return ""


def _reason(r, p) -> str:
    if not r.has_plan:
        reason = "без плану"
    elif p.due_date and r.forecast and r.forecast > p.due_date:
        return f"{r.forecast:%d.%m} > {p.due_date:%d.%m}"
    elif r.slip and r.slip > 0:
        return f"+{r.slip} дн. від базового плану"
    elif r.stuck:
        return "фаза затягується"
    else:
        reason = "за планом"
    if r.health in ("bad", "warn") and r.items:
        text = r.items[0]["text"]
        return f"{r.items[0]['icon']} {text[:44]}{'…' if len(text) > 44 else ''}"
    return reason


# ── сторінка ─────────────────────────────────────────────────────────────────

@dataclass
class Params:
    view: str = "table"
    group: str = "customer"
    mine: bool = False
    health: str = ""
    closed: bool = False
    q: str = ""

    @classmethod
    def parse(cls, get):
        return cls(view=get.get("view") if get.get("view") in VIEWS else "table",
                   group=get.get("group") if get.get("group") in GROUPS else "customer",
                   mine=get.get("f") == "mine", health=get.get("h") if get.get("h") in HEALTH_FILTERS else "",
                   closed=get.get("closed") == "1", q=" ".join(str(get.get("q") or "").split())[:100])

    def query(self, **change) -> str:
        """Адреса з цими параметрами (зміни — поверх поточних)."""
        from urllib.parse import urlencode
        d = {"view": self.view, "group": self.group, "f": "mine" if self.mine else "", "h": self.health,
             "closed": "1" if self.closed else "", "q": self.q}
        d.update({k: v for k, v in change.items()})
        return "?" + urlencode({k: v for k, v in d.items() if v and not (k == "view" and v == "table")
                                and not (k == "group" and v == "customer")})


def mine_ids(user) -> set:
    return set(Project.objects.filter(Q(owner=user) | Q(steps__assignee=user)).values_list("pk", flat=True))


def select(params, user, today):
    """(рядки після фільтрів, лічильники, усі рядки) — проєкти до LIMIT, нові впливи — окремо."""
    qs = Project.objects.select_related("customer", "owner")
    if not params.closed:
        qs = qs.filter(status__in=Project.OPEN_STATUSES)
    if params.q:
        q = params.q
        qs = qs.filter(Q(code__icontains=q) | Q(name__icontains=q) | Q(customer__company__icontains=q)
                       | Q(customer__name__icontains=q))
    projects = list(qs.order_by(F("due_date").asc(nulls_last=True), "code")[:LIMIT])
    rows = load(projects, user, today)
    mine = mine_ids(user)
    counts = {"all": len(rows), "mine": sum(1 for r in rows if r.project.pk in mine),
              "bad": sum(1 for r in rows if r.health == "bad"), "warn": sum(1 for r in rows if r.health == "warn"),
              "ok": sum(1 for r in rows if r.health == "ok"), "noplan": sum(1 for r in rows if not r.has_plan)}
    shown = [r for r in rows if (not params.mine or r.project.pk in mine)
             and (not params.health or (r.health == params.health if params.health != "noplan" else not r.has_plan))]
    shown.sort(key=lambda r: (HEALTH_ORDER.get(r.health, 9), r.project.due_date or date.max, r.project.code))
    return shown, counts, rows


def grouped(rows, group) -> list:
    """[(заголовок, [рядки])] за клієнтом / відповідальним / станом; без групування — один блок."""
    if group == "none":
        return [("", rows)]
    out = {}
    for r in rows:
        p = r.project
        if group == "customer":
            key = customer_name(p.customer) or "Без клієнта"
        elif group == "owner":
            key = (p.owner.get_full_name() or p.owner.get_username()) if p.owner_id else "Без відповідального"
        else:
            key = {"bad": "🔴 Термінове", "warn": "🟡 Потребує уваги", "ok": "🟢 Усе гаразд",
                   "closed": "⚪ Закрито"}.get(r.health, "⚪ Ще не пораховано")
        out.setdefault(key, []).append(r)
    if group == "health":
        return list(out.items())
    return sorted(out.items(), key=lambda kv: (kv[0].startswith("Без "), kv[0].lower()))


def board(rows) -> list:
    """Дошка фаз: колонки — поточна фаза (та сама назва — одна колонка, порядок — середнє місце фази в плані),
    спершу «Без плану», в кінці «Усі фази завершено»."""
    cols, noplan, finished = {}, [], []
    for r in rows:
        if not r.phases:
            noplan.append(r)
            continue
        if all(ph["status"] == ProjectPhase.DONE for ph in r.phases):
            finished.append(r)
            continue
        c = cols.setdefault(r.phase_name.strip().lower(), {"name": r.phase_name, "icon": r.phase_icon, "pos": [],
                                                           "rows": []})
        c["pos"].append((r.phase_index - 1) / max(r.phase_total - 1, 1))
        c["rows"].append(r)
    out = [{"name": "Без плану", "icon": "📭", "rows": noplan}] if noplan else []
    out += sorted(cols.values(), key=lambda c: (sum(c["pos"]) / len(c["pos"]), c["name"].lower()))
    if finished:
        out.append({"name": "Усі фази завершено", "icon": "✅", "rows": finished})
    for c in out:
        c["rows"].sort(key=lambda r: (not r.stuck, HEALTH_ORDER.get(r.health, 9), -(r.in_phase_days or 0)))
    return out


def span(rows, today) -> tuple:
    """Спільна шкала: від найранішої фази до найпізнішого строку / прогнозу, не далі PAST_DAYS / FUTURE_DAYS."""
    dates = [today - timedelta(days=14), today + timedelta(days=30)]
    for r in rows:
        for ph in r.phases:
            dates += [d for d in (ph["start"], ph["end"]) if d]
        dates += [d for d in (r.forecast, r.project.due_date) if d]
    start = max(min(dates), today - timedelta(days=PAST_DAYS))
    end = min(max(dates), today + timedelta(days=FUTURE_DAYS))
    return start - timedelta(days=2), end + timedelta(days=4)


def cross_deps(rows) -> list:
    """Залежності між проєктами серед показаних: [{pred, succ, late}] (кеш дат плану)."""
    ids = [r.project.pk for r in rows]
    out = []
    for d in (StepDependency.objects.filter(successor__project_id__in=ids, predecessor__project_id__in=ids)
              .exclude(predecessor__project_id=F("successor__project_id"))
              .select_related("predecessor", "successor")):
        p, s = d.predecessor, d.successor
        ready = _end(p) + timedelta(days=1 + d.lag_days) if _end(p) else None
        planned = s.start or s.baseline_start
        out.append({"pred": p, "succ": s, "late": bool(ready and planned and ready > planned
                                                         and _status(p) not in CLOSED)})
    return out


# ── 🔗 карта зв'язків ─────────────────────────────────────────────────────────

def relations(rows, impacts) -> dict:
    """Вузли й зв'язки карти: клієнти → проєкти → постачальники й виробники (закупівлі, JLC, учасники-
    постачальники), ⛓ залежності між проєктами, 📦 спільні товари, яких бракує."""
    from inventory.models import Supplier
    projects = {r.project.pk: r for r in rows}
    customers, suppliers, edges = {}, {}, []
    for r in rows:
        c = r.project.customer
        if c is not None:
            customers.setdefault(c.pk, {"id": f"c{c.pk}", "name": customer_name(c), "projects": 0,
                                        "url": f"/admin/crm/customer/{c.pk}/change/"})
            customers[c.pk]["projects"] += 1
            edges.append({"a": f"c{c.pk}", "b": f"p{r.project.pk}", "kind": "customer"})

    def supplier_node(key, name, url=""):
        return suppliers.setdefault(key, {"id": key, "name": name, "url": url, "pos": [], "notes": []})

    seen = set()
    info = {}

    def note(nid, project_id, text, url=""):
        """Рядок картки вузла (клік на карті): «P-2026-001 · 🛒 PO-12 · 1500 шт AN… · замовлено»."""
        p = projects.get(project_id)
        info.setdefault(nid, {"lines": []})["lines"].append(
            {"p": p.project.code if p else "", "t": text, "u": url})

    for link in (ProjectLink.objects.filter(project_id__in=projects, status=ProjectLink.CONFIRMED)
                 .filter(Q(purchase_order__isnull=False) | Q(jlc_order__isnull=False))
                 .select_related("purchase_order__supplier", "jlc_order")
                 .prefetch_related("purchase_order__lines__product")):
        if link.purchase_order_id and link.purchase_order.supplier_id:
            sup = link.purchase_order.supplier
            node = supplier_node(f"s{sup.pk}", sup.name, f"/admin/inventory/supplier/{sup.pk}/change/")
            po = link.purchase_order
            planned = po.status not in ("ordered", "partial", "received")
            label = po_label(po)
            url = f"/admin/inventory/purchaseorder/{po.pk}/change/"
            short = label.split(" · ", 1)[-1] if " · " in label else label
            if f"🛒 {short}"[:60] not in node["notes"]:
                node["notes"].append(f"🛒 {short}"[:60])
        elif link.jlc_order_id:
            node = supplier_node("jlc", JLC_NAME, "/admin/jlcpcb/jlcorder/")
            planned = False
            j = link.jlc_order
            label = f"🔧 JLC {j.jlc_order_number or j.jlc_order_id} · {j.get_local_status_display()}"
            url = f"/admin/jlcpcb/jlcorder/{j.pk}/change/"
        else:
            continue
        note(node["id"], link.project_id, label, url)
        key = (link.project_id, node["id"])
        if key in seen:
            next(e for e in edges if e.get("key") == key)["labels"].append(label)
            continue
        seen.add(key)
        edges.append({"a": f"p{link.project_id}", "b": node["id"], "kind": "plan" if planned else "supply",
                      "key": key, "labels": [label]})
    by_email = {s.email.lower(): s for s in Supplier.objects.exclude(email="")}
    for part in ProjectParticipant.objects.filter(project_id__in=projects, role=ProjectParticipant.SUPPLIER):
        sup = by_email.get((part.email or "").lower())
        if sup is None:
            continue
        node = supplier_node(f"s{sup.pk}", sup.name, f"/admin/inventory/supplier/{sup.pk}/change/")
        key = (part.project_id, node["id"])
        if key not in seen:
            seen.add(key)
            edges.append({"a": f"p{part.project_id}", "b": node["id"], "kind": "plan", "key": key, "labels": []})
    from . import sourcing                     # 📨 запити постачальникам: обрано — суцільна, запит — пунктир, ні — бліда
    for req in (sourcing.requests_for(list(projects)) if sourcing.available() else []):
        for r in req.recipients.all():
            o = sourcing.outcome(r)
            node = supplier_node(f"s{r.supplier_id}", r.supplier.name, f"/admin/inventory/supplier/{r.supplier_id}/change/")
            kind = "supply" if o == "chosen" else ("no" if o in sourcing.OFF else "plan")
            sub_ = sourcing.short(r, req)
            if sub_ not in node["notes"]:
                node["notes"].append(sub_)
            key = (req.project_id, node["id"])
            label = sourcing.line(r, req)
            note(node["id"], req.project_id, label, f"/admin/suppliers/supplierrequest/{req.pk}/work/")
            found = next((e for e in edges if e.get("key") == key), None)
            if found is not None:
                found["labels"].append(label)
                if kind == "supply":
                    found["kind"] = "supply"
                elif found["kind"] == "no" and kind == "plan":
                    found["kind"] = "plan"
                continue
            seen.add(key)
            edges.append({"a": f"p{req.project_id}", "b": node["id"], "kind": kind, "key": key, "labels": [label]})
    products = {}
    for i in impacts:
        if i.kind == "stock":
            pr = i.data["product"]
            node = products.setdefault(pr.pk, {"id": f"g{pr.pk}", "name": f"📦 {pr.sku}", "pos": [],
                                               "url": f"/admin/inventory/product/{pr.pk}/change/",
                                               "notes": [f"бракує {i.data['short']:.0f}"]})
            for p in i.projects:
                if p.pk in projects:
                    edges.append({"a": f"p{p.pk}", "b": node["id"], "kind": "short"})
        elif i.kind == "supplier" and i.data.get("late"):
            node = suppliers.get(f"s{i.data['supplier'].pk}")
            if node is not None:
                node["notes"].append("прострочена закупівля")
                node["late"] = True
        elif i.kind == "dep" and i.data["pred"].project_id in projects and i.data["succ"].project_id in projects:
            edges.append({"a": f"p{i.data['pred'].project_id}", "b": f"p{i.data['succ'].project_id}",
                          "kind": "dep", "late": i.data["late"] > 0,
                          "label": f"{i.data['pred'].title} → {i.data['succ'].title}"})
    for r in rows:                                     # картка проєкту
        p = r.project
        lines = [{"t": f"{r.dot} {r.reason or 'за планом'}", "u": ""},
                 {"t": f"Фаза: {r.phase_text or 'без плану'}", "u": ""}]
        if p.customer_id:
            lines.append({"t": f"Клієнт: {customer_name(p.customer)}", "u": f"/admin/crm/customer/{p.customer_id}/change/"})
        if r.next:
            lines.append({"t": f"Далі: {r.next.get('title', '')}" + (f" · {r.next['end']:%d.%m}" if r.next.get("end") else ""),
                          "u": ""})
        if r.waiting:
            lines.append({"t": f"Чекаємо: {r.waiting}", "u": ""})
        lines.append({"t": "🔗 Сторони й запити постачальникам", "u": f"/admin/projects/project/{p.pk}/change/#pj-parties"})
        info[f"p{p.pk}"] = {"lines": lines}
    for c in customers.values():                       # картка клієнта — його проєкти
        info[c["id"]] = {"lines": [{"p": "", "t": f"{r.project.code} · {r.project.name}",
                                    "u": f"/admin/projects/project/{r.project.pk}/change/"}
                                   for r in rows if r.project.customer_id and f"c{r.project.customer_id}" == c["id"]]}
    names = {**{c["id"]: c["name"] for c in customers.values()}, **{s["id"]: s["name"] for s in suppliers.values()},
             **{f"p{r.project.pk}": f"{r.project.code} · {r.project.name}" for r in rows},
             **{g["id"]: g["name"] for g in products.values()}}
    urls = {**{c["id"]: c["url"] for c in customers.values()}, **{s["id"]: s["url"] for s in suppliers.values()},
            **{f"p{r.project.pk}": f"/admin/projects/project/{r.project.pk}/change/" for r in rows},
            **{g["id"]: g["url"] for g in products.values()}}
    for nid in names:
        it = info.setdefault(nid, {"lines": []})
        it["title"], it["url"] = names[nid], urls.get(nid, "")
    for g in products.values():
        info[g["id"]]["lines"] += [{"p": "", "t": n, "u": ""} for n in g["notes"]]
    return {"customers": list(customers.values()), "projects": rows, "suppliers": list(suppliers.values()),
            "products": list(products.values()), "edges": edges, "info": info}


def po_label(po) -> str:
    """«🛒 PO-12 · 1500 шт AN220213 (+2) · замовлено» — що саме замовили."""
    lines = list(po.lines.all())
    what = ""
    if lines:
        first = lines[0]
        name = first.product.sku if first.product_id else (first.description or "")
        qty = first.qty_ordered
        qty_t = f"{int(qty)}" if qty == int(qty) else f"{qty}"
        what = f"{qty_t} шт {name}".strip() + (f" (+{len(lines) - 1})" if len(lines) > 1 else "")
    status = {"draft": "чернетка", "ordered": "замовлено", "partial": "частково отримано", "received": "отримано",
              "cancelled": "скасовано"}.get(po.status, po.status)
    return " · ".join(x for x in (f"🛒 {po}", what, status) if x)
