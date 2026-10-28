"""Проєкти з пошти: створення з листування, прив'язки, учасники, хронологія, підказки для нових листів.

Правило: автоматика ніколи не перебиває рішення людини — відхилений зв'язок не пропонується знову,
підтверджений не знімається автоматично.
"""
import logging
import re
from collections import Counter
from datetime import datetime

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import DismissedThread, Project, ProjectLink, ProjectParticipant

logger = logging.getLogger(__name__)

# Код проєкту в темі/тексті листа: P-2026-003
CODE_RE = re.compile(r"\bP-(\d{4})-(\d{3,})\b", re.IGNORECASE)
SKIP_CATEGORIES = ("newsletter",)


def _addr(a) -> str:
    return (a or "").strip().lower()


def _thread_messages(thread):
    from email_assistant.models import EmailMessage
    return EmailMessage.objects.filter(thread=thread, is_deleted=False).order_by("sent_at")


# ── Учасники ──────────────────────────────────────────────────────────────────

def participants_from_messages(messages, own=None, customer=None) -> dict:
    """{email: {name, role, customer}} з адрес листів: свої — колеги, клієнти CRM / їхній домен, постачальники."""
    from crm.models import Customer
    from django.db.models.functions import Lower
    from email_assistant.crm_link import FREE_MAIL_DOMAINS, is_own, own_addresses
    from inventory.models import Supplier

    own = own or own_addresses()
    names, order = {}, []
    for m in messages:
        pairs = [(m.from_email, m.from_name)] + [(a, "") for a in list(m.to_emails or []) + list(m.cc_emails or [])]
        for address, name in pairs:
            a = _addr(address)
            if not a or "@" not in a:
                continue
            if a not in names:
                order.append(a)
                names[a] = ""
            if name and not names[a]:
                names[a] = name.strip()[:200]

    customers = {c.le: c for c in Customer.objects.annotate(le=Lower("email")).filter(le__in=order)}
    suppliers = set(Supplier.objects.annotate(le=Lower("email")).filter(le__in=order).values_list("le", flat=True))
    customer_domains = set()
    for c in [customer] + list(customers.values()):
        if c is not None and c.email and "@" in c.email:
            customer_domains.add(c.email.lower().split("@", 1)[1])
    customer_domains -= FREE_MAIL_DOMAINS

    out = {}
    for a in order:
        if is_own(a, own):
            role, cust = ProjectParticipant.COLLEAGUE, None
        elif a in customers:
            role, cust = ProjectParticipant.CUSTOMER, customers[a]
        elif a in suppliers:
            role, cust = ProjectParticipant.SUPPLIER, None
        elif a.split("@", 1)[1] in customer_domains:
            role, cust = ProjectParticipant.CUSTOMER, customer
        else:
            role, cust = ProjectParticipant.OTHER, None
        out[a] = {"name": names[a], "role": role, "customer": cust}
    return out


def add_participants(project, messages, own=None) -> int:
    """Додати нових учасників (наявних не змінює — роль могла бути виправлена вручну)."""
    found = participants_from_messages(messages, own=own, customer=project.customer)
    have = set(project.participants.values_list("email", flat=True))
    new = [ProjectParticipant(project=project, email=a, name=i["name"], role=i["role"], customer=i["customer"])
           for a, i in found.items() if a not in have]
    ProjectParticipant.objects.bulk_create(new, ignore_conflicts=True)
    return len(new)


# ── Створення й прив'язки ─────────────────────────────────────────────────────

def suggest_name(threads) -> str:
    """Назва нового проєкту — найчастіша тема гілок без Re:/AW:/WG:."""
    from email_assistant.sync import _REPLY_PREFIX_RE
    subjects = []
    for t in threads:
        s = t.subject or ""
        while True:
            stripped = _REPLY_PREFIX_RE.sub("", s, count=1)
            if stripped == s:
                break
            s = stripped
        s = " ".join(s.split())
        if s:
            subjects.append(s)
    return Counter(subjects).most_common(1)[0][0][:200] if subjects else "Новий проєкт"


@transaction.atomic
def create_project(*, name="", user=None, customer=None, owner=None, threads=(), status=Project.ACTIVE,
                   description="") -> Project:
    threads = list(threads)
    if customer is None:
        # клієнта гілки ставить crm_link запитом у БД — об'єкти в пам'яті можуть бути застарілими
        from email_assistant.models import EmailThread
        ids = [i for i in EmailThread.objects.filter(pk__in=[t.pk for t in threads])
               .values_list("crm_customer_id", flat=True) if i]
        if ids:
            from crm.models import Customer
            customer = Customer.objects.filter(pk=Counter(ids).most_common(1)[0][0]).first()
    project = Project.objects.create(
        name=(name or suggest_name(threads)).strip()[:200], customer=customer, owner=owner or user,
        status=status, description=description, created_by=user,
        source_thread=threads[0] if threads else None)
    for t in threads:
        link_thread(project, t, user=user, reason="проєкт створено з цього листування")
    return project


def link_thread(project, thread, *, user=None, source=ProjectLink.MANUAL, status=ProjectLink.CONFIRMED,
                reason="") -> ProjectLink:
    """Прив'язати гілку. Вручну — підтверджує навіть відхилену; автоматично — не чіпає рішень людини."""
    link, created = ProjectLink.objects.get_or_create(
        project=project, thread=thread,
        defaults={"source": source, "status": status, "reason": reason[:255], "created_by": user})
    if not created and link.status != status:
        manual = source == ProjectLink.MANUAL
        upgrade = link.status == ProjectLink.SUGGESTED and status == ProjectLink.CONFIRMED
        if manual or upgrade:
            link.status, link.source = status, source
            link.reason = reason[:255] or link.reason
            link.decided_by, link.decided_at = user, timezone.now()
            link.save(update_fields=["status", "source", "reason", "decided_by", "decided_at"])
    if link.status == ProjectLink.CONFIRMED:
        _on_confirmed(project, thread)
    return link


def link_order(project, order, *, user=None, reason="") -> ProjectLink:
    link, _ = ProjectLink.objects.get_or_create(
        project=project, order=order,
        defaults={"source": ProjectLink.MANUAL, "status": ProjectLink.CONFIRMED, "reason": reason[:255],
                  "created_by": user})
    return link


def decide(link, accept: bool, user=None) -> ProjectLink:
    link.status = ProjectLink.CONFIRMED if accept else ProjectLink.REJECTED
    link.decided_by, link.decided_at = user, timezone.now()
    link.save(update_fields=["status", "decided_by", "decided_at"])
    if accept and link.thread_id:
        _on_confirmed(link.project, link.thread)
    elif link.thread_id:
        _knowledge_touch(link.thread_id)
    return link


def unlink(link):
    """Прибрати зв'язок. Автоматичний — лишається відхиленим, щоб система не запропонувала його знову."""
    if link.source == ProjectLink.MANUAL:
        thread_id = link.thread_id
        link.delete()
        if thread_id:
            _knowledge_touch(thread_id)
    else:
        decide(link, False)


def dismiss_threads(threads, user=None) -> int:
    n = 0
    for t in threads:
        _, created = DismissedThread.objects.get_or_create(thread=t, defaults={"dismissed_by": user})
        n += created
    return n


def _knowledge_touch(thread_id):
    """Проєкти гілки входять у документ бази знань (поле projects) → нова ревізія для RAG."""
    from email_assistant.knowledge_hook import touch
    touch(thread_id=thread_id)


def _on_confirmed(project, thread):
    _knowledge_touch(thread.pk)
    add_participants(project, list(_thread_messages(thread)))
    DismissedThread.objects.filter(thread=thread).delete()
    fields = ["updated_at"]
    if project.customer_id is None:
        from email_assistant.models import EmailThread
        cid = EmailThread.objects.filter(pk=thread.pk).values_list("crm_customer_id", flat=True).first()
        if cid:
            project.customer_id = cid
            fields.append("customer")
    project.save(update_fields=fields)


# ── Що належить проєкту ───────────────────────────────────────────────────────

def project_messages(project):
    """Листи проєкту: усі листи підтверджених гілок + окремо прив'язані листи."""
    from email_assistant.models import EmailMessage
    confirmed = ProjectLink.objects.filter(project=project, status=ProjectLink.CONFIRMED)
    return EmailMessage.objects.filter(
        Q(thread_id__in=confirmed.exclude(thread=None).values("thread_id"))
        | Q(pk__in=confirmed.exclude(message=None).values("message_id")),
        is_deleted=False)


def thread_projects(thread) -> dict:
    """Для прев'ю листа: підтверджені проєкти гілки і пропозиції системи (без відхилених)."""
    if thread is None:
        return {"confirmed": [], "suggested": []}
    links = list(ProjectLink.objects.filter(Q(thread=thread) | Q(message__thread=thread))
                 .exclude(status=ProjectLink.REJECTED).select_related("project").order_by("created_at"))
    return {"confirmed": [l for l in links if l.status == ProjectLink.CONFIRMED],
            "suggested": [l for l in links if l.status == ProjectLink.SUGGESTED]}


def candidate_projects(thread=None, limit=50) -> list:
    """Відкриті проєкти для «Додати до проєкту»: спершу проєкти клієнта цієї гілки."""
    qs = Project.objects.filter(status__in=Project.OPEN_STATUSES).select_related("customer")
    customer_id = getattr(thread, "crm_customer_id", None)
    projects = list(qs.order_by("-updated_at")[:limit])
    if customer_id:
        projects.sort(key=lambda p: p.customer_id != customer_id)
    return projects


def timeline(project, limit=300) -> list:
    """Хронологія проєкту: листи (📥/📤) і замовлення — нові зверху."""
    from email_assistant.crm_link import is_own, own_addresses
    own = own_addresses()
    events = []
    for m in project_messages(project).select_related("thread").order_by("-sent_at")[:limit]:
        out = is_own(m.from_email, own)
        events.append({
            "date": m.sent_at, "icon": "📤" if out else "📥",
            "title": m.subject or "(без теми)",
            "who": (", ".join((m.to_emails or [])[:2]) if out else (m.from_name or m.from_email)),
            "url": f"/email/thread/{m.thread_id}/" if m.thread_id else f"/email/message/{m.pk}/",
        })
    for link in (ProjectLink.objects.filter(project=project, status=ProjectLink.CONFIRMED)
                 .exclude(order=None).select_related("order")):
        o = link.order
        status = o.get_status_display() if hasattr(o, "get_status_display") else ""
        events.append({
            "date": timezone.make_aware(datetime.combine(o.order_date, datetime.min.time())) if o.order_date else None,
            "icon": "🛒", "title": f"{o.source}:{o.order_number}", "who": status,
            "url": f"/admin/sales/salesorder/{o.pk}/change/",
        })
    events.sort(key=lambda e: e["date"] or timezone.now(), reverse=True)
    return events


def tags_for_threads(thread_ids) -> dict:
    """{thread_id: [(code, pk, name, icon)]} — підтверджені проєкти для міток у списку листів."""
    out = {}
    for tid, code, pk, name, icon in (ProjectLink.objects
                                      .filter(thread_id__in=thread_ids, status=ProjectLink.CONFIRMED)
                                      .values_list("thread_id", "project__code", "project_id", "project__name",
                                                   "project__icon")):
        out.setdefault(tid, []).append((code, pk, name, icon))
    return out


# ── Підказки для нового листа ─────────────────────────────────────────────────

def suggest_for_message(msg) -> list:
    """
    Новий лист: якщо його гілка ще не в проєкті — знайти, до якого проєкту він може належати.
    1) код проєкту в темі/тексті → підтверджено (правило);
    2) замовлення з листа прив'язане до проєкту → пропозиція;
    3) та сама тема (без Re:/AW:) і зовнішній учасник проєкту → пропозиція;
    4) відправник — учасник рівно одного відкритого проєкту → пропозиція;
    5) у клієнта рівно один відкритий проєкт (лише категорія «Клієнти») → пропозиція.
    Гілка, вже підтверджена в проєкті, просто поповнює учасників.
    """
    from email_assistant.categorize import counterparts
    from email_assistant.crm_link import own_addresses
    from email_assistant.sync import normalize_subject

    from email_assistant.models import EmailThread
    # свіжа гілка з БД: клієнта гілки щойно поставив crm_link (запитом update)
    thread = EmailThread.objects.filter(pk=msg.thread_id).select_related("crm_customer").first()         if msg.thread_id else None
    if thread is None or msg.is_spam or msg.is_deleted or msg.folder in ("spam", "trash") \
            or msg.category in SKIP_CATEGORIES:
        return []
    own = own_addresses()
    existing = {l.project_id: l for l in ProjectLink.objects.filter(thread=thread).select_related("project")}
    confirmed = [l for l in existing.values() if l.status == ProjectLink.CONFIRMED]
    if confirmed:
        for l in confirmed:
            add_participants(l.project, [msg], own=own)
        return []
    if DismissedThread.objects.filter(thread=thread).exists():
        return []

    open_projects = Project.objects.filter(status__in=Project.OPEN_STATUSES)
    found = []   # (project, source, status, reason) — сильніші правила першими
    text = f"{msg.subject or ''}\n{(msg.body_text or '')[:20000]}"
    codes = {f"P-{y}-{n}" for y, n in CODE_RE.findall(text)}
    if codes:
        for p in open_projects.filter(code__in=codes):
            found.append((p, ProjectLink.RULE, ProjectLink.CONFIRMED, f"код {p.code} у листі"))

    from crm.models import CustomerTimeline
    order_ids = list(CustomerTimeline.objects.filter(email_message=msg).exclude(related_order_id=None)
                     .values_list("related_order_id", flat=True))
    if order_ids:
        for l in (ProjectLink.objects.filter(order_id__in=order_ids, status=ProjectLink.CONFIRMED,
                                             project__status__in=Project.OPEN_STATUSES)
                  .select_related("project", "order")):
            found.append((l.project, ProjectLink.AUTO, ProjectLink.SUGGESTED,
                          f"замовлення {l.order.source}:{l.order.order_number} проєкту"))

    external = set(counterparts(msg, own))
    core = normalize_subject(msg.subject)
    if len(core) >= 3 and external:
        for l in (ProjectLink.objects.filter(status=ProjectLink.CONFIRMED, thread__isnull=False,
                                             project__status__in=Project.OPEN_STATUSES,
                                             thread__subject__icontains=core[:100])
                  .exclude(thread=thread).select_related("project", "thread")[:30]):
            if normalize_subject(l.thread.subject) != core:
                continue
            members = set(l.project.participants.values_list("email", flat=True))
            if external & members:
                found.append((l.project, ProjectLink.AUTO, ProjectLink.SUGGESTED, "та сама тема й учасник проєкту"))

    if not found and external:
        ids = set(ProjectParticipant.objects.filter(email__in=external, project__status__in=Project.OPEN_STATUSES)
                  .exclude(role=ProjectParticipant.COLLEAGUE).values_list("project_id", flat=True))
        if len(ids) == 1:
            found.append((open_projects.get(pk=ids.pop()), ProjectLink.AUTO, ProjectLink.SUGGESTED,
                          "відправник — учасник проєкту"))

    if not found and thread.crm_customer_id and msg.category == "customer":
        only = list(open_projects.filter(customer_id=thread.crm_customer_id)[:2])
        if len(only) == 1:
            found.append((only[0], ProjectLink.AUTO, ProjectLink.SUGGESTED, "єдиний відкритий проєкт клієнта"))

    made, seen = [], set()
    for project, source, status, reason in found:
        if project.pk in seen:
            continue
        seen.add(project.pk)
        prior = existing.get(project.pk)
        if prior is not None and not (prior.status == ProjectLink.SUGGESTED and status == ProjectLink.CONFIRMED):
            continue   # уже запропоновано або людина відхилила — не повторювати
        made.append(link_thread(project, thread, source=source, status=status, reason=reason))
    return made
