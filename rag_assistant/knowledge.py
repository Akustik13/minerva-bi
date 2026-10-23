"""База знань: пошта Minerva → стрічка змін для RAG.

Контракт: api/schemas/knowledge (JSON-схеми, README), integration/API_CONTRACT.md (draft 0.3).
Гарантії:
- записувачі журналу серіалізовані: journal_writer() бере pg_advisory_xact_lock ДО видачі seq і тримає до commit,
  тож порядок seq = порядок commit (на SQLite запис і так серіалізований);
- KnowledgeDoc (revision, feed_version, fed) і подія KnowledgeChange пишуться в одній транзакції;
- revision монотонна, рядок KnowledgeDoc не видаляється;
- snapshot: snapshot_seq і склад — з одного REPEATABLE READ знімка.
Стан документа завжди будується з поточних даних; якщо лист змінено без сигналу (масовий .update()),
читання стрічки/документа спершу записує нову ревізію (самовиправлення), а нічна звірка (knowledge_refresh) — решту.
"""
import copy
import hashlib
import json
import logging
import os
import re
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone as dt_tz

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Max, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "minerva-knowledge/1.0-draft"
DOC_TYPE = "email"
LOCK_KEY = 0x4B4E4F57           # pg_advisory_xact_lock для записувачів журналу
TEXT_MAX = 100_000
TEXT_NEW_MAX = 20_000
CHANGE_RETENTION_DAYS = 30
SNAPSHOT_TTL = timedelta(hours=24)
REFRESH_EVERY = timedelta(hours=24)
PAUSE_RETRY_SECONDS = 60        # «⏸ Пауза»: через скільки RAG варто спробувати знову (429 rate_limited)
RAG_SILENT_AFTER = timedelta(minutes=5)   # RAG опитує стрічку кожні кілька секунд — довша тиша означає збій
EXCLUDED_FOLDERS = ("trash", "spam", "draft")
# Збереження листа лише з цими полями не змінює документ
IRRELEVANT_FIELDS = frozenset({"is_read", "is_starred", "ai_summary", "ai_reply_draft", "ai_translated",
                               "ai_translate_to", "imap_uid", "imap_folder_name"})
_INSTANCE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_DOC_ID_RE = re.compile(r"^email:([1-9][0-9]{0,18})$")


class KnowledgeError(Exception):
    """Помилка в форматі контракту: {code, message, retryable, request_id, details}."""

    def __init__(self, code, status, message, retryable=False, details=None):
        super().__init__(message)
        self.code, self.status, self.message, self.retryable, self.details = code, status, message, retryable, details


# ── Середовище й налаштування ────────────────────────────────────────────────

def instance_id() -> str:
    """Назва середовища для RAG: змінна MINERVA_INSTANCE_ID, інакше поле в «🧠 База знань»."""
    value = (getattr(settings, "MINERVA_INSTANCE_ID", "") or "").strip()
    if not value:
        from .models import KnowledgeSettings
        value = (KnowledgeSettings.objects.filter(pk=1).values_list("instance_id", flat=True).first() or "").strip()
    return value if _INSTANCE_RE.match(value) else ""


def instance_id_source() -> str:
    """Звідки взято назву: env / settings / ''."""
    if (getattr(settings, "MINERVA_INSTANCE_ID", "") or "").strip():
        return "env"
    return "settings" if instance_id() else ""


@dataclass
class Scope:
    enabled: bool
    account_ids: set
    categories: set
    since: datetime
    include_attachments: bool
    attachment_types: set
    attachment_max: int
    generation: str


def get_scope():
    from .models import KnowledgeSettings
    s = KnowledgeSettings.get()
    since = timezone.make_aware(datetime.combine(s.index_from, dtime.min)) if s.index_from else None
    return Scope(
        enabled=s.enabled,
        account_ids=set(s.accounts.values_list("pk", flat=True)) or None,
        categories=set(s.categories or []),
        since=since,
        include_attachments=s.include_attachments,
        attachment_types={t.strip().lower().lstrip(".") for t in (s.attachment_types or []) if t.strip()},
        attachment_max=int(s.attachment_max_mb or 0) * 1024 * 1024,
        generation=str(s.generation),
    )


def _messages_qs():
    from email_assistant.models import EmailMessage
    return EmailMessage.objects.select_related("account", "thread", "thread__crm_customer")


def scope_reason(msg, scope) -> str:
    """'' — лист допущено до індексу (без урахування паузи); інакше причина для tombstone."""
    if msg.is_deleted or msg.folder == "trash":
        return "deleted"
    if msg.is_spam or msg.folder in EXCLUDED_FOLDERS:
        return "out_of_scope"
    if msg.thread_id and msg.thread.knowledge_excluded:
        return "excluded"
    if not msg.account.is_active or not msg.account.user_id:
        return "access_revoked"
    if scope.account_ids is not None and msg.account_id not in scope.account_ids:
        return "out_of_scope"
    if msg.category not in scope.categories:
        return "out_of_scope"
    if scope.since and (not msg.sent_at or msg.sent_at < scope.since):
        return "out_of_scope"
    return ""


def scope_qs(scope):
    """Листи, допущені до індексу (DB-версія scope_reason)."""
    from email_assistant.models import EmailMessage
    qs = (EmailMessage.objects.filter(is_deleted=False, is_spam=False, account__is_active=True,
                                      account__user__isnull=False, category__in=scope.categories)
          .exclude(folder__in=EXCLUDED_FOLDERS).exclude(thread__knowledge_excluded=True))
    if scope.account_ids is not None:
        qs = qs.filter(account_id__in=scope.account_ids)
    if scope.since:
        qs = qs.filter(sent_at__gte=scope.since)
    return qs


# ── Документ ─────────────────────────────────────────────────────────────────

def _iso(dt):
    if dt is None:
        return None
    return dt.astimezone(dt_tz.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def _attachment_path(att) -> str:
    rel = att.get("file_path") or ""
    if not rel:
        return ""
    path = os.path.normpath(os.path.join(str(settings.MEDIA_ROOT), rel))
    root = os.path.normpath(str(settings.MEDIA_ROOT))
    return path if path.startswith(root + os.sep) and os.path.isfile(path) else ""


def _attachments(msg, scope) -> list:
    """Вкладення для документа. Хеш байтів рахується один раз і кешується в EmailMessage.attachments."""
    from email_assistant.models import EmailMessage
    raw = copy.deepcopy(list(msg.attachments or []))
    out, cache_changed = [], False
    for i, a in enumerate(raw):
        if a.get("is_inline"):
            continue
        name = (a.get("name") or "attachment")[:300]
        size = int(a.get("size") or 0)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        path = _attachment_path(a)
        reason = ""
        if not scope.include_attachments:
            reason = "attachments_disabled"
        elif ext not in scope.attachment_types:
            reason = "type_not_allowed"
        elif size > scope.attachment_max:
            reason = "too_large"
        elif not path:
            reason = "not_downloaded"
        content_hash = a.get("sha256") or None
        if not reason and not content_hash:
            content_hash = _sha_file(path)
            a["sha256"] = content_hash
            cache_changed = True
        entry = {"index": i, "name": name, "content_type": (a.get("content_type") or "application/octet-stream")[:200],
                 "size": size, "content_hash": content_hash, "indexable": not reason}
        if reason:
            entry["reason"] = reason
        out.append(entry)
    if cache_changed:   # без сигналу: кеш хешу не змінює лист
        EmailMessage.objects.filter(pk=msg.pk).update(attachments=raw)
        msg.attachments = raw
    return out


def _batch_extras(messages) -> dict:
    """Замовлення, клієнт із CRM-хронології і проєкти для пачки листів — кількома запитами."""
    from crm.models import CustomerTimeline
    messages = list(messages)
    pks = [m.pk for m in messages]
    thread_ids = {m.thread_id for m in messages if m.thread_id}
    extras = {pk: {"orders": [], "customer": None, "projects": {}} for pk in pks}
    order_ids = set()
    for mid, oid, cid, cname in (CustomerTimeline.objects.filter(email_message_id__in=pks)
                                 .values_list("email_message_id", "related_order_id", "customer_id", "customer__name")):
        if oid:
            extras[mid]["orders"].append(oid)
            order_ids.add(oid)
        if cid and extras[mid]["customer"] is None:
            extras[mid]["customer"] = {"id": cid, "name": (cname or "")[:300]}
    if order_ids:
        from sales.models import SalesOrder
        numbers = {pk: f"{src}:{num}"[:200] for pk, src, num in
                   SalesOrder.objects.filter(pk__in=order_ids).values_list("pk", "source", "order_number")}
        for e in extras.values():
            e["orders"] = [{"id": oid, "number": numbers[oid]} for oid in sorted(set(e["orders"])) if oid in numbers]
    try:
        from projects.models import ProjectLink
        links = (ProjectLink.objects.filter(Q(thread_id__in=thread_ids) | Q(message_id__in=pks),
                                            status=ProjectLink.CONFIRMED)
                 .values_list("thread_id", "message_id", "project_id", "project__code", "project__name"))
        by_thread, by_msg = {}, {}
        for tid, mid, pid, code, pname in links:
            item = {"id": pid, "code": code, "name": (pname or "")[:200]}
            (by_thread.setdefault(tid, {}) if tid else by_msg.setdefault(mid, {}))[pid] = item
        for m in messages:
            extras[m.pk]["projects"] = {**by_thread.get(m.thread_id, {}), **by_msg.get(m.pk, {})}
    except ImportError:
        pass
    return extras


def _direction(msg, own) -> str:
    from email_assistant.crm_link import is_own
    if not is_own(msg.from_email, own):
        return "in"
    recipients = list(msg.to_emails or []) + list(msg.cc_emails or [])
    return "internal" if recipients and all(is_own(a, own) for a in recipients) else "out"


def build_content(msg, scope, own, extras) -> dict:
    """Поля документа, що індексуються (без ревізії, посилань і часу) — з них рахується content_version."""
    from email_assistant.imap_client import html_to_text
    full = msg.body_text or (html_to_text(msg.body_html) if msg.body_html else "")
    text = full[:TEXT_MAX]
    thread = msg.thread
    customer = None
    if thread is not None and thread.crm_customer_id:
        customer = {"id": thread.crm_customer_id, "name": (thread.crm_customer.name or "")[:300]}
    elif extras.get("customer"):
        customer = extras["customer"]
    return {
        "mailbox": {"id": msg.account_id, "address": msg.account.email_address},
        "thread": None if thread is None else {"id": thread.pk, "subject": (thread.subject or "")[:500]},
        "message_id": (msg.message_id or "")[:500],
        "in_reply_to": (msg.in_reply_to or "")[:500],
        "direction": _direction(msg, own),
        "category": msg.category,
        "sent_at": _iso(msg.sent_at),
        "subject": (msg.subject or "")[:500],
        "from": {"email": (msg.from_email or "")[:500], "name": (msg.from_name or "")[:200]},
        "to": [a[:500] for a in list(msg.to_emails or [])[:200]],
        "cc": [a[:500] for a in list(msg.cc_emails or [])[:200]],
        "text": text,
        "text_truncated": len(full) > TEXT_MAX,
        "text_new": text[:TEXT_NEW_MAX],
        "text_new_mode": "full",
        "text_new_truncated": len(text) > TEXT_NEW_MAX,
        "customer": customer,
        "projects": [extras["projects"][k] for k in sorted(extras.get("projects", {}))],
        "orders": extras.get("orders", []),
        "attachments": _attachments(msg, scope),
        "access": {"scope": "mailbox", "mailbox_id": msg.account_id, "user_ids": [msg.account.user_id]},
    }


def content_version(content) -> str:
    raw = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def render_document(msg, content, doc, generation) -> dict:
    out = {
        "document_id": f"{DOC_TYPE}:{msg.pk}", "document_type": DOC_TYPE,
        "document_revision": doc.revision, "content_version": doc.feed_version,
        "source_instance_id": instance_id(), "generation": generation,
        "url": f"/email/message/{msg.pk}/",
        **copy.deepcopy(content),
        "updated_at": _iso(timezone.now()),
    }
    for a in out["attachments"]:
        if a["indexable"]:
            a["url"] = f"/api/v1/knowledge/documents/{DOC_TYPE}:{msg.pk}/attachments/{a['index']}/" \
                       f"?revision={doc.revision}"
    return out


# ── Записувачі журналу ───────────────────────────────────────────────────────

@contextmanager
def journal_writer():
    """Транзакція записувача: блокування до commit → порядок seq дорівнює порядку commit."""
    with transaction.atomic():
        if connection.vendor == "postgresql":
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", [LOCK_KEY])
        yield


def _tombstone(doc, now):
    doc.revision += 1
    doc.fed, doc.feed_version, doc.fed_at = False, "", now
    doc.save()


def _sync_batch(messages, scope, own, missing_ids=()) -> int:
    """Під блокуванням порівняти поточний стан листів із KnowledgeDoc і записати події. Повертає кількість подій."""
    from .models import KnowledgeChange, KnowledgeDoc
    messages = list(messages)
    ids = [m.pk for m in messages] + list(missing_ids)
    if not ids:
        return 0
    extras = _batch_extras(messages)
    now = timezone.now()
    with journal_writer():
        docs = {d.obj_id: d for d in KnowledgeDoc.objects.filter(doc_type=DOC_TYPE, obj_id__in=ids)}
        events = []
        for m in messages:
            doc = docs.get(m.pk)
            reason = scope_reason(m, scope)
            was_fed = doc is not None and doc.fed
            if not reason and (was_fed or scope.enabled):
                cv = content_version(build_content(m, scope, own, extras[m.pk]))
                if was_fed and doc.feed_version == cv:
                    continue
                doc = doc or KnowledgeDoc(doc_type=DOC_TYPE, obj_id=m.pk)
                doc.revision += 1
                doc.fed, doc.feed_version, doc.fed_at = True, cv, now
                doc.save()
                events.append(KnowledgeChange(doc_type=DOC_TYPE, obj_id=m.pk, action=KnowledgeChange.UPSERT))
            elif was_fed:
                _tombstone(doc, now)
                events.append(KnowledgeChange(doc_type=DOC_TYPE, obj_id=m.pk, action=KnowledgeChange.DELETE,
                                              reason=reason or "out_of_scope"))
        for pk in missing_ids:
            doc = docs.get(pk)
            if doc is not None and doc.fed:
                _tombstone(doc, now)
                events.append(KnowledgeChange(doc_type=DOC_TYPE, obj_id=pk, action=KnowledgeChange.DELETE,
                                              reason="deleted"))
        for e in events:      # по одному: seq видається під блокуванням у порядку подій
            e.save()
    return len(events)


def touch_messages(pks) -> int:
    """Перевірити листи після змін (сигнали, масові дії). Нічого не робить, поки база знань не використовується."""
    from email_assistant.crm_link import own_addresses
    from .models import KnowledgeDoc
    pks = sorted({int(p) for p in pks if p})
    if not pks:
        return 0
    scope = get_scope()
    if not scope.enabled and not KnowledgeDoc.objects.filter(doc_type=DOC_TYPE, obj_id__in=pks, fed=True).exists():
        return 0
    msgs = list(_messages_qs().filter(pk__in=pks))
    return _sync_batch(msgs, scope, own_addresses(), set(pks) - {m.pk for m in msgs})


def touch_thread(thread_id) -> int:
    from email_assistant.models import EmailMessage
    if not thread_id:
        return 0
    return touch_messages(EmailMessage.objects.filter(thread_id=thread_id).values_list("pk", flat=True))


def safe_touch(pks=(), thread_id=None):
    """Для хуків пошти/проєктів: збій бази знань ніколи не ламає основну дію."""
    try:
        with transaction.atomic():
            if thread_id:
                touch_thread(thread_id)
            if pks:
                touch_messages(pks)
    except Exception as e:  # noqa: BLE001
        logger.warning("knowledge touch failed (thread=%s, pks=%s): %s", thread_id, list(pks)[:5], e)


def touch_account(account_id, batch=300) -> int:
    """Права на скриньку змінились (власник, активність, адреса): нові ревізії або tombstone для всіх її листів."""
    from email_assistant.models import EmailMessage
    pks = list(EmailMessage.objects.filter(account_id=account_id).order_by("pk").values_list("pk", flat=True))
    return sum(touch_messages(pks[i:i + batch]) for i in range(0, len(pks), batch))


def start_account_touch(account_id):
    """touch_account у фоні: збереження скриньки не чекає перевірки тисяч листів."""
    caller = threading.current_thread()

    def run():
        try:
            touch_account(account_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("knowledge account touch failed (account=%s): %s", account_id, e)
        finally:
            if threading.current_thread() is not caller:   # власне з'єднання фонового потоку
                connection.close()
    _start_background(run)


def refresh_all(batch=300) -> dict:
    """Повна звірка: усі допущені листи + усі передані документи. Також чистить журнал і старі знімки."""
    from email_assistant.crm_link import own_addresses
    from .models import KnowledgeDoc, KnowledgeSettings
    scope, own = get_scope(), own_addresses()
    seen, events = set(), 0
    if scope.enabled:
        last = 0
        base = scope_qs(scope).select_related("account", "thread", "thread__crm_customer").order_by("pk")
        while True:
            chunk = list(base.filter(pk__gt=last)[:batch])
            if not chunk:
                break
            events += _sync_batch(chunk, scope, own)
            seen.update(m.pk for m in chunk)
            last = chunk[-1].pk
    rest = [i for i in KnowledgeDoc.objects.filter(doc_type=DOC_TYPE, fed=True).values_list("obj_id", flat=True)
            if i not in seen]
    for i in range(0, len(rest), batch):
        part = rest[i:i + batch]
        msgs = list(_messages_qs().filter(pk__in=part))
        events += _sync_batch(msgs, scope, own, set(part) - {m.pk for m in msgs})
    pruned = prune()
    cleanup_snapshots()
    result = f"{timezone.localtime():%d.%m.%Y %H:%M}: подій {events}, очищено журналу до seq {pruned or '—'}"
    KnowledgeSettings.objects.filter(pk=1).update(last_refresh_at=timezone.now(), last_refresh_result=result[:255])
    return {"events": events, "pruned_seq": pruned}


def refresh_if_due() -> bool:
    from .models import KnowledgeDoc, KnowledgeSettings
    s = KnowledgeSettings.get()
    if not s.enabled and not KnowledgeDoc.objects.filter(fed=True).exists():
        return False
    if s.last_refresh_at and timezone.now() - s.last_refresh_at < REFRESH_EVERY:
        return False
    refresh_all()
    return True


_refresh_lock = threading.Lock()


def _start_background(fn):
    threading.Thread(target=fn, daemon=True).start()


def start_refresh():
    """Звірка у фоні (після збереження налаштувань) — без паралельних запусків."""
    caller = threading.current_thread()

    def run():
        if not _refresh_lock.acquire(blocking=False):
            return
        try:
            refresh_all()
        except Exception as e:  # noqa: BLE001
            logger.exception("knowledge refresh failed: %s", e)
        finally:
            _refresh_lock.release()
            if threading.current_thread() is not caller:   # власне з'єднання фонового потоку
                connection.close()
    _start_background(run)


def prune(days=CHANGE_RETENTION_DAYS):
    """Видалити події, старші за days (курсори до межі → 410 cursor_expired)."""
    from .models import KnowledgeChange, KnowledgeSettings
    cutoff = timezone.now() - timedelta(days=days)
    with journal_writer():
        mx = KnowledgeChange.objects.filter(created_at__lt=cutoff).aggregate(m=Max("id"))["m"]
        if not mx:
            return None
        KnowledgeChange.objects.filter(id__lte=mx).delete()
        KnowledgeSettings.objects.filter(pk=1, pruned_seq__lt=mx).update(pruned_seq=mx)
    return mx


def clear_index() -> int:
    """«Прибрати все з індексу»: tombstone для всіх переданих документів і пауза."""
    from .models import KnowledgeChange, KnowledgeDoc, KnowledgeSettings
    now, n = timezone.now(), 0
    with journal_writer():
        KnowledgeSettings.objects.filter(pk=1).update(enabled=False)
        for doc in KnowledgeDoc.objects.filter(doc_type=DOC_TYPE, fed=True).order_by("obj_id"):
            _tombstone(doc, now)
            KnowledgeChange.objects.create(doc_type=DOC_TYPE, obj_id=doc.obj_id, action=KnowledgeChange.DELETE,
                                           reason="index_cleared")
            n += 1
    return n


def new_generation() -> str:
    """Після відновлення БД з бекапу: нове покоління → RAG отримає generation_mismatch і зробить повну звірку."""
    from .models import KnowledgeSettings
    with journal_writer():
        s = KnowledgeSettings.get()
        s.generation = uuid.uuid4()
        s.settings_version += 1
        s.save(update_fields=["generation", "settings_version"])
    return str(s.generation)


# ── Читання для RAG ──────────────────────────────────────────────────────────

def parse_document_id(document_id) -> int:
    m = _DOC_ID_RE.match(document_id or "")
    if not m:
        raise KnowledgeError("invalid_request", 400, "Некоректний document_id (очікується email:<pk>).")
    return int(m.group(1))


def _current(pks, scope, own):
    """{pk: (msg|None, doc|None, content|None)} — з самовиправленням розбіжностей (нова ревізія під блокуванням)."""
    from .models import KnowledgeDoc
    msgs = {m.pk: m for m in _messages_qs().filter(pk__in=pks)}
    docs = {d.obj_id: d for d in KnowledgeDoc.objects.filter(doc_type=DOC_TYPE, obj_id__in=pks)}
    extras = _batch_extras(msgs.values())
    contents, drift = {}, []
    for pk in pks:
        doc, msg = docs.get(pk), msgs.get(pk)
        if doc is None or not doc.fed:
            continue
        if msg is None or scope_reason(msg, scope):
            drift.append(pk)
            continue
        contents[pk] = build_content(msg, scope, own, extras[pk])
        if content_version(contents[pk]) != doc.feed_version:
            drift.append(pk)
    if drift:
        _sync_batch([msgs[p] for p in drift if p in msgs], scope, own, {p for p in drift if p not in msgs})
        docs = {d.obj_id: d for d in KnowledgeDoc.objects.filter(doc_type=DOC_TYPE, obj_id__in=pks)}
        msgs = {m.pk: m for m in _messages_qs().filter(pk__in=pks)}
        for p in drift:
            if p in msgs and docs[p].fed:
                contents[p] = build_content(msgs[p], scope, own, _batch_extras([msgs[p]])[p])
    return {pk: (msgs.get(pk), docs.get(pk), contents.get(pk)) for pk in pks}


def read_changes(after=0, limit=50, generation=None) -> dict:
    from email_assistant.crm_link import own_addresses
    from .models import KnowledgeChange, KnowledgeSettings
    s = KnowledgeSettings.get()
    gen = str(s.generation)
    if generation and generation != gen:
        raise KnowledgeError("generation_mismatch", 409, "Покоління журналу змінилось — потрібна повна звірка.",
                             details={"generation": gen})
    last_seq = max(KnowledgeChange.objects.aggregate(m=Max("id"))["m"] or 0, s.pruned_seq)
    if after > last_seq:
        raise KnowledgeError("cursor_ahead", 409, "Курсор новіший за журнал (відновлення БД?) — потрібна звірка.",
                             details={"last_seq": last_seq})
    if s.pruned_seq and after < s.pruned_seq:
        raise KnowledgeError("cursor_expired", 410, "Курсор старший за збережений журнал — потрібна звірка snapshot.",
                             details={"resume_after": s.pruned_seq})
    rows = list(KnowledgeChange.objects.filter(id__gt=after).order_by("id")[:limit])
    latest = {}
    for r in rows:
        latest[(r.doc_type, r.obj_id)] = r
    ordered = sorted(latest.values(), key=lambda r: r.id)
    scope = get_scope()
    state = _current([r.obj_id for r in ordered], scope, own_addresses())
    items = []
    for r in ordered:
        msg, doc, content = state[r.obj_id]
        item = {"seq": r.id, "document_id": r.document_id, "document_type": r.doc_type,
                "changed_at": _iso(r.created_at), "document_revision": max(doc.revision if doc else 1, 1)}
        if doc is not None and doc.fed and msg is not None and content is not None:
            item["action"] = "upsert"
            item["document"] = render_document(msg, content, doc, gen)
        else:
            item["action"] = "delete"
            item["reason"] = (r.reason if r.action == KnowledgeChange.DELETE and r.reason
                              else ("deleted" if msg is None else scope_reason(msg, scope) or "index_cleared"))
        items.append(item)
    next_after = rows[-1].id if rows else after
    last_seq = max(KnowledgeChange.objects.aggregate(m=Max("id"))["m"] or 0, s.pruned_seq, next_after)
    return {
        "contract_version": CONTRACT_VERSION, "source_instance_id": instance_id(), "generation": gen,
        "enabled": s.enabled, "last_seq": last_seq, "next_after": next_after,
        "has_more": KnowledgeChange.objects.filter(id__gt=next_after).exists(), "items": items,
    }


def get_document(document_id):
    """(msg, doc, документ) або KnowledgeError document_not_found."""
    from email_assistant.crm_link import own_addresses
    from .models import KnowledgeSettings
    pk = parse_document_id(document_id)
    msg, doc, content = _current([pk], get_scope(), own_addresses())[pk]
    if doc is None or not doc.fed or msg is None or content is None:
        raise KnowledgeError("document_not_found", 404, "Документ не підлягає індексації.")
    return msg, doc, render_document(msg, content, doc, str(KnowledgeSettings.get().generation))


def attachment_bytes(document_id, index, revision):
    """(байти, вкладення документа) з перевіркою ревізії й хешу."""
    from email_assistant.models import EmailMessage
    msg, doc, document = get_document(document_id)
    if revision != doc.revision:
        raise KnowledgeError("revision_mismatch", 412, "Документ змінився — перечитайте його.", retryable=True,
                             details={"current_revision": doc.revision})
    att = next((a for a in document["attachments"] if a["index"] == index), None)
    if att is None or not att["indexable"]:
        raise KnowledgeError("attachment_not_found", 404, "Вкладення недоступне для індексації.")
    path = _attachment_path((msg.attachments or [])[index])
    data = None
    if path:
        with open(path, "rb") as fh:
            data = fh.read()
    if data is None or "sha256:" + hashlib.sha256(data).hexdigest() != att["content_hash"]:
        raw = copy.deepcopy(list(msg.attachments or []))   # файл змінився: скинути кеш хешу → нова ревізія
        raw[index].pop("sha256", None)
        EmailMessage.objects.filter(pk=msg.pk).update(attachments=raw)
        touch_messages([msg.pk])
        raise KnowledgeError("revision_mismatch", 412, "Вкладення змінилось — перечитайте документ.", retryable=True,
                             details={"current_revision": max(doc.revision + 1, 1)})
    return data, att


# ── Знімки ───────────────────────────────────────────────────────────────────

def cleanup_snapshots():
    from .models import KnowledgeSnapshot
    KnowledgeSnapshot.objects.filter(expires_at__lt=timezone.now()).delete()


def create_snapshot() -> dict:
    from .models import KnowledgeChange, KnowledgeDoc, KnowledgeSettings, KnowledgeSnapshot, KnowledgeSnapshotItem
    cleanup_snapshots()
    s = KnowledgeSettings.get()
    outer = connection.in_atomic_block
    with transaction.atomic():
        if connection.vendor == "postgresql" and not outer:
            with connection.cursor() as cur:   # один MVCC-знімок для snapshot_seq і складу
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        snapshot_seq = max(KnowledgeChange.objects.aggregate(m=Max("id"))["m"] or 0, s.pruned_seq)
        docs = list(KnowledgeDoc.objects.filter(doc_type=DOC_TYPE, fed=True).order_by("obj_id")
                    .values_list("obj_id", "revision", "feed_version"))
        now = timezone.now()
        snap = KnowledgeSnapshot.objects.create(snapshot_seq=snapshot_seq, generation=s.generation,
                                                total=len(docs), expires_at=now + SNAPSHOT_TTL)
        KnowledgeSnapshotItem.objects.bulk_create(
            [KnowledgeSnapshotItem(snapshot=snap, position=i + 1, doc_type=DOC_TYPE, obj_id=o, revision=r,
                                   content_version=v) for i, (o, r, v) in enumerate(docs)], batch_size=2000)
    return {"contract_version": CONTRACT_VERSION, "snapshot_id": str(snap.pk), "snapshot_seq": snapshot_seq,
            "source_instance_id": instance_id(), "generation": str(snap.generation),
            "created_at": _iso(snap.created_at), "expires_at": _iso(snap.expires_at), "total": snap.total}


def snapshot_page(snapshot_id, cursor=None, limit=500) -> dict:
    from .models import KnowledgeSnapshot
    snap = KnowledgeSnapshot.objects.filter(pk=snapshot_id).first()
    if snap is None:
        raise KnowledgeError("snapshot_not_found", 404, "Знімок не знайдено — створіть новий.")
    if snap.expires_at < timezone.now():
        raise KnowledgeError("snapshot_expired", 410, "Знімок прострочений — створіть новий; нічого не видаляйте.")
    start = int(cursor) if str(cursor or "").isdigit() else 0
    rows = list(snap.items.filter(position__gt=start).order_by("position")[:limit + 1])
    more = len(rows) > limit
    rows = rows[:limit]
    return {"contract_version": CONTRACT_VERSION, "snapshot_id": str(snap.pk), "snapshot_seq": snap.snapshot_seq,
            "source_instance_id": instance_id(), "generation": str(snap.generation),
            "items": [{"document_id": f"{r.doc_type}:{r.obj_id}", "document_type": r.doc_type,
                       "document_revision": r.revision, "content_version": r.content_version} for r in rows],
            "next_cursor": str(rows[-1].position) if more else None, "complete": not more}


# ── Статуси від RAG ──────────────────────────────────────────────────────────

_STATE_FOR = {"indexed": "indexed", "failed": "failed", "skipped": "skipped", "deleted": "deleted"}


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return timezone.now()


def apply_status(payload, request_id) -> dict:
    """POST /status/: результат по кожному запису; переходи для ревізії визначає лише attempt_id."""
    from .models import KnowledgeDoc, KnowledgeSettings
    validate_status_payload(payload)
    s = KnowledgeSettings.get()
    same_source = payload["source_instance_id"] == instance_id() and payload["generation"] == str(s.generation)
    results, accepted, now = [], 0, timezone.now()
    with journal_writer():
        for item in payload["items"]:
            res = {"document_id": item["document_id"], "document_revision": item["document_revision"],
                   "attempt_id": item["attempt_id"]}
            m = _DOC_ID_RE.match(item["document_id"])
            doc = KnowledgeDoc.objects.filter(doc_type=DOC_TYPE, obj_id=int(m.group(1))).first() if m else None
            if not same_source:
                res["result"] = "instance_mismatch"
            elif doc is None or doc.revision == 0:
                res["result"] = "unknown_document"
            elif item["document_revision"] < doc.revision:
                res.update(result="stale", current_revision=doc.revision)
            elif item["document_revision"] > doc.revision:
                res.update(result="revision_ahead", current_revision=doc.revision)
                logger.warning("knowledge status revision_ahead %s r%s > r%s (request %s)",
                               item["document_id"], item["document_revision"], doc.revision, request_id)
            elif doc.status_revision == doc.revision and item["attempt_id"] == doc.attempt_id:
                res["result"] = "duplicate"
            elif doc.status_revision == doc.revision and item["attempt_id"] < doc.attempt_id:
                res["result"] = "stale_attempt"
            elif (item["state"] == "deleted") == doc.fed or \
                    (item["state"] != "deleted" and item.get("content_version") != doc.feed_version):
                # стан не відповідає ревізії (deleted для живого документа чи чужий вміст)
                res.update(result="stale", current_revision=doc.revision)
            else:
                doc.state = _STATE_FOR[item["state"]]
                doc.status_revision, doc.attempt_id = doc.revision, item["attempt_id"]
                doc.chunks = int(item.get("chunks") or 0)
                err = item.get("error") or {}
                doc.error = (f"{err.get('code', '')}: {err.get('message', '')}" if err else "")[:500]
                doc.last_status_at = now
                if item["state"] == "indexed":
                    doc.indexed_at = _parse_ts(item.get("reported_at"))
                doc.save()
                res["result"] = "accepted"
                accepted += 1
            results.append(res)
    return {"request_id": request_id, "accepted": accepted, "results": results}


def validate_status_payload(payload):
    """Мінімальна перевірка тіла без jsonschema (production): структура, типи, ліміти."""
    def bad(msg):
        raise KnowledgeError("invalid_request", 400, msg)
    if not isinstance(payload, dict):
        bad("Очікується JSON-об'єкт.")
    for key in ("source_instance_id", "generation"):
        if not isinstance(payload.get(key), str):
            bad(f"Поле {key} обов'язкове.")
    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        bad("items: від 1 до 200 записів.")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            bad(f"items[{i}]: очікується об'єкт.")
        if not isinstance(item.get("document_id"), str):
            bad(f"items[{i}].document_id обов'язковий.")
        for key in ("document_revision", "attempt_id"):
            if not isinstance(item.get(key), int) or isinstance(item.get(key), bool) or item[key] < 1:
                bad(f"items[{i}].{key}: ціле ≥ 1.")
        if item.get("state") not in _STATE_FOR:
            bad(f"items[{i}].state: indexed / failed / skipped / deleted.")
        if item["state"] != "deleted" and not isinstance(item.get("content_version"), str):
            bad(f"items[{i}].content_version обов'язковий для {item['state']}.")
        if item["state"] == "deleted" and "content_version" in item:
            bad(f"items[{i}]: deleted без content_version.")
        if item["state"] == "failed" and not isinstance(item.get("error"), dict):
            bad(f"items[{i}].error обов'язковий для failed.")


def _unconfirmed_tombstones():
    from django.db.models import F
    from .models import KnowledgeDoc
    return (KnowledgeDoc.objects.filter(doc_type=DOC_TYPE, fed=False, revision__gt=0)
            .exclude(status_revision=F("revision"), state=KnowledgeDoc.DELETED))


def settle_tombstones(after) -> int:
    """Tombstone, що з'явилися до завершеного знімка RAG і без подій журналу після нього, — закриті без звіту RAG.
    Знімок містить лише живі документи, а журнал RAG читає вже після snapshot_seq — про такі видалення RAG
    не дізнається й deleted не надішле (інакше «Видалення ще не підтверджено» не зникне ніколи).
    Ознака завершеного знімка — запит /changes/ з after ≥ snapshot_seq: RAG читає журнал лише після знімка.
    Видалення з подіями після знімка чекають звіту RAG, як і раніше. Викликає ChangesView; збій не ламає відповідь."""
    from django.db.models import F
    from .models import KnowledgeChange, KnowledgeDoc, KnowledgeSettings, KnowledgeSnapshot
    try:
        if not _unconfirmed_tombstones().exists():
            return 0
        s = KnowledgeSettings.get()
        snap = (KnowledgeSnapshot.objects.filter(generation=s.generation, snapshot_seq__lte=after)
                .order_by("-snapshot_seq", "-created_at").first())
        if snap is None:
            return 0
        later = KnowledgeChange.objects.filter(doc_type=DOC_TYPE, id__gt=snap.snapshot_seq).values("obj_id")
        if not _unconfirmed_tombstones().exclude(obj_id__in=later).exists():
            return 0
        with journal_writer():      # разом із записувачами: tombstone не зміниться посеред оновлення
            n = (_unconfirmed_tombstones().exclude(obj_id__in=later)
                 .update(state=KnowledgeDoc.DELETED, status_revision=F("revision"), chunks=0, error="",
                         last_status_at=timezone.now()))
        if n:
            logger.info("knowledge: %s tombstone(s) before snapshot seq %s settled without RAG report",
                        n, snap.snapshot_seq)
        return n
    except Exception as e:  # noqa: BLE001
        logger.warning("knowledge tombstones not settled: %s", e)
        return 0


def summary() -> dict:
    from .models import KnowledgeChange, KnowledgeDoc, KnowledgeSettings
    from django.db.models import F
    s = KnowledgeSettings.get()
    docs = KnowledgeDoc.objects.filter(doc_type=DOC_TYPE)
    fed = docs.filter(fed=True)
    current = fed.filter(status_revision=F("revision"))
    counts = {k: current.filter(state=k).count() for k in ("indexed", "failed", "skipped")}
    eligible = fed.count()
    pending_delete = _unconfirmed_tombstones().count()
    last_status = docs.aggregate(m=Max("last_status_at"))["m"]
    return {
        "contract_version": CONTRACT_VERSION, "source_instance_id": instance_id(), "generation": str(s.generation),
        "enabled": s.enabled,
        "last_seq": max(KnowledgeChange.objects.aggregate(m=Max("id"))["m"] or 0, s.pruned_seq),
        "settings_version": s.settings_version, "last_status_at": _iso(last_status),
        "documents": {"eligible": eligible, **counts,
                      "pending": eligible - sum(counts.values()), "pending_delete": pending_delete},
    }


# ── Що RAG робить зараз (сторінка «База знань») ──────────────────────────────

def feed_paused() -> bool:
    from .models import KnowledgeSettings
    return KnowledgeSettings.objects.filter(pk=1, feed_paused=True).exists()


def set_feed_paused(paused: bool):
    """«⏸ Пауза»: стрічка відповідає RAG 429 rate_limited (код контракту), звіти RAG приймаються далі.
    Після «▶ Продовжити» RAG сам повторює запит; уже проіндексоване він пропускає без повторних embeddings."""
    from .models import KnowledgeSettings
    KnowledgeSettings.get()
    KnowledgeSettings.objects.filter(pk=1).update(feed_paused=paused, feed_paused_at=timezone.now() if paused else None)


def note_activity(call, *, snapshot_total=None, document=False, changes=False, reported=0):
    """Звернення RAG до стрічки → етап і прогрес. Збій обліку ніколи не ламає відповідь API.
    Порядок роботи RAG: знімок (повна звірка) → документи знімка → лише потім стрічка змін."""
    from django.db.models import F
    from .models import KnowledgeActivity
    try:
        now = timezone.now()
        act = KnowledgeActivity.get()
        fields = {"last_call": call[:200], "last_call_at": now}
        if snapshot_total is not None:
            fields.update(phase=KnowledgeActivity.SNAPSHOT, phase_started_at=now, snapshot_total=snapshot_total,
                          snapshot_served=0, reported=0)
        elif document and act.phase == KnowledgeActivity.SNAPSHOT:
            fields["snapshot_served"] = F("snapshot_served") + 1
        elif changes and act.phase != KnowledgeActivity.CHANGES:   # знімок завершено — RAG перейшов до змін
            fields.update(phase=KnowledgeActivity.CHANGES, phase_started_at=now, reported=0)
        if reported:
            fields["reported"] = F("reported") + reported
        KnowledgeActivity.objects.filter(pk=1).update(**fields)
    except Exception as e:  # noqa: BLE001
        logger.warning("knowledge activity not recorded: %s", e)


def _duration(seconds) -> str:
    minutes = max(1, round(seconds / 60))
    return f"~{minutes} хв" if minutes < 60 else f"~{minutes // 60} год {minutes % 60} хв"


ROADMAP = [("connect", "Підключення"), ("snapshot", "Повна вигрузка"), ("changes", "Нові зміни"),
           ("confirm", "Підтвер­дження"), ("ready", "Готово")]   # м'який перенос — у вузькій колонці під крапкою


def progress() -> dict:
    """Етап і прогрес індексації для живої панелі «База знань» (без звернення до RAG).
    stage: setup / off / paused / waiting / snapshot / changes / confirm / ready;
    step — номер кроку дорожньої карти ROADMAP (0 підключення … 4 готово), steps — карта з деталями кроків.
    «Нові зміни» — RAG ще застосовує журнал (applied_seq з capabilities < last_seq);
    «Підтвердження» — RAG усе застосував і надсилає звіти, а Minerva ще чекає підтвердження документів."""
    from . import retrieval
    from .models import KnowledgeActivity, KnowledgeSettings
    s, act, now = KnowledgeSettings.get(), KnowledgeActivity.get(), timezone.now()
    summ = summary()
    d = summ["documents"]
    waiting_docs = d["pending"] + d["pending_delete"]
    seen_ago = int((now - act.last_call_at).total_seconds()) if act.last_call_at else None
    out = {"paused": s.feed_paused, "enabled": s.enabled, "documents": d, "percent": None, "eta": None, "rate": None,
           "served": act.snapshot_served, "total": act.snapshot_total, "last_call": act.last_call,
           "seen_ago": seen_ago, "silent": False, "rag": None, "breaker": None,
           "updated": timezone.localtime(now).strftime("%H:%M:%S")}
    caps = s.last_capabilities or {}
    if caps:
        ready, idx = caps.get("readiness") or {}, caps.get("index") or {}
        out["rag"] = {"retrieval": ready.get("retrieval"), "indexing": ready.get("indexing"), "docs": idx.get("docs"),
                      "chunks": idx.get("chunks"), "checked": timezone.localtime(s.last_capabilities_at).strftime(
                          "%d.%m %H:%M:%S") if s.last_capabilities_at else ""}
    until = retrieval.breaker_until(s)
    if until:
        out["breaker"] = timezone.localtime(until).strftime("%H:%M")
    working = not s.feed_paused and seen_ago is not None and seen_ago <= RAG_SILENT_AFTER.total_seconds()
    if act.phase == KnowledgeActivity.SNAPSHOT and act.snapshot_total:
        served = min(act.snapshot_served, act.snapshot_total)
        out["percent"] = int(served * 100 / act.snapshot_total)
        elapsed = (now - act.phase_started_at).total_seconds() if act.phase_started_at else 0
        if working and served >= 3 and elapsed > 0:     # під час паузи чи тиші швидкість і час до кінця не показуємо
            out["rate"] = f"{served * 60 / elapsed:.0f} док./хв"
            out["eta"] = _duration((act.snapshot_total - served) * elapsed / served)
    idx = (caps.get("index") or {}) if caps else {}
    applied, last_seq = idx.get("applied_seq"), summ["last_seq"]
    behind = last_seq - applied if isinstance(applied, int) and applied < last_seq else 0
    out["seq"] = {"last": last_seq, "received": idx.get("received_seq"), "applied": applied}
    out["reported"] = act.reported            # браузер рахує з нього швидкість підтверджень
    served = min(act.snapshot_served, act.snapshot_total)
    if not instance_id():
        stage, step, text = "setup", 0, "Не задано назву сервера для RAG — стрічка для RAG вимкнена."
    elif act.phase == KnowledgeActivity.SNAPSHOT:
        stage, step = "snapshot", 1
        text = (f"Повна вигрузка: RAG забирає й індексує листи — {served} з {act.snapshot_total}. "
                f"Пошук увімкнеться після завершення.")
    elif not s.enabled and not d["eligible"] and not waiting_docs:
        stage, step, text = "off", None, "Індексацію не ввімкнено — позначте «Індексувати пошту» нижче й збережіть."
    elif act.last_call_at is None:
        stage, step, text = "waiting", 0, "RAG ще не звертався до Minerva — перевірте ключ і з'єднання на RAG-ПК."
    elif behind:
        stage, step = "changes", 2
        text = f"Нові зміни: RAG застосовує журнал — ще подій {behind} (застосовано до seq {applied} з {last_seq})."
    elif waiting_docs:
        stage, step = "confirm", 3
        text = (f"RAG обробив зміни й надсилає підтвердження в Minerva — ще не підтверджено {waiting_docs} "
                f"(нових чи змінених {d['pending']}, видалень {d['pending_delete']}). Пошук уже працює.")
    else:
        stage, step, text = "ready", 4, "✅ Усе проіндексовано й підтверджено — пошук готовий."
    if s.feed_paused and step is not None:     # пауза — поверх кроку, на якому зупинились
        stage, text = "paused", (f"⏸ Призупинено на кроці «{ROADMAP[step][1].replace(chr(0xad), '')}»: "
                                 "Minerva відповідає RAG «зачекайте». "
                                 "Пошук працює з уже проіндексованим.")
    details = {     # (коли крок пройдено, коли він поточний) — коротко, бо під крапками; повний опис — у text
        "connect": (f"звертався {_ago(seen_ago)}" if seen_ago is not None else "",
                    "немає назви сервера" if stage == "setup" else "чекаємо RAG"),
        "snapshot": (f"{act.snapshot_total} док." if act.snapshot_total else "",
                     f"{served} з {act.snapshot_total}" + (f" · {out['eta']}" if out["eta"] else "")),
        "changes": (f"до seq {applied}" if isinstance(applied, int) else "", f"ще подій {behind}"),
        "confirm": ("усе підтверджено", f"ще {waiting_docs}"),
        "ready": ("пошук готовий", ""),
    }
    out["steps"] = []
    for i, (key, label) in enumerate(ROADMAP):
        status = ("todo" if step is None else "done" if i < step or (i == step and key == "ready")
                  else "current" if i == step else "todo")
        out["steps"].append({"key": key, "label": label, "status": status,
                             "detail": {"done": details[key][0], "current": details[key][1]}.get(status, "")})
    out.update(stage=stage, step=step, text=text)
    out["silent"] = (seen_ago is not None and seen_ago > RAG_SILENT_AFTER.total_seconds()
                     and stage in ("snapshot", "changes", "confirm", "ready", "paused"))
    return out


def _ago(seconds) -> str:
    if seconds < 60:
        return f"{seconds} с тому"
    return f"{seconds // 60} хв тому" if seconds < 3600 else f"{seconds // 3600} год тому"
