"""Розумніші підказки зв'язку проєктів Minerva ↔ askDANTE: бали за кількома ознаками, кожна — з поясненням.

Ознаки (бал, максимум 100):
- код Minerva P-… у назві, номері або описах записів часу проєкту askDANTE — 100;
- правило користувача (MatchRule: домен email, слово, клієнт CRM) — 90;
- номер замовлення або Angebot проєкту Minerva у назві / номері / описах записів askDANTE — 70;
- той самий клієнт CRM — 50; та сама назва клієнта — 40; домен email клієнта — 30;
- поведінка: у дні листування по проєкту Minerva людина записувала час на цей проєкт askDANTE (≥ 2 дні) —
  15 + до 40 за частку днів;
- та сама назва — 60, схожа — до 40; спільні значущі слова — до 30.
Підказки нічого не зберігають: зв'язок ставить людина (✓ або «прийняти всі з балом ≥ N»).
"""
from __future__ import annotations

import datetime
import difflib
import re

from django.db.models import Q
from django.utils import timezone

from .mapping import _norm
from .models import DanteProject, MatchRule, TimeRecord

SHOW_FROM = 35          # нижче — не підказка
BEHAVIOUR_DAYS = 120
STOP = {"projekt", "project", "проєкт", "проект", "gmbh", "support", "allgemein", "general", "neu", "new", "test",
        "angebot", "anfrage", "auftrag", "order", "kunde", "customer", "und", "and", "für", "for", "with", "mit"}
NUMBER_RE = re.compile(r"[A-Za-z]{0,4}-?\d{3,}[\w-]*")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[^\W\d_]{4,}", (text or "").lower()) if w not in STOP}


def _domain(email: str) -> str:
    email = (email or "").lower().strip()
    return email.rsplit("@", 1)[1] if "@" in email else ""


class Context:
    """Те, що рахується один раз на сторінку: тексти проєктів askDANTE, правила, дні записів часу."""

    def __init__(self, dante_projects):
        self.dps = list(dante_projects)
        since = timezone.localdate() - datetime.timedelta(days=BEHAVIOUR_DAYS)
        texts, days = {}, {}
        for r in (TimeRecord.objects.filter(dante_project__in=self.dps)
                  .values("dante_project_id", "description", "day", "dante_user__user_id")):
            texts.setdefault(r["dante_project_id"], []).append(r["description"] or "")
            if r["dante_user__user_id"] and r["day"] >= since:
                days.setdefault(r["dante_project_id"], set()).add((r["dante_user__user_id"], r["day"]))
        self.text = {d.pk: " ".join([d.name, d.reference_number, d.customer_name] + texts.get(d.pk, [])[:200]).lower()
                     for d in self.dps}
        self.words = {d.pk: _words(f"{d.name} {d.customer_name}") for d in self.dps}
        self.norm = {d.pk: _norm(d.name) for d in self.dps}
        self.days = days
        self.rules = list(MatchRule.objects.select_related("dante_project", "customer"))


def project_facts(project) -> dict:
    """Що відомо про проєкт Minerva для зіставлення: клієнт, домени, номери, слова, дні листування."""
    from projects.models import ProjectLink
    from projects.services import project_messages
    customer = project.customer
    names = [n for n in (_norm(getattr(customer, "company", "")), _norm(getattr(customer, "name", ""))) if n]
    domains = {_domain(p.email) for p in project.participants.all() if p.role != "colleague"}
    domains.add(_domain(getattr(customer, "email", "")))
    numbers = set(ProjectLink.objects.filter(project=project, status=ProjectLink.CONFIRMED)
                  .exclude(order=None).values_list("order__order_number", flat=True))
    try:
        numbers |= set(project.offers.values_list("number", flat=True))
    except AttributeError:
        pass
    since = timezone.now() - datetime.timedelta(days=BEHAVIOUR_DAYS)
    mail_days = {(uid, timezone.localtime(at).date()) for uid, at in
                 project_messages(project).filter(sent_at__gte=since).values_list("account__user_id", "sent_at")
                 if uid and at}
    subjects = " ".join(project_messages(project).values_list("subject", flat=True)[:50])
    return {"names": names, "domains": {d for d in domains if d}, "numbers": {n for n in numbers if n},
            "words": _words(f"{project.name} {project.description or ''}"), "mail_days": mail_days,
            "text": f"{project.name} {project.description or ''} {subjects}".lower(),
            "norm": _norm(project.name)}


def score(project, facts: dict, dp: DanteProject, ctx: Context) -> tuple[int, list[str]]:
    points, why = 0, []
    text = ctx.text.get(dp.pk, "")
    if project.code and project.code.lower() in text:
        return 100, [f"код {project.code} в askDANTE"]
    for rule in ctx.rules:
        if rule.dante_project_id != dp.pk:
            continue
        if (rule.kind == MatchRule.DOMAIN and rule.value.lower().lstrip("@") in facts["domains"]) \
                or (rule.kind == MatchRule.KEYWORD and rule.value.lower() in facts["text"]) \
                or (rule.kind == MatchRule.CUSTOMER and rule.customer_id and rule.customer_id == project.customer_id):
            return 90, [f"ваше правило: {rule}"]
    nums = [n for n in facts["numbers"] if n.lower() in text]
    if nums:
        points, why = 70, [f"номер {', '.join(sorted(nums)[:3])} в askDANTE"]
    if dp.crm_customer_id and dp.crm_customer_id == project.customer_id:
        points += 50
        why.append("той самий клієнт CRM")
    elif dp.customer_name and _norm(dp.customer_name) in facts["names"]:
        points += 40
        why.append(f"клієнт «{dp.customer_name}»")
    elif dp.crm_customer_id and _domain(getattr(dp.crm_customer, "email", "")) in facts["domains"]:
        points += 30
        why.append(f"домен @{_domain(dp.crm_customer.email)}")
    mail_days = facts["mail_days"]
    common = len(mail_days & ctx.days.get(dp.pk, set()))
    if common >= 2 and mail_days:
        share = common / len(mail_days)
        points += 15 + round(40 * share)
        why.append(f"у {common} з {len(mail_days)} днів листування записано час сюди")
    ratio = difflib.SequenceMatcher(None, facts["norm"], ctx.norm.get(dp.pk, "")).ratio() if facts["norm"] else 0
    if ratio >= 0.5:
        points += 60 if ratio > 0.999 else round(40 * ratio)
        why.append("та сама назва" if ratio > 0.999 else f"схожа назва ({int(ratio * 100)}%)")
    else:
        shared = facts["words"] & ctx.words.get(dp.pk, set())
        if shared:
            points += min(30, 10 * len(shared))
            why.append("слова: " + ", ".join(sorted(shared)[:3]))
    return min(points, 99), why


def ranked(project, ctx: Context, limit: int = 3, facts: dict | None = None) -> list[tuple]:
    """[(проєкт askDANTE, бал, причини)] від найкращого; лише з балом ≥ SHOW_FROM."""
    facts = facts or project_facts(project)
    out = []
    for dp in ctx.dps:
        if dp.archived or dp.gone or not dp.time_trackable:
            continue
        s, why = score(project, facts, dp, ctx)
        if s >= SHOW_FROM:
            out.append((dp, s, why))
    out.sort(key=lambda x: -x[1])
    return out[:limit]


def best_minerva_project(dp: DanteProject, projects, ctx: Context, facts_cache: dict):
    """Найкращий проєкт Minerva для проєкту askDANTE (зворотний бік, вкладка «Проєкти askDANTE»)."""
    best = None
    for pr in projects:
        facts = facts_cache.get(pr.pk) or facts_cache.setdefault(pr.pk, project_facts(pr))
        s, why = score(pr, facts, dp, ctx)
        if s >= SHOW_FROM and (best is None or s > best[1]):
            best = (pr, s, why)
    return best
