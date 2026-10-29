"""
«📊 Огляд бази знань»: що взагалі є в базі знань, що з цього потрапило в RAG, що чекає, що не вдалося — і чому решта
не передається. Лише читання бази (лічильники одним-двома запитами на розділ), без звернень до RAG.

- листи: шлях знизу вгору як у knowledge.scope_reason (видалено → спам / чернетки → 🔒 приватне → скринька неактивна →
  скринька не вибрана → категорія не вибрана → старіші за «Індексувати від»; «🧠 Передати» обходить категорію й дату),
  потім стан у RAG (KnowledgeDoc чинної ревізії), за скриньками й категоріями, вкладення за звітами RAG;
- документи: KnowledgeItem за видами (файл, посилання, джерела, Angebote, файли замовлень, даташити) і за
  «📂 Джерелами знань», причина, чому не передано (галочка вимкнена, джерело вимкнено, немає тексту, ніхто не має доступу);
- останні помилки RAG: тема листа — лише з власних скриньок (суперкористувач — усі), документ — якщо користувач його бачить.
"""
from datetime import timedelta

from django.db.models import Count, F, Q
from django.utils import timezone

from . import knowledge, knowledge_items, retrieval

FAILURES = 20


def _states(doc_type) -> dict:
    """{obj_id: стан чинної ревізії} переданих документів (fed)."""
    from .models import KnowledgeDoc
    qs = KnowledgeDoc.objects.filter(doc_type=doc_type, fed=True)
    return {pk: (state if sr == rev else KnowledgeDoc.PENDING)
            for pk, state, sr, rev in qs.values_list("obj_id", "state", "status_revision", "revision")}


def _ids(doc_type, indexed=False):
    """Підзапит obj_id переданих документів (indexed — лише проіндексованих у чинній ревізії)."""
    from .models import KnowledgeDoc
    qs = KnowledgeDoc.objects.filter(doc_type=doc_type, fed=True)
    if indexed:
        qs = qs.filter(state=KnowledgeDoc.INDEXED, status_revision=F("revision"))
    return qs.values("obj_id")


def _tally(states) -> dict:
    out = {"fed": len(states), "indexed": 0, "pending": 0, "failed": 0, "skipped": 0}
    for state in states.values():
        out[state if state in out else "pending"] += 1
    return out


def _pct(part, whole) -> int:
    return round(100 * part / whole) if whole else 0


# ── Листи ────────────────────────────────────────────────────────────────────

def mail(scope) -> dict:
    from email_assistant.models import EmailAccount, EmailMessage
    reasons = []
    qs = EmailMessage.objects.all()
    total = qs.count()

    def step(label, excluded, hint=""):
        nonlocal qs
        n = qs.filter(excluded).count()
        if n:
            reasons.append({"label": label, "count": n, "hint": hint})
        qs = qs.exclude(excluded)

    step("видалено або в кошику", Q(is_deleted=True) | Q(folder="trash"))
    step("спам і чернетки", Q(is_spam=True) | Q(folder__in=knowledge.EXCLUDED_FOLDERS))
    step("🔒 приватна переписка", Q(thread__knowledge_excluded=True), "знімається «🧠 Передати в базу знань» у листі")
    step("скринька неактивна або без власника", Q(account__is_active=False) | Q(account__user__isnull=True))
    if scope.account_ids is not None:
        step("скринька не вибрана в налаштуваннях", ~Q(account_id__in=scope.account_ids), "«🧠 База знань» → скриньки")
    rules = qs.exclude(thread__knowledge_forced=True)
    for row in (rules.exclude(category__in=scope.categories).values("category").annotate(n=Count("pk")).order_by("-n")):
        label = knowledge.CATEGORY_LABELS.get(row["category"], row["category"] or "без категорії")
        reasons.append({"label": f"категорія «{label}» не вибрана", "count": row["n"],
                        "hint": "«🧠 База знань» → категорії, або «🧠 Передати» для окремої гілки"})
    if scope.since:
        n = rules.filter(category__in=scope.categories).filter(Q(sent_at__isnull=True) | Q(sent_at__lt=scope.since)).count()
        if n:
            reasons.append({"label": f"старіші за {timezone.localtime(scope.since):%d.%m.%Y}", "count": n,
                            "hint": "«🧠 База знань» → «Індексувати від»"})
    admitted_qs = knowledge.scope_qs(scope)
    admitted = admitted_qs.count()
    states = _states(knowledge.DOC_TYPE)
    tally = _tally(states)

    by_account = []
    fed_ids, in_ids = _ids(knowledge.DOC_TYPE), _ids(knowledge.DOC_TYPE, indexed=True)
    fed_by = dict(EmailMessage.objects.filter(pk__in=fed_ids).values_list("account_id").annotate(n=Count("pk")))
    in_by = dict(EmailMessage.objects.filter(pk__in=in_ids).values_list("account_id").annotate(n=Count("pk")))
    admitted_by = dict(admitted_qs.values_list("account_id").annotate(n=Count("pk")))
    total_by = dict(EmailMessage.objects.values_list("account_id").annotate(n=Count("pk")))
    for acc in EmailAccount.objects.select_related("user").order_by("email_address"):
        row = {"name": acc.email_address, "owner": (acc.user.get_full_name() or acc.user.username) if acc.user_id else "",
               "active": acc.is_active, "selected": scope.account_ids is None or acc.pk in scope.account_ids,
               "total": total_by.get(acc.pk, 0), "admitted": admitted_by.get(acc.pk, 0),
               "fed": fed_by.get(acc.pk, 0), "indexed": in_by.get(acc.pk, 0)}
        row["pct"] = _pct(row["indexed"], row["admitted"])
        by_account.append(row)

    by_category = []
    in_cat = dict(EmailMessage.objects.filter(pk__in=in_ids).values_list("category").annotate(n=Count("pk")))
    for category, n in admitted_qs.values_list("category").annotate(n=Count("pk")).order_by("-n"):
        by_category.append({"label": knowledge.CATEGORY_LABELS.get(category, category or "без категорії"), "admitted": n,
                            "indexed": in_cat.get(category, 0), "pct": _pct(in_cat.get(category, 0), n)})
    return {"total": total, "admitted": admitted, "not_admitted": reasons, **tally,
            "pct": _pct(tally["indexed"], tally["fed"]), "by_account": by_account, "by_category": by_category,
            "attachments": attachments()}


def attachments() -> dict:
    """Вкладення листів у RAG за звітами RAG (KnowledgeDoc.attachments чинної ревізії)."""
    from .models import KnowledgeDoc
    out = {"indexed": 0, "failed": 0, "not_indexed": {}, "unknown_docs": 0}
    rows = (KnowledgeDoc.objects.filter(doc_type=knowledge.DOC_TYPE, fed=True, state=KnowledgeDoc.INDEXED,
                                        status_revision=F("revision")).values_list("attachments", flat=True))
    for report in rows.iterator(chunk_size=2000):
        if report is None:
            out["unknown_docs"] += 1
            continue
        for a in report:
            if a.get("state") == "indexed":
                out["indexed"] += 1
            elif a.get("state") == "failed":
                out["failed"] += 1
            else:
                label = knowledge.ATTACHMENT_LABELS.get(a.get("reason"), a.get("reason") or "не прочитано")
                out["not_indexed"][label] = out["not_indexed"].get(label, 0) + 1
    out["not_indexed"] = sorted(out["not_indexed"].items(), key=lambda kv: -kv[1])
    return out


# ── Документи ────────────────────────────────────────────────────────────────

AUTO_LABELS = {"auto_offers": "Angebote", "auto_order_files": "файли замовлень", "auto_datasheets": "даташити"}


def _item_reason(item, scope, users) -> str:
    toggle = knowledge_items.AUTO_TOGGLES.get(item.kind)
    if item.auto and toggle and not getattr(scope, toggle, False):
        return f"галочка «{AUTO_LABELS.get(toggle, toggle)}» на «🧠 База знань» вимкнена"
    if item.knowledge_source_id and not item.knowledge_source.enabled:
        return "джерело знань вимкнено"
    if item.url and not item.fetched_at and not item.fetch_error and not item.file:
        return "Minerva ще завантажує сторінку"
    if item.fetch_error:
        return f"не вдалося завантажити: {item.fetch_error}"
    if not (knowledge_items.has_file(item) or item.text.strip() or item.note.strip()):
        return "немає тексту"
    if not knowledge_items.access_user_ids(item, users):
        return "ніхто з активних співробітників не має доступу"
    if not scope.enabled:
        return "індексацію вимкнено («🧠 База знань» → «Індексувати пошту»)"
    return "чекає звірки стрічки"


def documents(scope) -> dict:
    from .models import KnowledgeItem, KnowledgeSource
    items = list(KnowledgeItem.objects.select_related("knowledge_source"))
    states = _states(knowledge_items.DOC_TYPE)
    kinds = {}
    reasons = {}
    users = {}
    for item in items:
        key = item.kind
        row = kinds.setdefault(key, {"label": item.get_kind_display(), "total": 0, "fed": 0, "indexed": 0,
                                     "pending": 0, "failed": 0, "skipped": 0})
        row["total"] += 1
        state = states.get(item.pk)
        if state is None:
            reason = _item_reason(item, scope, users)
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        row["fed"] += 1
        row[state if state in row else "pending"] += 1
    for row in kinds.values():
        row["pct"] = _pct(row["indexed"], row["fed"])
    tally = _tally(states)
    sources = []
    per_source = {}
    for item in items:
        if item.knowledge_source_id:
            per = per_source.setdefault(item.knowledge_source_id, {"total": 0, "indexed": 0, "fed": 0})
            per["total"] += 1
            per["fed"] += item.pk in states
            per["indexed"] += states.get(item.pk) == "indexed"
    for source in KnowledgeSource.objects.order_by("name"):
        per = per_source.get(source.pk, {"total": 0, "indexed": 0, "fed": 0})
        sources.append({"source": source, **per, "pct": _pct(per["indexed"], per["fed"])})
    order = [k for k, _ in KnowledgeItem.KINDS]
    return {"total": len(items), **tally, "pct": _pct(tally["indexed"], tally["fed"]),
            "kinds": [kinds[k] for k in order if k in kinds],
            "not_fed": sorted(({"label": k, "count": v} for k, v in reasons.items()), key=lambda r: -r["count"]),
            "sources": sources, "supported": retrieval.documents_supported()}


# ── Помилки й разом ──────────────────────────────────────────────────────────

def failures(user) -> list:
    from email_assistant.models import EmailMessage
    from .models import KnowledgeDoc, KnowledgeItem
    docs = [d for d in KnowledgeDoc.objects.filter(fed=True, state__in=[KnowledgeDoc.FAILED, KnowledgeDoc.SKIPPED])
            .order_by("-last_status_at")[:FAILURES * 3] if d.status_revision == d.revision][:FAILURES]
    mails = {m.pk: m for m in EmailMessage.objects.filter(pk__in=[d.obj_id for d in docs if d.doc_type == "email"])
             .select_related("account")}
    items = {i.pk: i for i in KnowledgeItem.objects.filter(pk__in=[d.obj_id for d in docs if d.doc_type == "document"])
             .select_related("knowledge_source")}
    out = []
    for d in docs:
        row = {"state": d.get_state_display(), "error": d.error or ("RAG пропустив — немає тексту" if d.state == "skipped" else ""),
               "at": d.last_status_at}
        if d.doc_type == "email":
            m = mails.get(d.obj_id)
            own = m is not None and (user.is_superuser or m.account.user_id == user.pk)
            row.update(kind="✉️", title=(m.subject or "(без теми)") if own else f"лист #{d.obj_id}",
                       url=f"/email/message/{d.obj_id}/" if own else "")
        else:
            i = items.get(d.obj_id)
            seen = i is not None and knowledge_items.can_view(user, i)
            row.update(kind="📎", title=i.title if seen else f"документ #{d.obj_id}",
                       url=f"/rag/knowledge/{d.obj_id}/" if seen else "")
        out.append(row)
    return out


def rag_state() -> dict:
    from .models import KnowledgeActivity, KnowledgeSettings
    s = KnowledgeSettings.get()
    act = KnowledgeActivity.objects.filter(pk=1).first()
    last = act.last_call_at if act else None
    return {"enabled": s.enabled, "instance": knowledge.instance_id(), "paused": s.feed_paused,
            "last_call_at": last, "silent": bool(last and timezone.now() - last > timedelta(minutes=30)),
            "rag_types": (act.rag_types if act else []) or [], "documents_supported": retrieval.documents_supported()}


def build(user) -> dict:
    scope = knowledge.get_scope()
    m, d = mail(scope), documents(scope)
    return {"rag": rag_state(), "mail": m, "documents": d, "failures": failures(user),
            "totals": {"items": m["fed"] + d["fed"], "indexed": m["indexed"] + d["indexed"],
                       "pending": m["pending"] + d["pending"], "failed": m["failed"] + d["failed"],
                       "skipped": m["skipped"] + d["skipped"],
                       "pct": _pct(m["indexed"] + d["indexed"], m["fed"] + d["fed"])},
            "built_at": timezone.now()}
