"""Стан проєкту для списку й фільтра «За терміновістю» — кеш ProjectState поверх overview.collect().

Картка проєкту рахує стан наживо (cockpit.context → save). Список і фільтр беруть кеш і перераховують лише застаріле:
старше STALE_MINUTES, позначене dirty (projects/signals.py: прив'язки, важливі моменти, сам проєкт, новий лист гілки,
зміни замовлення, закупівлі, JLC-замовлення, відправлення, Angebot) або пораховане вчора (строки рахуються від «сьогодні»).

Колір без плану фаз — з «Потребує уваги»: є термінове (high) — 🔴, важливе (medium) — 🟡, нічого — 🟢; закритий — ⚪.
"""
from datetime import timedelta

from django.db import IntegrityError
from django.utils import timezone

from . import overview
from .models import Project, ProjectState

STALE_MINUTES = 15
HEALTH = {
    "bad": ("🔴", "Термінове"),
    "warn": ("🟡", "Потребує уваги"),
    "ok": ("🟢", "Усе гаразд"),
    "closed": ("⚪", "Закрито"),
}
ITEM_KEYS = ("level", "icon", "text", "url", "kind", "owner", "private_text")


def health_of(project, attention) -> str:
    if not project.is_open:
        return "closed"
    levels = {a["level"] for a in attention}
    if "high" in levels:
        return "bad"
    if "medium" in levels:
        return "warn"
    return "ok"


def save(project, facts=None) -> ProjectState:
    """Порахувати й записати стан. Закритий проєкт — без збору фактів (у списку для нього «—»)."""
    if facts is None and not project.is_open:
        items, health = [], "closed"
    else:
        facts = facts or overview.collect(project)
        items, health = overview.urgent(facts["attention"]), health_of(project, facts["attention"])
    defaults = {"health": health, "high": sum(1 for a in items if a["level"] == "high"),
                "medium": sum(1 for a in items if a["level"] == "medium"),
                "items": [{k: a[k] for k in ITEM_KEYS if k in a} for a in items],
                "computed_at": timezone.now(), "dirty": False}
    try:
        st, _ = ProjectState.objects.update_or_create(project=project, defaults=defaults)
    except IntegrityError:          # два процеси рахували одночасно — запис уже є
        ProjectState.objects.filter(project=project).update(**defaults)
        st = ProjectState.objects.get(project=project)
    return st


def is_stale(st, now=None) -> bool:
    now = now or timezone.now()
    if st.dirty or st.computed_at is None or st.computed_at < now - timedelta(minutes=STALE_MINUTES):
        return True
    return timezone.localtime(st.computed_at).date() != timezone.localdate()


def states(projects) -> dict:
    """{project_id: ProjectState} — свіжі; застаріле й відсутнє рахується зараз."""
    projects = list(projects)
    have = {s.project_id: s for s in ProjectState.objects.filter(project_id__in=[p.pk for p in projects])}
    now = timezone.now()
    for p in projects:
        st = have.get(p.pk)
        if st is None or is_stale(st, now):
            have[p.pk] = save(p)
    return have


def open_states():
    """Стани всіх відкритих проєктів (фільтр «За терміновістю»)."""
    return states(Project.objects.filter(status__in=Project.OPEN_STATUSES))


def mark_dirty(project_ids):
    ids = {i for i in project_ids if i}
    if ids:
        ProjectState.objects.filter(project_id__in=ids).update(dirty=True)


def items_for(st, viewer) -> list:
    """Пункти стану для конкретної людини: тема чужого листа — без тексту (overview.visible)."""
    return overview.visible(st.items or [], viewer) if st is not None else []
