"""⚠️ Правила ризиків — без AI: ризики, які Minerva відкриває й закриває сама (ProjectRisk.source = rule).

hits(project, facts, impacts, today) → [Hit]:
  due    — прогноз завершення за планом пізніше строку клієнту (ймовірність 3, вплив 3);
  slip   — віхи на критичному шляху зсунулись від базового плану на SLIP_MIN дн. і більше — один ризик на проєкт, бо
           зсуваються вони ланцюжком (3 × 2; пізніше строку — 3 × 3);
  dep    — крок чекає крок іншого проєкту, а той пізніше за план кроку (3 × 2);
  stock  — спільного товару бракує кільком проєктам (3 × 2; частина вже очікується — 2 × 2);
  wait   — клієнт / постачальник / колега на WAIT_DAYS дн. і більше пізніше за план кроку (2 × 2; критичний шлях — 2 × 3);
  po     — прив'язана закупівля прострочена (3 × 2; на неї чекає крок критичного шляху — 3 × 3).
sync(project, hits) — відкрити нові, оновити наявні, закрити ті, чия умова зникла (closed_auto, повернеться —
відкриється знову); закритий людиною не відкривається; ймовірність і вплив, змінені людиною (edited), правило не чіпає.
attention(project, add, today) — ризики людей і AI з балом від 6 і прострочена дата перегляду → «потребує уваги».
Викликається з overview.collect (пульт, кеш стану, тижневий звіт).
"""
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import ProjectRisk, ProjectStep

SLIP_MIN = 3
WAIT_DAYS = 7
STEPS_MAX = 5
ATTENTION_MAX = 4
CLOSED = (ProjectStep.DONE, ProjectStep.SKIPPED)
WHO = {ProjectStep.CUSTOMER: "клієнта", ProjectStep.SUPPLIER: "постачальника", ProjectStep.COLLEAGUE: "колегу"}
RULE_NAMES = {"due": "строк клієнту", "slip": "критичний шлях", "dep": "залежність", "stock": "склад",
              "wait": "чекаємо понад план", "po": "закупівля"}


@dataclass
class Hit:
    key: str
    title: str
    category: str
    probability: int
    impact: int
    evidence: str
    steps: list = field(default_factory=list)


def rule_name(key) -> str:
    return RULE_NAMES.get((key or "").split(":", 1)[0], "")


def _num(value) -> str:
    d = Decimal(value or 0).normalize()
    return f"{d:,.0f}".replace(",", " ") if d == d.to_integral() else f"{d:f}"


def hits(project, facts, impacts, today) -> list:
    from .schedule import _blocked, planned_end
    plan = facts.get("plan")
    out = []
    if plan is not None and plan.has_plan:
        critical = [c.step.pk for c in plan.steps if c.critical and c.status not in CLOSED]
        if project.due_date and plan.end and plan.end > project.due_date:
            out.append(Hit("due", f"Прогноз {plan.end:%d.%m} пізніше строку клієнту {project.due_date:%d.%m} "
                                  f"(+{(plan.end - project.due_date).days} дн.)", ProjectRisk.SCHEDULE, 3, 3,
                           f"Правило «строк клієнту»: прогноз завершення за планом {plan.end:%d.%m.%Y}, строк клієнту "
                           f"{project.due_date:%d.%m.%Y}.", critical[:STEPS_MAX]))
        out += _slip(project, plan)
        for c in plan.steps:
            s = c.step
            if c.status in CLOSED:
                continue
            planned = planned_end(s)
            if (s.side in WHO and planned and (today - planned).days >= WAIT_DAYS and not _blocked(c)):
                days = (today - planned).days
                out.append(Hit(f"wait:{s.pk}", f"Чекаємо {WHO[s.side]} {days} дн. понад план: «{s.title}»",
                               ProjectRisk.CUSTOMER if s.side == ProjectStep.CUSTOMER else ProjectRisk.SUPPLY,
                               2, 3 if c.critical else 2,
                               f"Правило «чекаємо понад {WAIT_DAYS} дн.»: крок мав закінчитися {planned:%d.%m.%Y}.",
                               [s.pk]))
    for i in impacts:
        if i.kind == "dep" and i.data["succ"].project_id == project.pk and i.data["late"] > 0:
            p, s = i.data["pred"], i.data["succ"]
            out.append(Hit(f"dep:{p.pk}:{s.pk}", f"«{s.title}» не почати без «{p.title}» ({p.project.code})",
                           ProjectRisk.SCHEDULE, 3, 2, f"Правило «залежність»: {i.text}", [s.pk]))
        elif i.kind == "stock" and project.pk in i.project_ids:
            pr = i.data["product"]
            others = [x.code for x in i.projects if x.pk != project.pk]
            partly = "очікується" in i.text
            out.append(Hit(f"stock:{pr.pk}", f"{pr.sku}: бракує {_num(i.data['short'])} шт (разом з {', '.join(others)})",
                           ProjectRisk.SUPPLY, 2 if partly else 3, 2, f"Правило «склад»: {i.text}"))
    out += _purchases(project, facts, plan, today)
    return out


def _slip(project, plan) -> list:
    """Один ризик на проєкт: віхи критичного шляху зсуваються ланцюжком від однієї причини — назва за першою
    зсунутою віхою, решта — у підставі й кроках."""
    slipped = sorted((c for c in plan.steps
                      if c.status not in CLOSED and c.step.kind == ProjectStep.MILESTONE and c.critical
                      and (c.slip or 0) >= SLIP_MIN and c.step.baseline_end),
                     key=lambda c: (c.step.baseline_end, c.step.pk))
    if not slipped:
        return []
    first = slipped[0]
    late = bool(project.due_date and any(c.end and c.end > project.due_date for c in slipped))
    title = f"«{first.step.title}» на {first.slip} дн. пізніше базового плану"
    evidence = (f"Правило «критичний шлях»: віха на критичному шляху, план {first.step.baseline_end:%d.%m} → "
                f"прогноз {first.end:%d.%m}.")
    if len(slipped) > 1:
        title += f" · зсунулось віх: {len(slipped)}"
        names = [f"«{c.step.title}» +{c.slip} дн." for c in slipped[1:STEPS_MAX]]
        evidence += " " + f"Також зсунулись: {', '.join(names)}"
    return [Hit("slip", title, ProjectRisk.SCHEDULE, 3, 3 if late else 2, evidence,
                [c.step.pk for c in slipped[:STEPS_MAX]])]


def _purchases(project, facts, plan, today) -> list:
    pos = [po for po in facts.get("purchases", []) if po.status in ("ordered", "partial") and po.expected_date
           and po.expected_date < today]
    if not pos:
        return []
    waiting = {}
    for row in (ProjectStep.links.through.objects.filter(projectstep__project=project,
                                                         projectlink__purchase_order__in=pos)
                .values_list("projectlink__purchase_order_id", "projectstep_id")):
        waiting.setdefault(row[0], []).append(row[1])
    critical = {c.step.pk for c in plan.steps if c.critical} if plan is not None else set()
    out = []
    for po in pos:
        steps = waiting.get(po.pk, [])
        who = f" ({po.supplier.name})" if po.supplier_id else ""
        out.append(Hit(f"po:{po.pk}", f"{po}{who} прострочено на {(today - po.expected_date).days} дн.",
                       ProjectRisk.SUPPLY, 3, 3 if critical & set(steps) else 2,
                       f"Правило «закупівля»: чекали {po.expected_date:%d.%m.%Y}, ще не отримано.", steps[:STEPS_MAX]))
    return out


def sync(project, found, now=None) -> tuple:
    """Ризики правил ↔ знайдене: (відкрито, закрито)."""
    now = now or timezone.now()
    existing = {r.rule_key: r for r in ProjectRisk.objects.filter(project=project, source=ProjectRisk.RULE)}
    seen, opened, closed = set(), 0, 0
    for h in found:
        if h.key in seen:
            continue
        seen.add(h.key)
        title = h.title[:200]
        r = existing.get(h.key)
        if r is None:
            try:
                with transaction.atomic():
                    r = ProjectRisk.objects.create(project=project, source=ProjectRisk.RULE, rule_key=h.key,
                                                   title=title, category=h.category, probability=h.probability,
                                                   impact=h.impact, evidence=h.evidence, owner=project.owner,
                                                   opened_at=now)
            except IntegrityError:          # інший процес щойно відкрив той самий ризик
                continue
            if h.steps:
                r.steps.set(h.steps)
            opened += 1
            continue
        if r.status == ProjectRisk.CLOSED and not r.closed_auto:
            continue                        # закрила людина — правило не відкриває знову
        changes = {}
        if r.status == ProjectRisk.CLOSED:
            changes.update(status=ProjectRisk.OPEN, closed_auto=False, closed_at=None, opened_at=now)
            opened += 1
        if r.title != title:
            changes["title"] = title
        if r.evidence != h.evidence:
            changes["evidence"] = h.evidence
        if not r.edited and (r.probability, r.impact) != (h.probability, h.impact):
            changes.update(probability=h.probability, impact=h.impact)
        if changes:
            ProjectRisk.objects.filter(pk=r.pk).update(updated_at=now, **changes)
        if set(r.steps.values_list("pk", flat=True)) != set(h.steps):
            r.steps.set(h.steps)
    for key, r in existing.items():
        if key not in seen and r.status != ProjectRisk.CLOSED:
            ProjectRisk.objects.filter(pk=r.pk).update(status=ProjectRisk.CLOSED, closed_auto=True, closed_at=now,
                                                       updated_at=now)
            closed += 1
    return opened, closed


def attention(project, add, today):
    """Ризики → «потребує уваги»: відкриті ризики людей і AI з балом від 6 (правила вже мають свої пункти) і ризики
    з минулою датою перегляду (до ATTENTION_MAX)."""
    url = f"/admin/projects/project/{project.pk}/change/#pj-risks"
    n = 0
    for r in (ProjectRisk.objects.filter(project=project, status__in=(ProjectRisk.OPEN, ProjectRisk.WATCH))
              .select_related("owner").order_by("-probability", "-impact", "pk")):
        if n >= ATTENTION_MAX:
            break
        owner = (r.owner.get_full_name() or r.owner.get_username()) if r.owner_id else ""
        if r.source != ProjectRisk.RULE and r.status == ProjectRisk.OPEN and r.score >= 6:
            add("medium", "⚠️", f"Ризик «{r.title}» — бал {r.score}" + (f", власник {owner}" if owner
                                                                       else ", без власника"), url, "risk")
            n += 1
        elif r.review_on and r.review_on < today:
            add("info", "⏰", f"Переглянути ризик «{r.title}» — дата перегляду {r.review_on:%d.%m} минула", url, "todo")
            n += 1


def counts(project_ids) -> dict:
    """{project_id: {"red": n, "yellow": n, "green": n}} — відкриті ризики й ті, за якими стежимо."""
    out = {}
    for pid, p, i in (ProjectRisk.objects.filter(project_id__in=project_ids,
                                                 status__in=(ProjectRisk.OPEN, ProjectRisk.WATCH))
                      .values_list("project_id", "probability", "impact")):
        level = "red" if p * i >= 6 else ("yellow" if p * i >= 3 else "green")
        d = out.setdefault(pid, {"red": 0, "yellow": 0, "green": 0})
        d[level] += 1
    return out


def matrix(risks) -> list:
    """Матриця 3 × 3: рядки — ймовірність 3…1, клітинки — вплив 1…3: [{p, rows: [{i, level, items}]}]."""
    cells = {(p, i): [] for p in (1, 2, 3) for i in (1, 2, 3)}
    for n, r in enumerate(risks, 1):
        cells[(r.probability, r.impact)].append({"n": n, "risk": r})
    return [{"p": p, "cells": [{"i": i, "score": p * i,
                                "level": "red" if p * i >= 6 else ("yellow" if p * i >= 3 else "green"),
                                "items": cells[(p, i)]} for i in (1, 2, 3)]} for p in (3, 2, 1)]


def review_default(today=None):
    return (today or timezone.localdate()) + timedelta(days=7)
