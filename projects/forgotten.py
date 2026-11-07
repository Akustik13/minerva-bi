"""🔎 Забуті листи проєкту (етап 5): гілки з власних скриньок людини, які стосуються проєкту, але не прив'язані.

local — без AI й RAG: адреси учасників проєкту й клієнта, домен клієнта, клієнт CRM гілки, код P-…, номери замовлень,
закупівель і JLC, трек-номери, артикули товарів проєкту в темі й тексті → бали й причини, найкращі першими.
rag_start / rag_rows — пошук у базі знань (rag_assistant.retrieval: лише власні скриньки, фрагменти перевірені), якщо
людина має право «🔎 пошук у базі знань». Не пропонуються гілки з рішенням людини для цього проєкту (підтверджені
й відхилені), «не стосується проєктів» (DismissedThread), спам і кошик; запропоновані системою — з позначкою.
decide: ✓ — прив'язати гілку
(services.link_thread), ✗ — відхилити (більше не пропонується ні тут, ні в підказках прив'язок).
"""
import logging
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import DismissedThread, ProjectLink, ProjectParticipant

logger = logging.getLogger(__name__)

DAYS = 730                  # шукати в листах за два роки
LIMIT = 15                  # рядків у результаті
MIN_SCORE = 40
SCAN_MAX = 500              # листів-кандидатів на оцінку
RAG_HITS = 20
W_CODE, W_ORDER, W_TRACK, W_PO, W_SKU = 100, 70, 70, 60, 40
W_PERSON, W_CUSTOMER, W_DOMAIN, W_ELSEWHERE = 30, 25, 20, -30


class ForgottenError(ValueError):
    pass


def _add(tokens, value, weight, label, min_len):
    v = " ".join(str(value or "").split())
    if len(v) >= min_len and v.lower() not in tokens:
        tokens[v.lower()] = (weight, label)


def signals(project) -> tuple:
    """(люди {адреса: ім'я}, домени, токени {текст: (вага, причина)}) — за чим лист стосується проєкту."""
    from email_assistant.crm_link import FREE_MAIL_DOMAINS, is_own, own_addresses
    own = own_addresses()
    people = {}
    for email, name in (ProjectParticipant.objects.filter(project=project)
                        .exclude(role=ProjectParticipant.COLLEAGUE).values_list("email", "name")):
        e = (email or "").strip().lower()
        if e and not is_own(e, own):
            people[e] = name or e
    domains = set()
    customer = project.customer if project.customer_id else None
    if customer is not None and customer.email:
        e = customer.email.strip().lower()
        if not is_own(e, own):
            people.setdefault(e, customer.name or e)
            d = e.rpartition("@")[2]
            if d and d not in FREE_MAIL_DOMAINS:
                domains.add(d)
    tokens = {}
    _add(tokens, project.code, W_CODE, f"код {project.code}", 6)
    for link in (ProjectLink.objects.filter(project=project, status=ProjectLink.CONFIRMED)
                 .select_related("order", "purchase_order", "jlc_order", "shipment")):
        if link.order_id:
            _add(tokens, link.order.order_number, W_ORDER, f"замовлення {link.order.order_number}", 5)
            _add(tokens, getattr(link.order, "tracking_number", ""), W_TRACK, "трек-номер замовлення", 8)
        if link.purchase_order_id:
            po = link.purchase_order
            _add(tokens, po.code, W_PO, f"закупівля {po.code}", 4)
            _add(tokens, po.supplier_order_no, W_PO, f"номер постачальника {po.supplier_order_no}", 5)
            _add(tokens, po.tracking_number, W_TRACK, "трек-номер закупівлі", 8)
        if link.jlc_order_id:
            _add(tokens, link.jlc_order.jlc_order_number, W_PO, f"JLC {link.jlc_order.jlc_order_number}", 5)
            _add(tokens, getattr(link.jlc_order, "tracking_number", ""), W_TRACK, "трек-номер JLC", 8)
        if link.shipment_id:
            _add(tokens, link.shipment.tracking_number, W_TRACK, "трек-номер відправлення", 8)
    for sku in project.products.values_list("sku", flat=True):
        _add(tokens, sku, W_SKU, f"артикул {sku}", 4)
    return people, domains, tokens


def _skip(project, thread_ids) -> tuple:
    """(гілки поза пошуком: з рішенням людини для цього проєкту — підтверджені чи відхилені — і «не стосується
    проєктів», {гілка: код іншого проєкту, де її підтвердили}, гілки, які система вже запропонувала цьому проєкту)."""
    ids = list(thread_ids)
    links = ProjectLink.objects.filter(project=project)
    decided = links.exclude(status=ProjectLink.SUGGESTED)
    skip = set(decided.filter(thread_id__in=ids).values_list("thread_id", flat=True))
    skip |= set(decided.filter(message__thread_id__in=ids).values_list("message__thread_id", flat=True))
    skip |= set(DismissedThread.objects.filter(thread_id__in=ids).values_list("thread_id", flat=True))
    suggested = set(links.filter(status=ProjectLink.SUGGESTED, thread_id__in=ids).values_list("thread_id", flat=True))
    elsewhere = {}
    for tid, code in (ProjectLink.objects.filter(thread_id__in=ids, status=ProjectLink.CONFIRMED)
                      .exclude(project=project).values_list("thread_id", "project__code")):
        elsewhere.setdefault(tid, code)
    return skip, elsewhere, suggested


def _own_base(user, days=DAYS):
    from email_assistant.models import EmailMessage
    since = timezone.now() - timedelta(days=days)
    return (EmailMessage.objects.filter(account__user=user, is_deleted=False, is_spam=False, sent_at__gte=since)
            .exclude(folder=EmailMessage.FOLDER_TRASH).exclude(thread=None))


def _row(thread, reasons, score, last) -> dict:
    when = timezone.localtime(last.sent_at) if last is not None and last.sent_at else None
    return {"thread": thread.pk, "subject": thread.subject or "(без теми)", "url": f"/email/thread/{thread.pk}/",
            "date": when.strftime("%d.%m.%Y") if when else "", "sort": when.isoformat() if when else "",
            "from": (last.from_name or last.from_email) if last is not None else "",
            "reasons": reasons, "score": score}


def local(project, user, limit=LIMIT) -> list:
    """Гілки з власних скриньок, що стосуються проєкту за учасниками, номерами й артикулами (без AI)."""
    if user is None or not getattr(user, "pk", None):
        return []
    people, domains, tokens = signals(project)
    cond = Q()
    for e in people:
        cond |= Q(from_email__iexact=e) | Q(to_emails__icontains=e) | Q(cc_emails__icontains=e)
    for d in domains:
        cond |= Q(from_email__iendswith="@" + d)
    for t in tokens:
        cond |= Q(subject__icontains=t) | Q(body_text__icontains=t)
    if project.customer_id:
        cond |= Q(thread__crm_customer_id=project.customer_id)
    if not cond:
        return []
    msgs = list(_own_base(user).filter(cond).select_related("thread").order_by("-sent_at", "-pk")[:SCAN_MAX])
    skip, elsewhere, suggested = _skip(project, {m.thread_id for m in msgs})
    found = {}
    for m in msgs:
        if m.thread_id in skip:
            continue
        reasons = found.setdefault(m.thread_id, {"thread": m.thread, "last": m, "why": {}})["why"]
        hay = f"{m.subject or ''}\n{m.body_text or ''}".lower()
        addrs = {(m.from_email or "").lower()} | {str(a).lower() for a in (m.to_emails or []) + (m.cc_emails or [])}
        for e, name in people.items():
            if any(e in a for a in addrs):
                reasons.setdefault(f"person:{e}", (W_PERSON, f"листування з {name}"))
        for d in domains:
            if (m.from_email or "").lower().endswith("@" + d):
                reasons.setdefault(f"domain:{d}", (W_DOMAIN, f"домен {d}"))
        for t, (w, label) in tokens.items():
            if t in hay:
                reasons.setdefault(f"token:{t}", (w, label))
        if project.customer_id and m.thread.crm_customer_id == project.customer_id:
            reasons.setdefault("customer", (W_CUSTOMER, "клієнт проєкту"))
    rows = []
    for tid, f in found.items():
        why = dict(f["why"])
        if tid in elsewhere:
            why["elsewhere"] = (W_ELSEWHERE, f"уже в проєкті {elsewhere[tid]}")
        score = sum(w for w, _ in why.values())
        if tid in suggested:
            why["suggested"] = (0, "❔ система вже пропонувала цю гілку")
        if score >= MIN_SCORE:
            rows.append(_row(f["thread"], [label for _, label in sorted(why.values(), key=lambda x: -x[0])],
                             score, f["last"]))
    rows.sort(key=lambda r: (r["score"], r["sort"]), reverse=True)     # більший бал, потім новіші
    return rows[:limit]


def rag_ready(user) -> bool:
    try:
        from rag_assistant import retrieval
        return bool(user is not None and retrieval.available_for(user))
    except Exception as e:  # noqa: BLE001 — без бази знань пошук однаково працює
        logger.warning("project forgotten rag_ready: %s", e)
        return False


def rag_start(project, user):
    """Пошук у базі знань (без цього проєкту гілок) → RetrievalJob або None (RAG недоступний людині)."""
    if not rag_ready(user):
        return None
    from rag_assistant import retrieval
    from rag_assistant.entity_help import build_query
    linked = set(ProjectLink.objects.filter(project=project).exclude(thread=None).values_list("thread_id", flat=True))
    return retrieval.start(user, build_query("project", project), purpose="project_forgotten",
                           customer_id=project.customer_id or None, exclude_thread_ids=linked, limit=RAG_HITS,
                           documents=False)


def rag_rows(project, user, job) -> list:
    """Перевірені фрагменти завершеного пошуку → рядки гілок (без пов'язаних із проєктом і «не стосується»)."""
    from email_assistant.models import EmailMessage
    from email_assistant.quotes import tidy
    hits = [h for h in (job.hits or []) if h.get("kind") == "email" and h.get("thread_id")]
    if not hits:
        return []
    skip, elsewhere, suggested = _skip(project, {h["thread_id"] for h in hits})
    msgs = {m.pk: m for m in EmailMessage.objects.filter(pk__in=[h["message_pk"] for h in hits], account__user=user)
            .select_related("thread")}
    rows, seen = [], set()
    for h in hits:
        m = msgs.get(h.get("message_pk"))
        if m is None or m.thread_id in skip or m.thread_id in seen:
            continue
        seen.add(m.thread_id)
        snippet = " ".join(tidy(h.get("text") or "").split())[:160]
        reasons = [f"🔎 база знань: «{snippet}…»" if snippet else "🔎 база знань"]
        if m.thread_id in elsewhere:
            reasons.append(f"уже в проєкті {elsewhere[m.thread_id]}")
        if m.thread_id in suggested:
            reasons.append("❔ система вже пропонувала цю гілку")
        rows.append(_row(m.thread, reasons, 0, m))
    return rows[:LIMIT]


def decide(project, user, thread_id, accept) -> str:
    """✓ — гілка до проєкту; ✗ — не стосується цього проєкту (більше не пропонується)."""
    from email_assistant.models import EmailThread

    from . import services
    thread = EmailThread.objects.filter(pk=thread_id, account__user=user).first() if user is not None else None
    if thread is None:
        raise ForgottenError("Гілку не знайдено у ваших скриньках")
    if accept:
        services.link_thread(project, thread, user=user, source=ProjectLink.MANUAL, status=ProjectLink.CONFIRMED,
                             reason="🔎 знайдено серед забутих листів")
        return f"✉️ Гілку додано до проєкту: {thread.subject or '(без теми)'}"
    services.link_thread(project, thread, user=user, source=ProjectLink.MANUAL, status=ProjectLink.REJECTED,
                         reason="🔎 не стосується проєкту")
    return "✕ Більше не пропонуватиму цю гілку"
