"""План проєкту: прогноз дат, критичний шлях, зсув від базового плану, стан кроків з прив'язаних об'єктів — без AI.

compute(project, facts, today) рахує план; єдина зміна в базі — автоприв'язка: крок з «Виконано, коли» без
прив'язаних об'єктів отримує найраніший підхожий об'єкт проєкту (Angebot, замовлення, закупівлю, JLC-замовлення,
відправлення), не прив'язаний до іншого кроку з тією самою умовою і не старший за план кроку більш ніж на
BIND_EARLY_DAYS. persist(plan) записує кеш у кроки й фази (f_start / f_end / eff_status / critical / slip_days /
auto_note) — лише змінене.

Дні — календарні. Залежність: фініш попереднього → старт наступного + відступ; попередній крок з іншого проєкту —
його збережений прогноз. Невиконаний крок не закінчується раніше за «сьогодні» й за очікувану дату прив'язаних
об'єктів (закупівля, JLC, посилка). Рішення людини («Виконано», «Пропущено») автоматика не змінює.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from django.db.models import F
from django.utils import timezone

from .models import ProjectLink, ProjectPhase, ProjectStep, StepDependency

BIND_EARLY_DAYS = 14
TRIGGER_TYPES = {
    "offer_sent": "offer", "offer_accepted": "offer",
    "order_received": "order", "order_shipped": "order", "order_delivered": "order",
    "po_ordered": "po", "po_received": "po",
    "jlc_ordered": "jlc", "jlc_delivered": "jlc",
    "shipment_sent": "shipment", "shipment_delivered": "shipment",
}
LINK_FIELD = {"order": "order", "po": "purchase_order", "jlc": "jlc_order", "shipment": "shipment"}
CLOSED = (ProjectStep.DONE, ProjectStep.SKIPPED)


def _d(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    return value


def _fmt(value) -> str:
    d = _d(value)
    return d.strftime("%d.%m") if d else ""


@dataclass
class StepCalc:
    step: ProjectStep
    status: str = ""
    start: date | None = None
    end: date | None = None
    expected: date | None = None
    started: date | None = None
    finished: date | None = None
    note: str = ""
    critical: bool = False
    slip: int | None = None
    preds: list = field(default_factory=list)      # [(StepCalc | ProjectStep, lag)]


@dataclass
class PhaseCalc:
    phase: ProjectPhase | None
    steps: list = field(default_factory=list)
    start: date | None = None
    end: date | None = None
    base_start: date | None = None
    base_end: date | None = None
    status: str = ProjectPhase.PLANNED
    done: int = 0

    @property
    def gate_step(self):
        miles = [c for c in self.steps if c.step.kind == ProjectStep.MILESTONE]
        return miles[-1] if miles else None

    @property
    def slip(self):
        return (self.end - self.base_end).days if self.end and self.base_end else None


@dataclass
class Plan:
    phases: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    by_id: dict = field(default_factory=dict)
    end: date | None = None
    base_end: date | None = None
    today: date | None = None

    @property
    def has_plan(self) -> bool:
        return bool(self.steps or self.phases)

    @property
    def current(self):
        """Поточна фаза: перша незавершена."""
        return next((p for p in self.phases if p.status != ProjectPhase.DONE and (p.steps or p.phase)), None)


# ── Об'єкти проєкту й умови «Виконано, коли» ─────────────────────────────────

def _objects(facts) -> dict:
    """{тип: [(дата, об'єкт)]} — від найранішого."""
    from shipping.models import Shipment
    out = {
        "offer": [(_d(o.sent_at) or _d(o.created_at), o) for o in facts.get("offers", [])],
        "order": [(o.order_date or _d(o.shipped_at), o) for o in facts.get("orders", [])
                  if o.status != "cancelled"],
        "po": [(p.order_date, p) for p in facts.get("purchases", []) if p.status != p.Status.CANCELLED],
        "jlc": [(j.order_date, j) for j in facts.get("jlc", []) if j.local_status != j.LocalStatus.CANCELLED],
        "shipment": [(_d(s.submitted_at) or _d(s.created_at), s) for s in facts.get("shipments", [])
                     if s.direction == Shipment.OUT],
    }
    for kind in out:
        out[kind].sort(key=lambda x: (x[0] or date.max, x[1].pk))
    return out


def _bound(step, kind, offers_by_step, links_by_step) -> list:
    if kind == "offer":
        return offers_by_step.get(step.pk, [])
    field_name = LINK_FIELD[kind]
    return [getattr(l, field_name) for l in links_by_step.get(step.pk, []) if getattr(l, f"{field_name}_id")]


def _ship_eta(order):
    etas = [s.eta_to or s.carrier_eta for s in order.shipments.all() if (s.eta_to or s.carrier_eta)]
    return max(etas) if etas else None


def _eval(trigger, obj):
    """(виконано — дата | None, почато — дата | None, очікується — дата | None, примітка)."""
    if trigger == "offer_sent":
        if obj.sent_at:
            return _d(obj.sent_at), _d(obj.created_at), None, f"Angebot {obj.display_number} надіслано {_fmt(obj.sent_at)}"
        return None, _d(obj.created_at), None, f"Angebot {obj.display_number} — {obj.get_status_display()}"
    if trigger == "offer_accepted":
        if obj.status == obj.ACCEPTED:
            return (_d(obj.decided_at) or _d(obj.sent_at) or _d(obj.created_at), _d(obj.sent_at), None,
                    f"Angebot {obj.display_number} прийнято")
        return None, _d(obj.sent_at), obj.valid_until if obj.status == obj.SENT else None, \
            f"Angebot {obj.display_number} — {obj.get_status_display()}"
    if trigger == "order_received":
        return obj.order_date or timezone.localdate(), obj.order_date, None, f"замовлення {obj.source}:{obj.order_number}"
    if trigger == "order_shipped":
        if obj.status in ("shipped", "delivered"):
            return (_d(obj.shipped_at) or _d(obj.delivered_at) or obj.order_date, obj.order_date, None,
                    f"замовлення {obj.order_number} відправлено")
        return None, obj.order_date, obj.shipping_deadline, f"замовлення {obj.order_number} — {obj.get_status_display()}"
    if trigger == "order_delivered":
        if obj.status == "delivered":
            return (_d(obj.delivered_at) or _d(obj.shipped_at) or obj.order_date, _d(obj.shipped_at), None,
                    f"замовлення {obj.order_number} доставлено")
        return None, _d(obj.shipped_at), _ship_eta(obj) or obj.shipping_deadline, \
            f"замовлення {obj.order_number} — {obj.get_status_display()}"
    if trigger == "po_ordered":
        if obj.status in (obj.Status.ORDERED, obj.Status.PARTIAL, obj.Status.RECEIVED):
            return obj.order_date, obj.order_date, None, f"{obj} замовлено"
        return None, obj.order_date, None, f"{obj} — чернетка"
    if trigger == "po_received":
        if obj.status == obj.Status.RECEIVED:
            return obj.received_date or timezone.localdate(), obj.order_date, None, f"{obj} отримано"
        return None, obj.order_date, obj.expected_date, f"{obj} — {obj.get_status_display()}" + \
            (f", очікується {_fmt(obj.expected_date)}" if obj.expected_date else "")
    if trigger == "jlc_ordered":
        return obj.order_date, obj.order_date, None, f"JLC {obj.jlc_order_number or obj.jlc_order_id} замовлено"
    if trigger == "jlc_delivered":
        name = f"JLC {obj.jlc_order_number or obj.jlc_order_id}"
        if obj.local_status == obj.LocalStatus.DELIVERED:
            return obj.delivered_date or timezone.localdate(), obj.order_date, None, f"{name} доставлено"
        return None, obj.order_date, obj.expected_date, f"{name} — {obj.get_local_status_display()}"
    if trigger in ("shipment_sent", "shipment_delivered"):
        name = obj.tracking_number or f"#{obj.pk}"
        sent = obj.status not in ("draft", "error", "cancelled")
        if trigger == "shipment_sent":
            if sent:
                return _d(obj.submitted_at) or _d(obj.created_at), _d(obj.created_at), None, f"відправлення {name} передано"
            return None, _d(obj.created_at), None, f"відправлення {name} — {obj.get_status_display()}"
        if obj.status == "delivered":
            return _d(obj.delivered_at) or timezone.localdate(), _d(obj.submitted_at), None, f"відправлення {name} доставлено"
        return None, _d(obj.submitted_at) if sent else None, obj.eta_to or obj.carrier_eta, \
            f"відправлення {name} — {obj.get_status_display()}"
    return None, None, None, ""


def _evidence(trigger, objs):
    """Стан кроку з усіх прив'язаних об'єктів: усі виконали умову — виконано (найпізніша дата), інакше — в роботі."""
    if not objs:
        return None
    results = [_eval(trigger, o) for o in objs]
    starts = [r[1] for r in results if r[1]]
    notes = "; ".join(r[3] for r in results if r[3])[:255]
    if all(r[0] for r in results):
        return ProjectStep.DONE, min(starts) if starts else None, max(r[0] for r in results), None, notes
    expected = [r[2] for r in results if not r[0] and r[2]]
    return ProjectStep.DOING, min(starts) if starts else None, None, max(expected) if expected else None, notes


def _autobind(steps, objects, offers_by_step, links_by_step, project):
    """Крок з умовою без прив'язаних об'єктів ← найраніший підхожий об'єкт (див. опис модуля)."""
    used = defaultdict(set)
    for s in steps:
        kind = TRIGGER_TYPES.get(s.trigger)
        if kind:
            used[s.trigger] |= {o.pk for o in _bound(s, kind, offers_by_step, links_by_step)}
    for s in steps:
        kind = TRIGGER_TYPES.get(s.trigger)
        if not kind or not s.auto or s.status in CLOSED or _bound(s, kind, offers_by_step, links_by_step):
            continue
        earliest = (s.start or s.end) and (s.start or s.end) - timedelta(days=BIND_EARLY_DAYS)
        pick = next((o for when, o in objects.get(kind, [])
                     if o.pk not in used[s.trigger] and (earliest is None or (when and when >= earliest))), None)
        if pick is None:
            continue
        used[s.trigger].add(pick.pk)
        if kind == "offer":
            s.offers.add(pick)
            offers_by_step[s.pk] = [pick]
        else:
            link, _ = ProjectLink.objects.get_or_create(
                project=project, **{LINK_FIELD[kind]: pick},
                defaults={"source": ProjectLink.RULE, "status": ProjectLink.CONFIRMED,
                          "reason": f"крок плану «{s.title}"[:250] + "»"})
            if link.status != ProjectLink.CONFIRMED:      # відхилене людиною не прив'язуємо
                used[s.trigger].discard(pick.pk)
                continue
            s.links.add(link)
            links_by_step[s.pk] = links_by_step.get(s.pk, []) + [link]


# ── Розрахунок ───────────────────────────────────────────────────────────────

def _topo(calcs, succ):
    """Порядок кроків за залежностями (Кан); кроки в циклі — у кінці, без циклічних ребер."""
    indeg = {id(c): 0 for c in calcs}
    for c in calcs:
        for s, _lag in succ.get(id(c), []):
            indeg[id(s)] += 1
    ready = [c for c in calcs if indeg[id(c)] == 0]
    out, seen = [], set()
    while ready:
        c = ready.pop(0)
        out.append(c)
        seen.add(id(c))
        for s, _lag in succ.get(id(c), []):
            indeg[id(s)] -= 1
            if indeg[id(s)] == 0:
                ready.append(s)
    return out + [c for c in calcs if id(c) not in seen]


def _duration(step) -> int:
    if step.kind == ProjectStep.MILESTONE:
        return 0
    if step.start and step.end:
        return max((step.end - step.start).days + 1, 1)
    return max(step.duration_days or 1, 1)


def planned_end(step):
    """Плановий кінець кроку: «Кінець», інакше «Початок» + тривалість; без планових дат — None."""
    if step.end:
        return step.end
    if step.start:
        return step.start + timedelta(days=max(_duration(step) - 1, 0))
    return None


def compute(project, facts=None, today=None) -> Plan:
    today = today or timezone.localdate()
    plan = Plan(today=today)
    phases = list(ProjectPhase.objects.filter(project=project).order_by("order", "pk"))
    steps = list(ProjectStep.objects.filter(project=project).select_related("assignee", "phase")
                 .order_by("phase__order", "order", "pk"))
    phase_pos = {p.pk: i for i, p in enumerate(phases)}
    steps.sort(key=lambda s: (phase_pos.get(s.phase_id, len(phases)), s.order, s.pk))
    if not steps and not phases:
        return plan

    ids = [s.pk for s in steps]
    offers_by_step, links_by_step = defaultdict(list), defaultdict(list)
    for through in ProjectStep.offers.through.objects.filter(projectstep_id__in=ids).select_related("offer"):
        offers_by_step[through.projectstep_id].append(through.offer)
    for through in (ProjectStep.links.through.objects.filter(projectstep_id__in=ids)
                    .select_related("projectlink__order", "projectlink__purchase_order",
                                    "projectlink__jlc_order", "projectlink__shipment")):
        if through.projectlink.status == ProjectLink.CONFIRMED:
            links_by_step[through.projectstep_id].append(through.projectlink)
    if facts is not None:
        _autobind(steps, _objects(facts), offers_by_step, links_by_step, project)

    calcs = [StepCalc(step=s) for s in steps]
    plan.steps = calcs
    plan.by_id = {c.step.pk: c for c in calcs}
    succ = defaultdict(list)
    for dep in StepDependency.objects.filter(successor_id__in=ids).select_related("predecessor__project"):
        target = plan.by_id[dep.successor_id]
        pred = plan.by_id.get(dep.predecessor_id)
        if pred is not None:
            target.preds.append((pred, dep.lag_days))
            succ[id(pred)].append((target, dep.lag_days))
        else:                                   # крок іншого проєкту — його збережений прогноз
            target.preds.append((dep.predecessor, dep.lag_days))

    # Стан кроку: рішення людини → докази з прив'язаних об'єктів → як записано
    for c in calcs:
        s = c.step
        c.status, c.started, c.finished = s.status, s.started_on, s.done_on
        kind = TRIGGER_TYPES.get(s.trigger)
        if s.status not in CLOSED and s.auto and kind:
            ev = _evidence(s.trigger, _bound(s, kind, offers_by_step, links_by_step))
            if ev:
                status, started, finished, expected, note = ev
                c.status = status if not (status == ProjectStep.DOING and s.status == ProjectStep.WAITING) else s.status
                c.started = c.started or started
                c.finished = finished if status == ProjectStep.DONE else None
                c.expected, c.note = expected, note

    # Прямий прохід: ранні дати
    for c in _topo(calcs, succ):
        s = c.step
        dur = _duration(s)
        after = None
        for pred, lag in c.preds:
            p_end = pred.end if isinstance(pred, StepCalc) else (pred.f_end or pred.end)
            if p_end:
                cand = p_end + timedelta(days=1 + lag)
                after = cand if after is None or cand > after else after
        if c.status == ProjectStep.DONE:
            c.end = c.finished or s.end or today
            c.start = c.started or s.start or (c.end - timedelta(days=max(dur - 1, 0)))
            c.start = min(c.start, c.end)
            continue
        if c.status == ProjectStep.SKIPPED:
            c.start = c.end = after or s.start or s.end or today
            continue
        start = max([d for d in (s.start, after) if d] or [today])
        if c.started and c.status in (ProjectStep.DOING, ProjectStep.WAITING):
            start = c.started
        end = start + timedelta(days=max(dur - 1, 0))
        if s.kind == ProjectStep.MILESTONE and s.end and s.end > end:
            end = start = max(s.end, start)
        if c.expected and c.expected > end:
            end = c.expected
        if end < today:                          # ще не виконано — не раніше сьогодні
            end = today
            if s.kind == ProjectStep.MILESTONE:
                start = today
        c.start, c.end = min(start, end), end

    ends = [c.end for c in calcs if c.end]
    plan.end = max(ends) if ends else None
    bases = [c.step.baseline_end for c in calcs if c.step.baseline_end]
    plan.base_end = max(bases) if bases else None

    # Зворотний прохід: запас і критичний шлях (лише незавершене)
    latest = {}
    for c in reversed(_topo(calcs, succ)):
        if c.status in CLOSED or not c.end:
            continue
        nexts = [(s, lag) for s, lag in succ.get(id(c), []) if s.status not in CLOSED and id(s) in latest]
        lf = min((latest[id(s)] - timedelta(days=1 + lag) for s, lag in nexts), default=plan.end)
        ls = lf - timedelta(days=max(_duration(c.step) - 1, 0))
        latest[id(c)] = ls
        c.critical = (ls - c.start).days <= 0
    for c in calcs:
        if c.step.baseline_end and c.end:
            c.slip = (c.end - c.step.baseline_end).days

    # Фази
    by_phase = defaultdict(list)
    for c in calcs:
        by_phase[c.step.phase_id].append(c)
    for p in phases:
        plan.phases.append(_phase(p, by_phase.get(p.pk, [])))
    if by_phase.get(None):
        plan.phases.append(_phase(None, by_phase[None]))
    return plan


def _phase(phase, calcs) -> PhaseCalc:
    pc = PhaseCalc(phase=phase, steps=calcs)
    starts = [c.start for c in calcs if c.start]
    ends = [c.end for c in calcs if c.end]
    pc.start = min(starts) if starts else (phase.start if phase else None)
    pc.end = max(ends) if ends else (phase.end if phase else None)
    bs = [c.step.baseline_start for c in calcs if c.step.baseline_start]
    be = [c.step.baseline_end for c in calcs if c.step.baseline_end]
    pc.base_start, pc.base_end = (min(bs) if bs else None), (max(be) if be else None)
    pc.done = sum(1 for c in calcs if c.status in CLOSED)
    if calcs and pc.done == len(calcs):
        pc.status = ProjectPhase.DONE
    elif any(c.status in (ProjectStep.DOING, ProjectStep.WAITING) or c.status in CLOSED for c in calcs):
        pc.status = ProjectPhase.ACTIVE
    return pc


def persist(plan):
    """Кеш розрахунку → кроки й фази (лише змінене). Змінився прогноз чи стан кроку, від якого залежать кроки
    інших проєктів, — їхній кеш стану позначається застарілим (state.mark_dirty): там перерахують прогноз."""
    changed, moved = [], []
    for c in plan.steps:
        s = c.step
        new = {"f_start": c.start, "f_end": c.end, "eff_status": c.status, "critical": c.critical,
               "slip_days": c.slip, "auto_note": c.note[:255]}
        if any(getattr(s, k) != v for k, v in new.items()):
            if (s.f_end, s.eff_status) != (c.end, c.status):
                moved.append(s.pk)
            for k, v in new.items():
                setattr(s, k, v)
            changed.append(s)
    if changed:
        ProjectStep.objects.bulk_update(changed, ["f_start", "f_end", "eff_status", "critical", "slip_days",
                                                  "auto_note"])
    if moved:
        from . import state
        state.mark_dirty(StepDependency.objects.filter(predecessor_id__in=moved)
                         .exclude(successor__project_id=F("predecessor__project_id"))
                         .values_list("successor__project_id", flat=True))
    phases = []
    for pc in plan.phases:
        p = pc.phase
        if p is None:
            continue
        if (p.f_start, p.f_end, p.eff_status) != (pc.start, pc.end, pc.status):
            p.f_start, p.f_end, p.eff_status = pc.start, pc.end, pc.status
            phases.append(p)
    if phases:
        ProjectPhase.objects.bulk_update(phases, ["f_start", "f_end", "eff_status"])


# ── «Потребує уваги» з плану ─────────────────────────────────────────────────

WHO_WAIT = {ProjectStep.CUSTOMER: "клієнта", ProjectStep.SUPPLIER: "постачальника", ProjectStep.COLLEAGUE: "колегу"}
PLAN_ITEMS = 8


def _blocked(c) -> bool:
    """Крок чекає незавершених попередніх кроків — затримка не в ньому (про неї скаже попередній крок)."""
    for pred, _lag in c.preds:
        status = pred.status if isinstance(pred, StepCalc) else (pred.eff_status or pred.status)
        if status not in CLOSED:
            return True
    return False


def attention(plan, project, add, today):
    """Пункти «потребує уваги» з плану (add(level, icon, text, url, kind)): прогноз пізніше строку клієнту (без
    строку — пізніше базового плану), найближча віха, що зсунулась (далі той самий зсув лише повторюється),
    прострочені кроки, які вже можна робити (попередні виконано), кроки нашої сторони найближчих днів."""
    if not plan.has_plan or not project.is_open:
        return
    url = f"/admin/projects/project/{project.pk}/change/#pj-plan"
    if project.due_date and plan.end and plan.end > project.due_date:
        add("high", "📅", f"Прогноз завершення {plan.end:%d.%m.%Y} пізніше строку клієнту "
                          f"{project.due_date:%d.%m.%Y} (+{(plan.end - project.due_date).days} дн.)", url, "risk")
    elif not project.due_date and plan.end and plan.base_end and plan.end > plan.base_end:
        add("medium", "📅", f"Прогноз завершення {plan.end:%d.%m.%Y} — на {(plan.end - plan.base_end).days} дн. "
                            f"пізніше базового плану", url, "risk")
    items, milestone = 0, False
    for c in plan.steps:
        if items >= PLAN_ITEMS:
            break
        s = c.step
        if c.status in CLOSED:
            continue
        planned = planned_end(s)
        if s.kind == ProjectStep.MILESTONE and c.slip and c.slip > 0:
            if not milestone:
                add("medium", "◆", f"Віха «{s.title}» зсунулась на {c.slip} дн.: план {s.baseline_end:%d.%m} → "
                                   f"прогноз {c.end:%d.%m}", url, "risk")
                items += 1
                milestone = True
            continue
        if not planned or _blocked(c):
            continue
        if planned < today:
            if s.side == ProjectStep.WE:
                add("high" if c.critical else "medium", "📋",
                    f"Крок «{s.title}» прострочено: мав закінчитися {planned:%d.%m}", url, "todo")
            else:
                add("medium", "⏳", f"Чекаємо {WHO_WAIT.get(s.side, '')}: «{s.title}» — мало бути {planned:%d.%m}",
                    url, "risk")
            items += 1
        elif s.side == ProjectStep.WE and (planned - today).days <= 3:
            add("medium", "📋", f"Крок «{s.title}» — до {planned:%d.%m}", url, "todo")
            items += 1
