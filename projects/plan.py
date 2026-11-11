"""План проєкту: шаблони, застосування шаблону, кроки й фази, залежності, базовий план (розрахунок — schedule.py).

Шаблон (ProjectTemplate.plan) — JSON:
    [{"name": "Прототип", "icon": "🔧", "gate": "Прототипи зібрано",
      "steps": [{"title": "…", "days": 7, "side": "we|customer|supplier|colleague", "kind": "task|milestone",
                 "trigger": "jlc_delivered", "after": null | "parallel" | [номери кроків шаблону від 0]}]}]
after: null — після попереднього кроку шаблону, "parallel" — разом з попереднім (ті самі попередники), список —
після цих кроків. Вбудовані шаблони (BUILTIN) з'являються в базі самі (ensure_builtin), їх можна сховати, не видалити.
"""
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Project, ProjectLink, ProjectPhase, ProjectStep, ProjectTemplate, StepDependency

W, C, S = ProjectStep.WE, ProjectStep.CUSTOMER, ProjectStep.SUPPLIER
M = ProjectStep.MILESTONE
OTHER_STEPS = 400               # кроків інших проєктів у вікні кроку (⛓ залежність між проєктами)


def _st(title, days=1, side=W, kind=ProjectStep.TASK, trigger="", after=None):
    return {"title": title, "days": days, "side": side, "kind": kind, "trigger": trigger, "after": after}


BUILTIN = [
    {"name": "Розробка антени під клієнта", "order": 10,
     "description": "Від запиту й специфікації через симуляцію, прототип, вимірювання й зразки — до серії й поставки.",
     "plan": [
         {"name": "Запит і специфікація", "icon": "📋", "gate": "Специфікацію погоджено", "steps": [
             _st("Уточнити вимоги: частота, VSWR, роз'єм, кабель, корпус", 5),
             _st("Надіслати Angebot на розробку й зразки", 2, trigger="offer_sent"),
             _st("Клієнт погодив специфікацію й Angebot", 0, C, M, "offer_accepted")]},
         {"name": "Розробка й симуляція", "icon": "📐", "gate": "Дизайн готовий до прототипу", "steps": [
             _st("EM-модель і симуляція", 10),
             _st("Gerber і BOM прототипу", 5)]},
         {"name": "Прототип", "icon": "🔧", "gate": "Прототипи зібрано", "steps": [
             _st("Замовити PCB / PCBA", 2, trigger="jlc_ordered"),
             _st("Виготовлення й доставка плат", 10, S, trigger="jlc_delivered"),
             _st("Складання й налаштування прототипів", 5)]},
         {"name": "Вимірювання", "icon": "📏", "gate": "Параметри в допуску", "steps": [
             _st("VSWR і ефективність", 2),
             _st("Діаграма спрямованості в безлунній камері", 3)]},
         {"name": "Зразки клієнту", "icon": "📦", "gate": "Клієнт погодив зразок", "steps": [
             _st("Відправити зразки", 1, trigger="shipment_sent"),
             _st("Тест у клієнта", 10, C),
             _st("Клієнт погодив зразок", 0, C, M)]},
         {"name": "Серія", "icon": "🏭", "gate": "Замовлення на серію отримано", "steps": [
             _st("Angebot на серію", 2, trigger="offer_sent"),
             _st("Замовлення клієнта на серію", 0, C, M, "order_received"),
             _st("Замовлення виробнику (PO)", 2, trigger="po_ordered"),
             _st("Виготовлення серії", 21, S, trigger="po_received")]},
         {"name": "Поставка", "icon": "🚚", "gate": "Доставлено клієнту", "steps": [
             _st("Вхідний контроль і пакування", 2),
             _st("Відправити клієнту", 1, trigger="order_shipped"),
             _st("Доставлено", 0, C, M, "order_delivered")]},
     ]},
    {"name": "Серійне виготовлення через постачальника", "order": 20,
     "description": "Angebot → замовлення клієнта → закупівля у виробника → виготовлення → контроль → поставка.",
     "plan": [
         {"name": "Пропозиція", "icon": "📝", "gate": "Замовлення отримано", "steps": [
             _st("Запит ціни й строку у виробника", 3, S),
             _st("Надіслати Angebot", 2, trigger="offer_sent"),
             _st("Замовлення клієнта", 0, C, M, "order_received")]},
         {"name": "Виготовлення", "icon": "🏭", "gate": "Товар на складі", "steps": [
             _st("Замовлення виробнику (PO)", 2, trigger="po_ordered"),
             _st("Виготовлення", 21, S),
             _st("Доставка від виробника", 7, S, trigger="po_received")]},
         {"name": "Поставка", "icon": "🚚", "gate": "Доставлено клієнту", "steps": [
             _st("Вхідний контроль і пакування", 2),
             _st("Відправити клієнту", 1, trigger="order_shipped"),
             _st("Доставлено", 0, C, M, "order_delivered")]},
     ]},
    {"name": "Кабельна збірка", "order": 30,
     "description": "Креслення → погодження → зразок → серія кабельних збірок.",
     "plan": [
         {"name": "Креслення", "icon": "📐", "gate": "Креслення погоджено", "steps": [
             _st("Креслення кабелю: роз'єми, довжина, маркування", 3),
             _st("Погодження креслення клієнтом", 5, C),
             _st("Креслення погоджено", 0, C, M)]},
         {"name": "Зразок", "icon": "🔧", "gate": "Зразок погоджено", "steps": [
             _st("Компоненти для зразка (PO)", 7, S, trigger="po_received"),
             _st("Виготовити й перевірити зразок", 3),
             _st("Відправити зразок", 1, trigger="shipment_sent"),
             _st("Клієнт погодив зразок", 7, C, M)]},
         {"name": "Серія", "icon": "🏭", "gate": "Доставлено клієнту", "steps": [
             _st("Замовлення клієнта на серію", 0, C, M, "order_received"),
             _st("Компоненти для серії (PO)", 10, S, trigger="po_received"),
             _st("Виготовлення серії", 7),
             _st("Відправити клієнту", 1, trigger="order_shipped"),
             _st("Доставлено", 0, C, M, "order_delivered")]},
     ]},
    {"name": "Перепродаж зі складу", "order": 40,
     "description": "Короткий проєкт: Angebot → замовлення → відправка зі складу.",
     "plan": [
         {"name": "Продаж", "icon": "🛒", "gate": "Доставлено клієнту", "steps": [
             _st("Надіслати Angebot", 1, trigger="offer_sent"),
             _st("Замовлення клієнта", 0, C, M, "order_received"),
             _st("Відправити зі складу", 2, trigger="order_shipped"),
             _st("Доставлено", 0, C, M, "order_delivered")]},
     ]},
]


class PlanError(ValueError):
    pass


def ensure_builtin():
    """Вбудовані шаблони — у базі (лише відсутні за назвою; приховані людиною не повертаються)."""
    have = set(ProjectTemplate.objects.values_list("name", flat=True))
    for t in BUILTIN:
        if t["name"] not in have:
            ProjectTemplate.objects.create(name=t["name"], description=t["description"], plan=t["plan"],
                                           builtin=True, order=t["order"])


def templates():
    ensure_builtin()
    return list(ProjectTemplate.objects.filter(is_active=True).order_by("order", "name"))


def template_counts(template) -> tuple:
    phases = template.plan or []
    return len(phases), sum(len(p.get("steps") or []) for p in phases)


@transaction.atomic
def apply_template(project, template, start=None, user=None) -> int:
    """Фази й кроки шаблону → проєкт; дати — від start за тривалостями й залежностями. → кроків."""
    return len(apply_plan(project, template.plan or [], start, user))


@transaction.atomic
def apply_plan(project, phases, start=None, user=None, source=ProjectStep.TEMPLATE) -> list:
    """Фази й кроки у форматі шаблону (шаблон або AI-чернетка, projects/ai_plan.py) → порожній план проєкту.
    → [(крок, опис кроку з phases)]."""
    from . import schedule
    if ProjectStep.objects.filter(project=project).exists() or ProjectPhase.objects.filter(project=project).exists():
        raise PlanError("У проєкті вже є план — додайте кроки вручну або спершу очистіть план")
    start = start or project.start_date or timezone.localdate()
    created, order = [], 0
    for pi, ph in enumerate(phases or []):
        phase = ProjectPhase.objects.create(project=project, order=(pi + 1) * 10, name=str(ph.get("name"))[:120],
                                            icon=str(ph.get("icon") or "")[:8], gate=str(ph.get("gate") or "")[:200])
        for st in ph.get("steps") or []:
            order += 10
            kind = M if st.get("kind") == M else ProjectStep.TASK
            step = ProjectStep.objects.create(
                project=project, phase=phase, order=order, title=str(st.get("title") or "Крок")[:200], kind=kind,
                side=st.get("side") if st.get("side") in dict(ProjectStep.SIDE_CHOICES) else W,
                duration_days=max(int(st.get("days") or 0), 0 if kind == M else 1),
                trigger=st.get("trigger") if st.get("trigger") in schedule.TRIGGER_TYPES else "",
                source=source, created_by=user)
            created.append((step, st))
    for i, (step, st) in enumerate(created):
        if i == 0:
            continue
        after = st.get("after")
        if after == "parallel":
            preds = [p for p, _ in StepDependency.objects.filter(successor=created[i - 1][0])
                     .values_list("predecessor_id", "lag_days")]
            for pid in preds:
                StepDependency.objects.get_or_create(predecessor_id=pid, successor=step)
            continue
        idxs = after if isinstance(after, list) else [i - 1]
        for j in idxs:
            if isinstance(j, int) and 0 <= j < len(created) and j != i:
                StepDependency.objects.get_or_create(predecessor=created[j][0], successor=step)
    _fix_dates(project, start)
    if not project.start_date:
        project.start_date = start
        project.save(update_fields=["start_date", "updated_at"])
    return created


def _fix_dates(project, start):
    """Планові дати кроків, у яких їх немає: перший — від start, далі — за залежностями (прямий прохід)."""
    from . import schedule
    first = ProjectStep.objects.filter(project=project, start__isnull=True).order_by("phase__order", "order",
                                                                                      "pk").first()
    if first is not None and not first.predecessor_links.exists():
        first.start = start
        first.save(update_fields=["start"])
    plan = schedule.compute(project, today=start)
    for c in plan.steps:
        s = c.step
        if s.start is None or s.end is None:
            s.start = s.start or c.start
            s.end = s.end or c.end
            s.save(update_fields=["start", "end"])


def save_as_template(project, name, user=None) -> ProjectTemplate:
    """План проєкту → новий шаблон: тривалості з планових дат, залежності — номерами кроків."""
    name = " ".join(str(name or "").split())[:120]
    if not name:
        raise PlanError("Вкажіть назву шаблону")
    if ProjectTemplate.objects.filter(name=name).exists():
        raise PlanError("Шаблон з такою назвою вже є")
    phases = list(ProjectPhase.objects.filter(project=project).order_by("order", "pk"))
    steps = list(ProjectStep.objects.filter(project=project).order_by("phase__order", "order", "pk"))
    if not steps:
        raise PlanError("У плані немає кроків")
    index = {s.pk: i for i, s in enumerate(steps)}
    deps = {}
    for d in StepDependency.objects.filter(successor__in=steps):
        if d.predecessor_id in index:
            deps.setdefault(d.successor_id, []).append(index[d.predecessor_id])
    out = []
    groups = [(p, [s for s in steps if s.phase_id == p.pk]) for p in phases]
    rest = [s for s in steps if s.phase_id is None]
    if rest:
        groups.append((None, rest))
    for p, items in groups:
        out.append({"name": p.name if p else "Інше", "icon": p.icon if p else "", "gate": p.gate if p else "",
                    "steps": [{"title": s.title, "kind": s.kind, "side": s.side, "trigger": s.trigger,
                               "days": 0 if s.kind == M else ((s.end - s.start).days + 1 if s.start and s.end
                                                              else s.duration_days),
                               "after": sorted(deps.get(s.pk, []))} for s in items]})
    return ProjectTemplate.objects.create(name=name, plan=out, created_by=user,
                                          description=f"З проєкту {project.code} · {project.name}"[:500])


@transaction.atomic
def clear(project):
    ProjectStep.objects.filter(project=project).delete()
    ProjectPhase.objects.filter(project=project).delete()
    if project.baseline_at:
        project.baseline_at = None
        project.save(update_fields=["baseline_at", "updated_at"])


@transaction.atomic
def baseline(project, user=None) -> int:
    """Базовий план = поточний прогноз кожного кроку (з чим потім порівнюється «зсув»)."""
    from . import overview, schedule
    plan = schedule.compute(project, overview.collect(project))
    n = 0
    for c in plan.steps:
        ProjectStep.objects.filter(pk=c.step.pk).update(baseline_start=c.start, baseline_end=c.end)
        n += 1
    project.baseline_at = timezone.now()
    project.save(update_fields=["baseline_at", "updated_at"])
    return n


# ── Кроки й фази ─────────────────────────────────────────────────────────────

def _date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise PlanError(f"Дата «{value}» — не у форматі РРРР-ММ-ДД")


def _would_cycle(step, pred) -> bool:
    """Чи з'явиться цикл, якщо step піде після pred: step уже є (транзитивним) попередником pred."""
    seen, todo = set(), [pred.pk]
    while todo:
        cur = todo.pop()
        if cur == step.pk:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        todo.extend(StepDependency.objects.filter(successor_id=cur).values_list("predecessor_id", flat=True))
    return False


@transaction.atomic
def save_step(project, data, user=None) -> ProjectStep:
    """Створити / змінити крок: {id, title, kind, phase, side, assignee, start, end, duration, status, trigger, auto,
    after: [id], lag, links: [id], offers: [id], notes}."""
    from django.contrib.auth import get_user_model
    from . import schedule
    title = " ".join(str(data.get("title") or "").split())[:200]
    if not title:
        raise PlanError("Напишіть назву кроку")
    step = (ProjectStep.objects.filter(project=project, pk=data.get("id")).first()
            if str(data.get("id") or "").isdigit() else None)
    if data.get("id") and step is None:
        raise PlanError("Крок не знайдено")
    step = step or ProjectStep(project=project, created_by=user,
                               order=(ProjectStep.objects.filter(project=project).order_by("-order")
                                      .values_list("order", flat=True).first() or 0) + 10)
    step.title = title
    step.kind = ProjectStep.MILESTONE if data.get("kind") == M else ProjectStep.TASK
    phase_id = data.get("phase")
    step.phase = (ProjectPhase.objects.filter(project=project, pk=phase_id).first()
                  if str(phase_id or "").isdigit() else None)
    step.side = data.get("side") if data.get("side") in dict(ProjectStep.SIDE_CHOICES) else W
    from .models import ProjectParty
    from .parties import ROLE_SIDE
    pid = data.get("party")
    step.party = (ProjectParty.objects.filter(project=project, pk=pid).first() if str(pid or "").isdigit() else None)
    if step.party is not None:
        step.side = ROLE_SIDE[step.party.role]           # «чекаємо клієнта / постачальника» — за роллю сторони
    aid = data.get("assignee")
    step.assignee = (get_user_model().objects.filter(pk=aid, is_active=True, is_staff=True).first()
                     if str(aid or "").isdigit() else None)
    step.start, step.end = _date(data.get("start")), _date(data.get("end"))
    if step.kind == M:
        step.start = step.end = step.end or step.start
    if step.start and step.end and step.end < step.start:
        raise PlanError("Кінець раніше за початок")
    try:
        step.duration_days = max(min(int(data.get("duration") or 1), 999), 0 if step.kind == M else 1)
    except (TypeError, ValueError):
        step.duration_days = 1
    old_status = step.status
    status = data.get("status")
    step.status = status if status in dict(ProjectStep.STATUS_CHOICES) else step.status
    if step.status == ProjectStep.DONE and old_status != ProjectStep.DONE and not step.done_on:
        step.done_on = timezone.localdate()
    if step.status != ProjectStep.DONE and old_status == ProjectStep.DONE:
        step.done_on = None
    if step.status == ProjectStep.DOING and not step.started_on:
        step.started_on = timezone.localdate()
    step.trigger = data.get("trigger") if data.get("trigger") in schedule.TRIGGER_TYPES else ""
    step.auto = bool(data.get("auto", True))
    step.notes = str(data.get("notes") or "")[:4000]
    step.save()

    if "after" in data:
        lag = data.get("lag") or 0
        try:
            lag = max(min(int(lag), 365), -365)
        except (TypeError, ValueError):
            lag = 0
        wanted = [int(i) for i in (data.get("after") or []) if str(i).isdigit()]
        # попередні — кроки цього проєкту або відкритих інших (⛓ залежність між проєктами, impact.py)
        preds = list(ProjectStep.objects.filter(Q(project=project) | Q(project__status__in=Project.OPEN_STATUSES),
                                                pk__in=wanted).exclude(pk=step.pk).select_related("project"))
        for p in preds:
            if _would_cycle(step, p):
                where = "" if p.project_id == project.pk else f" ({p.project.code})"
                raise PlanError(f"«{p.title}»{where} уже йде після цього кроку — вийшло б коло")
        StepDependency.objects.filter(successor=step).exclude(predecessor__in=preds).delete()
        for p in preds:
            dep, _ = StepDependency.objects.get_or_create(predecessor=p, successor=step)
            if dep.lag_days != lag:
                dep.lag_days = lag
                dep.save(update_fields=["lag_days"])
    if "links" in data:
        ids = [int(i) for i in (data.get("links") or []) if str(i).isdigit()]
        step.links.set(ProjectLink.objects.filter(project=project, pk__in=ids, status=ProjectLink.CONFIRMED)
                       .exclude(thread__isnull=False).exclude(message__isnull=False))
    if "offers" in data:
        ids = [int(i) for i in (data.get("offers") or []) if str(i).isdigit()]
        step.offers.set(project.offers.filter(pk__in=ids) if hasattr(project, "offers") else [])
    return step


@transaction.atomic
def delete_step(project, step_id) -> bool:
    """Крок прибрано — наступні за ним кроки йдуть після його попередників (ланцюжок не рветься)."""
    step = ProjectStep.objects.filter(project=project, pk=step_id).first()
    if step is None:
        return False
    preds = list(StepDependency.objects.filter(successor=step).values_list("predecessor_id", flat=True))
    succs = list(StepDependency.objects.filter(predecessor=step).values_list("successor_id", "lag_days"))
    step.delete()
    for sid, lag in succs:
        for pid in preds:
            if pid != sid:
                StepDependency.objects.get_or_create(predecessor_id=pid, successor_id=sid, defaults={"lag_days": lag})
    return True


CLOSED_STATES = (ProjectStep.DONE, ProjectStep.SKIPPED)
OPEN_SHOWN = 15


def _mail_day(message):
    if message is None or not message.sent_at:
        return timezone.localdate()
    return timezone.localtime(message.sent_at).date() if timezone.is_aware(message.sent_at) else message.sent_at.date()


def brief(project) -> dict:
    """Для прев'ю листа (📁 біля проєкту): колір стану (кеш state.py), фаза «3/7 · 🔧 Прототип», наступний крок і
    відкриті кроки — для «✅ Закрити крок». Лише кеш плану (f_* / eff_status), без розрахунку."""
    from . import state
    st = state.states([project]).get(project.pk)
    icon, label = state.HEALTH.get(st.health if st else "", ("", ""))
    phases = list(ProjectPhase.objects.filter(project=project).order_by("order", "pk"))
    steps = list(ProjectStep.objects.filter(project=project).order_by("phase__order", "order", "pk"))

    def is_open(s):
        return (s.eff_status or s.status) not in CLOSED_STATES

    current = next((p for p in phases if any(s.phase_id == p.pk and is_open(s) for s in steps)), None)
    if current is not None:
        phase = f"{phases.index(current) + 1}/{len(phases)} · {current.icon} {current.name}".replace("  ", " ")
    else:
        phase = "усі фази завершено" if phases and steps else ""
    open_steps = sorted((s for s in steps if is_open(s)), key=lambda s: (s.f_start or s.start or date.max, s.order))

    def when(s):
        d = s.f_end or s.end
        return f"{d:%d.%m}" if d else ""
    nxt = open_steps[0] if open_steps else None
    return {"health": st.health if st else "", "icon": icon, "label": label, "phase": phase,
            "next": {"title": nxt.title, "end": when(nxt)} if nxt else None,
            "open": [{"id": s.pk, "title": s.title, "end": when(s)} for s in open_steps[:OPEN_SHOWN]]}


def quick_step(project, title, message=None, user=None) -> ProjectStep:
    """➕ Крок з листа: у поточну фазу (перша незавершена), після її кроків, «ми», від сьогодні на 1 день;
    у примітці — дата листа (тема — ні: її бачить лише власник скриньки)."""
    title = " ".join(str(title or "").split())[:200]
    if not title:
        raise PlanError("Напишіть назву кроку")
    steps = list(ProjectStep.objects.filter(project=project).order_by("phase__order", "order", "pk"))
    phases = list(ProjectPhase.objects.filter(project=project).order_by("order", "pk"))
    phase = next((p for p in phases if any(s.phase_id == p.pk and (s.eff_status or s.status) not in CLOSED_STATES
                                           for s in steps)), phases[-1] if phases else None)
    same = [s.order for s in steps if s.phase_id == (phase.pk if phase else None)]
    order = (max(same) + 1) if same else (max([s.order for s in steps] or [0]) + 10)
    today = timezone.localdate()
    return ProjectStep.objects.create(
        project=project, phase=phase, order=order, title=title, kind=ProjectStep.TASK, side=ProjectStep.WE,
        start=today, end=today, duration_days=1, notes=f"✉️ з листа від {_mail_day(message):%d.%m.%Y}",
        source=ProjectStep.MANUAL, created_by=user)


MEETING_NOTE = "🎙 з наради від {day:%d.%m.%Y}"


def meeting_step(project, title, *, assignee=None, due=None, side=ProjectStep.WE, quote="", meeting=None,
                 meeting_second=None, user=None) -> ProjectStep:
    """📅 Крок з пункту наради (meetings/services.accept): як «➕ Крок з листа» — у поточну фазу, після її кроків, від
    сьогодні до строку (немає — на 1 день); хто робить і виконавець (активний співробітник); у примітці — дата наради й
    дослівна цитата (її можна прибрати разом із нарадою: meetings/services.scrub_quotes)."""
    from django.contrib.auth import get_user_model
    from . import state
    title = " ".join(str(title or "").split())[:200]
    if not title:
        raise PlanError("Напишіть назву кроку")
    person = None
    if assignee is not None and assignee != "":
        pk = getattr(assignee, "pk", assignee)
        person = (get_user_model().objects.filter(pk=pk, is_active=True, is_staff=True).first()
                  if str(pk).isdigit() else None)
        if person is None:
            raise PlanError("Виконавця не знайдено або його вимкнено")
    steps = list(ProjectStep.objects.filter(project=project).order_by("phase__order", "order", "pk"))
    phases = list(ProjectPhase.objects.filter(project=project).order_by("order", "pk"))
    phase = next((p for p in phases if any(s.phase_id == p.pk and (s.eff_status or s.status) not in CLOSED_STATES
                                           for s in steps)), phases[-1] if phases else None)
    same = [s.order for s in steps if s.phase_id == (phase.pk if phase else None)]
    order = (max(same) + 1) if same else (max([s.order for s in steps] or [0]) + 10)
    today = timezone.localdate()
    end = due or today
    start = min(today, end)
    if (end - start).days > 998:
        raise PlanError("Строк кроку задалеко — до трьох років")
    start_at =(meeting.started_at or meeting.created_at) if meeting is not None else None
    day = timezone.localtime(start_at).date() if start_at else today
    note = MEETING_NOTE.format(day=day)
    quote = " ".join(str(quote or "").split())
    if quote:
        note += f": «{quote}»"
    step = ProjectStep.objects.create(
        project=project, phase=phase, order=order, title=title, kind=ProjectStep.TASK,
        side=side if side in dict(ProjectStep.SIDE_CHOICES) else ProjectStep.WE, assignee=person,
        start=start, end=end, duration_days=(end - start).days + 1, notes=note[:4000],
        source=ProjectStep.MEETING, meeting=meeting,
        meeting_second=max(int(meeting_second), 0) if meeting_second is not None else None, created_by=user)
    state.mark_dirty([project.pk])
    return step


def close_step(project, step_id, message=None, user=None) -> ProjectStep:
    """✅ Закрити крок з листа: «виконано» датою листа; у примітці — що за листом (без теми)."""
    step = ProjectStep.objects.filter(project=project, pk=step_id).first()
    if step is None:
        raise PlanError("Крок не знайдено")
    day = _mail_day(message)
    step.status, step.done_on = ProjectStep.DONE, day
    note = f"✓ виконано за листом від {day:%d.%m.%Y}"
    step.notes = f"{step.notes}\n{note}".strip() if step.notes else note
    step.save(update_fields=["status", "done_on", "notes", "updated_at"])
    return step


def move_step(project, step_id, start=None, end=None) -> ProjectStep:
    """📋 Класична діаграма: смугу перетягнули — нові планові дати (крок: початок і кінець → тривалість; віха —
    дата). Прогноз однаково рахує розрахунок (не раніше попередніх кроків)."""
    step = ProjectStep.objects.filter(project=project, pk=step_id).first()
    if step is None:
        raise PlanError("Крок не знайдено")
    start, end = _date(start), _date(end)
    if step.kind == ProjectStep.MILESTONE:
        day = end or start
        if day is None:
            raise PlanError("Немає дати віхи")
        step.start, step.end = None, day
    else:
        start = start or step.start
        end = end or step.end
        if start is None or end is None:
            raise PlanError("Немає дат кроку")
        if end < start:
            raise PlanError("Кінець раніше за початок")
        if (end - start).days > 999:
            raise PlanError("Крок довший за 999 днів")
        step.start, step.end, step.duration_days = start, end, (end - start).days + 1
    step.save(update_fields=["start", "end", "duration_days", "updated_at"])
    return step


@transaction.atomic
def shift_phase(project, phase_id, days) -> tuple:
    """📋 Фазу перетягнули мишкою: невиконані кроки фази — на days днів (планові дати; без своїх дат — від прогнозу),
    власні дати фази теж. Виконані й пропущені кроки — як були (це вже історія). → (фаза, скільки кроків зсунуто)."""
    phase = ProjectPhase.objects.filter(project=project, pk=phase_id).first()
    if phase is None:
        raise PlanError("Фазу не знайдено")
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise PlanError("Скільки днів?") from None
    if not days or abs(days) > 999:
        raise PlanError("Зсув — від 1 до 999 днів")
    delta, moved = timedelta(days=days), 0
    for s in ProjectStep.objects.filter(project=project, phase=phase):
        if (s.eff_status or s.status) in (ProjectStep.DONE, ProjectStep.SKIPPED):
            continue
        start, end = s.start or s.f_start, s.end or s.f_end
        if end is None and start is None:
            continue
        if s.kind == ProjectStep.MILESTONE:
            s.start, s.end = None, (end or start) + delta
        else:
            start, end = start or end, end or start
            s.start, s.end = start + delta, end + delta
            s.duration_days = (s.end - s.start).days + 1
        s.save(update_fields=["start", "end", "duration_days", "updated_at"])
        moved += 1
    if phase.start or phase.end:
        phase.start = phase.start + delta if phase.start else None
        phase.end = phase.end + delta if phase.end else None
        phase.save(update_fields=["start", "end"])
    return phase, moved


def save_phase(project, data) -> ProjectPhase:
    name = " ".join(str(data.get("name") or "").split())[:120]
    if not name:
        raise PlanError("Напишіть назву фази")
    phase = (ProjectPhase.objects.filter(project=project, pk=data.get("id")).first()
             if str(data.get("id") or "").isdigit() else None)
    if data.get("id") and phase is None:
        raise PlanError("Фазу не знайдено")
    if phase is None:
        last = ProjectPhase.objects.filter(project=project).order_by("-order").values_list("order", flat=True).first()
        phase = ProjectPhase(project=project, order=(last or 0) + 10)
    phase.name = name
    phase.icon = str(data.get("icon") or "").strip()[:8]
    phase.gate = " ".join(str(data.get("gate") or "").split())[:200]
    if str(data.get("order") or "").lstrip("-").isdigit():
        phase.order = max(int(data["order"]), 0)
    phase.save()
    return phase


def delete_phase(project, phase_id) -> bool:
    """Фазу прибрано — її кроки лишаються без фази."""
    return bool(ProjectPhase.objects.filter(project=project, pk=phase_id).delete()[0])


def editor_data(project, plan) -> dict:
    """Дані для вікна кроку (JSON у пульті): кроки, фази, люди, об'єкти для прив'язки, варіанти полів."""
    from django.contrib.auth import get_user_model
    from .models import TRIGGERS
    steps = []
    deps = {}
    for d in StepDependency.objects.filter(successor__project=project):
        deps.setdefault(d.successor_id, []).append((d.predecessor_id, d.lag_days))
    link_ids = {}
    for row in ProjectStep.links.through.objects.filter(projectstep__project=project):
        link_ids.setdefault(row.projectstep_id, []).append(row.projectlink_id)
    offer_ids = {}
    for row in ProjectStep.offers.through.objects.filter(projectstep__project=project):
        offer_ids.setdefault(row.projectstep_id, []).append(row.offer_id)
    agent = {}                          # 🤖 завдання агенту, доручені з кроку (projects/delegate.py)
    for row in (ProjectStep.agent_tasks.through.objects.filter(projectstep__project=project)
                .select_related("agenttask").order_by("-agenttask__created_at")):
        agent.setdefault(row.projectstep_id, []).append(
            {"id": row.agenttask_id, "label": f"#{row.agenttask_id} {row.agenttask.get_status_display()}",
             "url": f"/admin/agent/agenttask/{row.agenttask_id}/review/"})
    for c in plan.steps:
        s = c.step
        d = deps.get(s.pk, [])
        steps.append({"id": s.pk, "title": s.title, "kind": s.kind, "phase": s.phase_id or "", "side": s.side,
                      "party": s.party_id or "",
                      "assignee": s.assignee_id or "", "start": s.start.isoformat() if s.start else "",
                      "end": s.end.isoformat() if s.end else "", "duration": s.duration_days, "status": s.status,
                      "trigger": s.trigger, "auto": s.auto, "after": [p for p, _ in d],
                      "lag": d[0][1] if d else 0, "links": link_ids.get(s.pk, []), "offers": offer_ids.get(s.pk, []),
                      "notes": s.notes, "eff": c.status, "note": c.note, "agent": agent.get(s.pk, [])[:3],
                      "f_start": c.start.isoformat() if c.start else "", "f_end": c.end.isoformat() if c.end else ""})
    from .cockpit import link_label
    links = []
    for l in (ProjectLink.objects.filter(project=project, status=ProjectLink.CONFIRMED)
              .exclude(thread__isnull=False).exclude(message__isnull=False)
              .select_related("order", "purchase_order__supplier", "jlc_order", "shipment")):
        links.append({"id": l.pk, "label": link_label(l, None)[0]})
    offers = [{"id": o.pk, "label": f"Angebot {o.display_number} · {o.get_status_display()}"}
              for o in (project.offers.order_by("created_at") if hasattr(project, "offers") else [])]
    users = [{"id": u.pk, "label": u.get_full_name() or u.username}
             for u in get_user_model().objects.filter(is_active=True, is_staff=True).order_by("first_name",
                                                                                              "username")[:200]]
    # ⛓ кроки інших відкритих проєктів — для «Після кроків» (залежність між проєктами)
    others = [{"id": s.pk, "label": f"{s.project.code} · {s.title}"}
              for s in (ProjectStep.objects.filter(project__status__in=Project.OPEN_STATUSES).exclude(project=project)
                        .select_related("project").order_by("project__code", "phase__order", "order", "pk")
                        [:OTHER_STEPS])]
    return {"steps": steps, "phases": [{"id": p.pk, "name": p.name, "icon": p.icon, "gate": p.gate, "order": p.order}
                                       for p in ProjectPhase.objects.filter(project=project).order_by("order", "pk")],
            "users": users, "links": links, "offers": offers, "others": others,
            "parties": [{"id": p.pk, "label": f"{p.get_role_display().split(' ', 1)[0]} {p.name}", "role": p.role}
                        for p in project.parties.all()],
            "kinds": ProjectStep.KIND_CHOICES, "sides": ProjectStep.SIDE_CHOICES,
            "statuses": ProjectStep.STATUS_CHOICES, "triggers": TRIGGERS}
