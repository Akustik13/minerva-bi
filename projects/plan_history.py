"""🕘 Історія змін плану: кожна зміна кроку чи фази — хто, коли, що було → що стало, звідки.

Записують сигнали кроків і фаз (projects/signals.py): збереження з полями плану (назва, вид, фаза, хто робить,
відповідальний, сторона, дати, тривалість, стан), створення й видалення — будь-яким шляхом (вікно кроку, діаграма,
редактор, шаблон, AI, нарада, адмінка). Кеш розрахунку (прогноз, критичний шлях) і порядок рядків не записуються.

Звідки й хто — `context(user=, source=, meeting=, review=)`: його ставлять дії (views._plan_action — вручну,
plan_editor.save_all — редактор, plan.apply_plan — шаблон / AI, plan.meeting_step і plan_review.apply — нарада); без
нього — людина запиту (core.utils.get_current_user) і «вручну». batch — одна дія (зсув фази, шаблон, «✓ Застосувати
всі»): у списку — разом.
"""
from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from datetime import date

from django.utils import timezone

STEP_FIELDS = ("title", "kind", "phase_id", "side", "assignee_id", "party_id", "start", "end", "duration_days",
               "status")
PHASE_FIELDS = ("name", "icon", "gate", "start", "end")
LABELS = {"title": "назва", "kind": "вид", "phase": "фаза", "side": "хто робить", "assignee": "відповідальний",
          "party": "сторона", "start": "початок", "end": "кінець", "duration_days": "тривалість", "status": "стан",
          "name": "назва", "icon": "значок", "gate": "умова переходу"}
SHOWN = 30                 # дій у «🕘 Історія змін плану»
LINES = 6                  # рядків однієї дії, далі «ще N»

_ctx = contextvars.ContextVar("plan_history", default=None)
_deleting = contextvars.ContextVar("plan_history_deleting", default=frozenset())


def project_deleting(pk, on=True):
    """Проєкт видаляють (pre_delete / post_delete): його кроки й фази йдуть разом з ним — історію не писати
    (новий рядок історії під час каскаду зламав би видалення проєкту)."""
    cur = set(_deleting.get())
    (cur.add if on else cur.discard)(pk)
    _deleting.set(frozenset(cur))


@contextmanager
def context(user=None, source=None, meeting=None, review=None):
    """Хто й звідки змінює план усередині блоку; вкладений блок уточнює (джерело, нарада), batch — той самий."""
    cur = dict(_ctx.get() or {})
    if user is not None and getattr(user, "pk", None):
        cur["user"] = user
    if source:
        cur["source"] = source
    if meeting is not None:
        cur["meeting"] = meeting
    if review is not None:
        cur["review"] = review
    cur.setdefault("batch", uuid.uuid4().hex)
    token = _ctx.set(cur)
    try:
        yield cur
    finally:
        _ctx.reset(token)


def current() -> dict:
    return _ctx.get() or {}


def _plain(value):
    if isinstance(value, date):
        return value.isoformat()
    return value


def _readable(field, value):
    """Значення поля для історії: дата — ISO, фаза / людина / сторона — назвою (на момент зміни), стан і вид — кодом."""
    if value in (None, ""):
        return ""
    if field == "phase_id":
        from .models import ProjectPhase
        p = ProjectPhase.objects.filter(pk=value).values_list("icon", "name").first()
        return f"{p[0]} {p[1]}".strip() if p else f"#{value}"
    if field == "assignee_id":
        from django.contrib.auth import get_user_model
        u = get_user_model().objects.filter(pk=value).first()
        return (u.get_full_name() or u.username) if u else f"#{value}"
    if field == "party_id":
        from .models import ProjectParty
        return ProjectParty.objects.filter(pk=value).values_list("name", flat=True).first() or f"#{value}"
    return _plain(value)


def _key(field) -> str:
    return field[:-3] if field.endswith("_id") else field


def _values(obj, fields) -> dict:
    return {f: getattr(obj, f) for f in fields}


def _diff(old: dict, new: dict, fields) -> tuple:
    before, after = {}, {}
    for f in fields:
        if _plain(old.get(f)) != _plain(new.get(f)):
            before[_key(f)] = _readable(f, old.get(f))
            after[_key(f)] = _readable(f, new.get(f))
    return before, after


def _record(project_id, action, *, step=None, phase=None, title="", before=None, after=None):
    from core.utils import get_current_user

    from .models import PlanChange
    c = current()
    user = c.get("user") or get_current_user()
    if user is not None and not getattr(user, "pk", None):
        user = None
    PlanChange.objects.create(
        project_id=project_id, step=step, phase=phase, title=(title or "")[:200], action=action,
        before=before or {}, after=after or {}, source=c.get("source") or PlanChange.MANUAL,
        meeting=c.get("meeting"), review=c.get("review"), batch=c.get("batch") or uuid.uuid4().hex, user=user,
        at=timezone.now())


# ── сигнали (projects/signals.py) ─────────────────────────────────────────────

def _tracked(update_fields, fields) -> bool:
    if update_fields is None:
        return True
    names = {f[:-3] if f.endswith("_id") else f for f in fields} | set(fields)
    return bool(set(update_fields) & names)


def step_pre_save(step, update_fields=None):
    from .models import ProjectStep
    step._plan_old = None
    if step.pk and _tracked(update_fields, STEP_FIELDS):
        step._plan_old = ProjectStep.objects.filter(pk=step.pk).values(*STEP_FIELDS).first()


def step_post_save(step, created):
    old = getattr(step, "_plan_old", None)
    step._plan_old = None
    new = _values(step, STEP_FIELDS)
    if created:
        after = {_key(f): _readable(f, v) for f, v in new.items() if v not in (None, "") and f != "title"}
        _record(step.project_id, "add", step=step, title=step.title, after=after)
        return
    if not old:
        return
    before, after = _diff(old, new, STEP_FIELDS)
    if before or after:
        _record(step.project_id, "edit", step=step, title=step.title, before=before, after=after)


def step_post_delete(step):
    if step.project_id in _deleting.get():
        return                       # проєкт видаляють цілком — історія піде разом з ним
    old = _values(step, STEP_FIELDS)
    before = {_key(f): _readable(f, v) for f, v in old.items() if v not in (None, "") and f != "title"}
    _record(step.project_id, "remove", title=step.title, before=before)


def phase_pre_save(phase, update_fields=None):
    from .models import ProjectPhase
    phase._plan_old = None
    if phase.pk and _tracked(update_fields, PHASE_FIELDS):
        phase._plan_old = ProjectPhase.objects.filter(pk=phase.pk).values(*PHASE_FIELDS).first()


def phase_post_save(phase, created):
    old = getattr(phase, "_plan_old", None)
    phase._plan_old = None
    name = f"{phase.icon} {phase.name}".strip()
    if created:
        _record(phase.project_id, "phase_add", phase=phase, title=name)
        return
    if not old:
        return
    before, after = _diff(old, _values(phase, PHASE_FIELDS), PHASE_FIELDS)
    if before or after:
        _record(phase.project_id, "phase_edit", phase=phase, title=name, before=before, after=after)


def phase_post_delete(phase):
    if phase.project_id in _deleting.get():
        return
    _record(phase.project_id, "phase_remove", title=f"{phase.icon} {phase.name}".strip())


# ── показ: «🕘 Історія змін плану» ─────────────────────────────────────────────

STATUS_WORDS = {"todo": "заплановано", "doing": "в роботі", "waiting": "чекаємо", "done": "виконано",
                "skipped": "пропущено"}
KIND_WORDS = {"task": "крок", "milestone": "◆ віха"}
SIDE_WORDS = {"we": "ми", "customer": "клієнт", "supplier": "постачальник", "colleague": "колега"}


def _show(field, value) -> str:
    if value in (None, ""):
        return "—"
    if field in ("start", "end"):
        try:
            return date.fromisoformat(str(value)).strftime("%d.%m.%Y")
        except ValueError:
            return str(value)
    if field == "status":
        return STATUS_WORDS.get(value, value)
    if field == "kind":
        return KIND_WORDS.get(value, value)
    if field == "side":
        return SIDE_WORDS.get(value, value)
    if field == "duration_days":
        return f"{value} дн."
    return str(value)


def describe(ch) -> str:
    """Один рядок історії людською мовою: «Прототип: кінець 12.01.2026 → 20.12.2025 · стан заплановано → в роботі»."""
    name = ch.title or "—"
    if ch.action == "add":
        parts = []
        if ch.after.get("kind") == "milestone" and (ch.after.get("end") or ch.after.get("start")):
            parts.append(f"◆ {_show('end', ch.after.get('end') or ch.after.get('start'))}")
        elif ch.after.get("start") or ch.after.get("end"):
            parts.append(f"{_show('start', ch.after.get('start'))} – {_show('end', ch.after.get('end'))}")
        if ch.after.get("phase"):
            parts.append(f"фаза {ch.after['phase']}")
        return f"＋ «{name}»" + (f" ({', '.join(parts)})" if parts else "")
    if ch.action == "remove":
        return f"− прибрано «{name}»"
    if ch.action == "phase_add":
        return f"＋ фаза «{name}»"
    if ch.action == "phase_remove":
        return f"− прибрано фазу «{name}»"
    keys = [k for k in ("title", "name", "kind", "phase", "side", "assignee", "party", "start", "end", "duration_days",
                        "status", "icon", "gate") if k in ch.after or k in ch.before]
    pieces = [f"{LABELS.get(k, k)} {_show(k, ch.before.get(k))} → {_show(k, ch.after.get(k))}" for k in keys]
    what = "фаза " if ch.action == "phase_edit" else ""
    return f"{what}«{name}»: " + " · ".join(pieces)


def entries(project, viewer=None, limit=SHOWN) -> list:
    """Останні дії з планом: [{at, who, source, source_label, meetings [{title, day, url, tag}] (назва й посилання — лише
    власнику наради; кілька нарад однієї дії — M1, M2… і в рядках), lines, more, count}]."""
    from .models import PlanChange
    rows = list(PlanChange.objects.filter(project=project).select_related("user", "meeting")
                .order_by("-at", "-pk")[:limit * 20])
    groups, order = {}, []
    for ch in rows:
        key = ch.batch or f"pk{ch.pk}"
        if key not in groups:
            if len(order) >= limit:
                continue
            groups[key] = {"at": timezone.localtime(ch.at), "user": ch.user, "source": ch.source,
                           "source_label": dict(PlanChange.SOURCE_CHOICES).get(ch.source, ch.source),
                           "meeting": ch.meeting, "review_id": ch.review_id, "items": []}
            order.append(key)
        groups[key]["items"].append(ch)
    uid = getattr(viewer, "pk", None)
    out = []
    for key in order:
        g = groups[key]
        items = list(reversed(g["items"]))          # усередині дії — у тому порядку, як робили
        seen = {}
        for c in items:
            if c.meeting is not None:
                seen.setdefault(c.meeting.pk, c.meeting)
        ms = sorted(seen.values(), key=lambda m: (m.start, m.pk))
        tags = {m.pk: f"M{i}" for i, m in enumerate(ms, 1)} if len(ms) > 1 else {}
        meetings = []
        for m in ms:
            mine = uid and m.owner_id == uid
            meetings.append({"title": m.title if mine else "", "day": m.day, "tag": tags.get(m.pk, ""),
                             "url": f"/admin/meetings/meeting/{m.pk}/page/" if mine else ""})
        who = g["user"]
        out.append({"at": g["at"], "who": (who.get_full_name() or who.username) if who else "",
                    "source": g["source"], "source_label": g["source_label"], "meetings": meetings,
                    "meeting": meetings[-1] if meetings else None,
                    "lines": [(f"{tags[c.meeting_id]} · " if c.meeting_id in tags else "") + describe(c)
                              for c in items[:LINES]],
                    "more": max(len(items) - LINES, 0), "count": len(items)})
    return out
