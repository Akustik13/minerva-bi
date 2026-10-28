"""
Документи бази знань поза поштою — KnowledgeItem, тип `document` у стрічці для RAG (rag_assistant/knowledge.py).

Етап 2 — людина додає в картці клієнта / проєкту / замовлення / товару («📥 Додати документ / посилання»):
- файл (PDF, DOCX, TXT, CSV…) — Minerva зберігає його й рахує sha256, текст витягує RAG (як для вкладень листів);
- посилання — Minerva сама забирає сторінку (лише публічні адреси, без переадресацій у локальну мережу, ≤ 10 МБ):
  HTML → текст у KnowledgeItem.text, PDF / DOCX за посиланням → зберігається як файл; «🔄 Оновити» — вручну.
Етап 3 — те саме автоматично (knowledge_auto.py): Angebote, документи замовлень, даташити (auto, source_key).

Доступ (access.user_ids у документі; RAG фільтрує ДО ранжування, Minerva перевіряє кожен фрагмент ще раз):
- private («🔒 лише я») — лише власник;
- team — активні співробітники, які мають право перегляду хоча б однієї пов'язаної картки
  (view_customer / view_project / view_salesorder / view_product); без прив'язок — усі співробітники.
Права змінились → нічна звірка (knowledge_refresh) видасть нові ревізії; читання стрічки самовиправляється.
Старий RAG (без підтримки document) цих документів не отримує: стрічка віддає їх лише на ?types=…,document.
"""
import hashlib
import logging
import os
import re
import threading
from contextlib import contextmanager
from urllib.parse import urljoin, urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import connection
from django.utils import timezone

logger = logging.getLogger(__name__)

DOC_TYPE = "document"
TITLE_MAX, NOTE_MAX = 300, 2000
FILE_MAX_MB = 25
LINK_MAX_BYTES = 10 * 1024 * 1024
FETCH_TIMEOUT = 15
LOCAL_TEXT = 6000            # скільки тексту документа бере «🧭 Огляд / ❓ Запитати» картки без RAG
USERS_MAX = 500
LINK_FIELDS = {"customer": "customer", "project": "project", "order": "order", "product": "product"}
PERMS = {"customer": "crm.view_customer", "project": "projects.view_project",
         "order": "sales.view_salesorder", "product": "inventory.view_product"}
AUTO_TOGGLES = {"offer": "auto_offers", "order_file": "auto_order_files", "datasheet": "auto_datasheets"}
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_quiet = threading.local()


@contextmanager
def quiet():
    """Масові зміни (knowledge_auto): сигнали не шлють подію на кожен запис — викликач робить touch пачками."""
    previous = getattr(_quiet, "on", False)
    _quiet.on = True
    try:
        yield
    finally:
        _quiet.on = previous


def is_quiet() -> bool:
    return getattr(_quiet, "on", False)


class ItemError(Exception):
    """Зрозуміла людині причина, чому документ не додано / не оновлено."""


# ── Доступ ───────────────────────────────────────────────────────────────────

def linked_kinds(item) -> list:
    return [k for k, field in LINK_FIELDS.items() if getattr(item, f"{field}_id")]


def can_view(user, item) -> bool:
    if not (user and user.is_authenticated and user.is_active):
        return False
    if item.visibility == item.PRIVATE:
        return item.owner_id == user.pk
    if not user.is_staff:
        return False
    kinds = linked_kinds(item)
    return not kinds or any(user.has_perm(PERMS[k]) for k in kinds)


def can_change(user, item) -> bool:
    """Змінити / видалити: власник або адміністратор; автоматичні документи веде Minerva."""
    if item.auto or not (user and user.is_authenticated and user.is_active):
        return False
    return item.owner_id == user.pk or user.is_superuser or user.has_perm("rag_assistant.change_knowledgeitem")


def access_user_ids(item, cache=None) -> list:
    """Хто бачить документ зараз — для access.user_ids. cache — спільний для пачки документів."""
    cache = cache if cache is not None else {}
    if item.visibility == item.PRIVATE:
        owner = item.owner if item.owner_id else None
        return [owner.pk] if owner is not None and owner.is_active else []
    kinds = tuple(sorted(linked_kinds(item)))
    if kinds not in cache:
        users = get_user_model().objects.filter(is_active=True, is_staff=True).order_by("pk")
        cache[kinds] = [u.pk for u in users if not kinds or any(u.has_perm(PERMS[k]) for k in kinds)][:USERS_MAX]
    return cache[kinds]


def visible(user, qs):
    """Документи, які користувач бачить (перевірка прав — у Python: прав мало, документів на картці небагато)."""
    return [i for i in qs if can_view(user, i)]


def items_for(user, kind, obj):
    from .models import KnowledgeItem
    if kind not in LINK_FIELDS:
        return []
    qs = (KnowledgeItem.objects.filter(**{LINK_FIELDS[kind]: obj})
          .select_related("owner", "customer", "project", "order", "product").order_by("-created_at"))
    return visible(user, qs)


# ── Файл ─────────────────────────────────────────────────────────────────────

def _ext(name) -> str:
    name = (name or "").lower()
    return name.rsplit(".", 1)[-1] if "." in name else ""


def file_path(item) -> str:
    """Шлях до файлу документа лише всередині MEDIA_ROOT."""
    if not item.file:
        return ""
    root = os.path.normpath(str(settings.MEDIA_ROOT))
    path = os.path.normpath(os.path.join(root, item.file.name))
    return path if path.startswith(root + os.sep) and os.path.isfile(path) else ""


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def file_hash(item) -> str:
    """sha256 файлу (кешується в item.file_sha256 без сигналів)."""
    from .models import KnowledgeItem
    if item.file_sha256:
        return item.file_sha256
    path = file_path(item)
    if not path:
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    item.file_sha256 = "sha256:" + h.hexdigest()
    KnowledgeItem.objects.filter(pk=item.pk).update(file_sha256=item.file_sha256)
    return item.file_sha256


def local_text(item, limit=LOCAL_TEXT) -> str:
    """Текст для Minerva без RAG (огляд / запитання картки): опис + текст сторінки / автоматичний опис + файл."""
    from email_assistant.reply_brief import attachment_text
    parts = [item.note.strip()] if item.note.strip() else []
    if item.text.strip():
        parts.append(item.text.strip())
    if item.file:
        text = attachment_text({"name": item.file_name or item.file.name,
                                "file_path": item.file.name}, limit=limit)
        if text:
            parts.append(text)
    return "\n\n".join(parts)[:limit]


# ── Додати / оновити / видалити ──────────────────────────────────────────────

def _links(item, links):
    for kind, obj in (links or {}).items():
        if kind in LINK_FIELDS and obj is not None:
            setattr(item, LINK_FIELDS[kind], obj)


def _visibility(value):
    from .models import KnowledgeItem
    return KnowledgeItem.PRIVATE if value in (True, "private", "1", "on", "true") else KnowledgeItem.TEAM


def add_file(user, upload, *, title="", note="", private=False, links=None):
    """Файл із браузера → KnowledgeItem (upload) → подія стрічки. → item."""
    from .models import KnowledgeItem
    if upload is None:
        raise ItemError("Виберіть файл")
    if upload.size > FILE_MAX_MB * 1024 * 1024:
        raise ItemError(f"Файл більший за {FILE_MAX_MB} МБ")
    data = upload.read()
    name = os.path.basename(upload.name or "document")[:200] or "document"
    item = KnowledgeItem(kind=KnowledgeItem.UPLOAD, title=(title or name).strip()[:TITLE_MAX],
                         note=(note or "").strip()[:NOTE_MAX], owner=user, visibility=_visibility(private),
                         file_name=name, file_size=len(data), content_type=(upload.content_type or "")[:200],
                         file_sha256=_sha(data))
    _links(item, links)
    item.file.save(name, ContentFile(data), save=False)
    item.save()                      # сигнал post_save → подія стрічки
    return item


def add_link(user, url, *, title="", note="", private=False, links=None, fetch=True):
    """Посилання → KnowledgeItem (link); сторінку Minerva забирає у фоні (start_fetch). → item."""
    from .models import KnowledgeItem
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ItemError("Потрібна адреса http(s)://…")
    item = KnowledgeItem(kind=KnowledgeItem.LINK, url=url[:2000], title=(title or parsed.hostname).strip()[:TITLE_MAX],
                         note=(note or "").strip()[:NOTE_MAX], owner=user, visibility=_visibility(private))
    _links(item, links)
    item.save()               # опис уже можна індексувати (сигнал); текст сторінки — після завантаження
    if fetch:
        start_fetch(item.pk, title_from_page=not title.strip())
    return item


def _public(host) -> bool:
    from email_assistant.logos import _public as public
    return public(host)


def _download(url):
    """GET лише публічних адрес (кожна переадресація перевіряється) → (адреса, байти, content-type)."""
    import requests
    for _ in range(5):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ItemError("Потрібна адреса http(s)://…")
        if not _public(parsed.hostname):
            raise ItemError("Адреса локальної мережі або не знайдена — Minerva такі сторінки не відкриває")
        try:
            r = requests.get(url, timeout=FETCH_TIMEOUT, allow_redirects=False, stream=True,
                             headers={"User-Agent": "Mozilla/5.0 (Minerva knowledge base)"})
        except requests.RequestException as e:
            raise ItemError(f"Сторінка не відповіла: {type(e).__name__}") from e
        if r.status_code in (301, 302, 303, 307, 308):
            url = urljoin(url, r.headers.get("Location", ""))
            r.close()
            continue
        if r.status_code != 200:
            r.close()
            raise ItemError(f"Сторінка відповіла HTTP {r.status_code}")
        data = b""
        for chunk in r.iter_content(65536):
            data += chunk
            if len(data) > LINK_MAX_BYTES:
                r.close()
                raise ItemError("Сторінка більша за 10 МБ")
        return url, data, (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    raise ItemError("Забагато переадресацій")


def _file_kind(data, ctype) -> str:
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"PK") and "wordprocessingml" in ctype:
        return "docx"
    return ""


def fetch(item, title_from_page=False):
    """Забрати сторінку за посиланням: HTML / текст → item.text, PDF / DOCX → файл. Помилка — у fetch_error."""
    from email_assistant.imap_client import html_to_text
    from .models import KnowledgeItem
    try:
        final, data, ctype = _download(item.url)
        kind = _file_kind(data, ctype)
        if kind:
            name = (os.path.basename(urlparse(final).path) or f"document.{kind}")[:200]
            if not name.lower().endswith("." + kind):
                name += "." + kind
            if item.file:
                item.file.delete(save=False)
            item.file.save(name, ContentFile(data), save=False)
            item.file_name, item.file_size, item.content_type = name, len(data), ctype[:200]
            item.file_sha256, item.text = _sha(data), ""
        else:
            raw = data.decode("utf-8", errors="replace")
            if "html" in ctype or raw.lstrip()[:200].lower().startswith(("<!doctype html", "<html")):
                m = _TITLE_RE.search(raw)
                if title_from_page and m and m.group(1).strip():
                    item.title = " ".join(m.group(1).split())[:TITLE_MAX]
                raw = html_to_text(raw)
            item.text = re.sub(r"\n{3,}", "\n\n", raw).strip()[:100_000]
            if not item.text:
                raise ItemError("На сторінці немає тексту")
        item.fetched_at, item.fetch_error = timezone.now(), ""
    except ItemError as e:
        item.fetch_error = str(e)[:300]
    except Exception as e:  # noqa: BLE001 — збій сторінки не ламає Minerva
        logger.warning("knowledge link %s not fetched: %s", item.pk, e)
        item.fetch_error = f"Не вдалося завантажити: {type(e).__name__}"[:300]
    KnowledgeItem.objects.filter(pk=item.pk).update(
        title=item.title, text=item.text, file=item.file.name if item.file else "", file_name=item.file_name,
        file_size=item.file_size, content_type=item.content_type, file_sha256=item.file_sha256,
        fetched_at=item.fetched_at, fetch_error=item.fetch_error, updated_at=timezone.now())
    touch([item.pk])
    return item


def _start_background(fn):
    threading.Thread(target=fn, daemon=True).start()


def start_fetch(pk, title_from_page=False):
    caller = threading.current_thread()

    def run():
        from .models import KnowledgeItem
        try:
            item = KnowledgeItem.objects.filter(pk=pk).first()
            if item is not None and item.url:
                fetch(item, title_from_page=title_from_page)
        except Exception as e:  # noqa: BLE001
            logger.exception("knowledge link fetch %s failed: %s", pk, e)
        finally:
            if threading.current_thread() is not caller:   # власне з'єднання фонового потоку
                connection.close()
    _start_background(run)


def update(item, *, title=None, note=None, private=None):
    if title is not None and title.strip():
        item.title = title.strip()[:TITLE_MAX]
    if note is not None:
        item.note = note.strip()[:NOTE_MAX]
    if private is not None:
        item.visibility = _visibility(private)
    item.save()
    return item


def delete(item):
    """Видалити документ: файл і запис; сигнал post_delete → tombstone у стрічці (RAG прибере вектори).
    Файл автоматичного документа належить Angebot / замовленню / товару — він лишається."""
    if item.file and not item.auto:
        item.file.delete(save=False)
    item.delete()


# ── Стрічка для RAG (тип document) ───────────────────────────────────────────

def touch(pks) -> int:
    """Перевірити документи після змін → події стрічки (нічого, поки база знань не використовується)."""
    from . import knowledge
    try:
        return knowledge.touch_docs(DOC_TYPE, pks)
    except Exception as e:  # noqa: BLE001 — збій бази знань не ламає основну дію
        logger.warning("knowledge document touch failed (%s): %s", list(pks)[:5], e)
        return 0


class Handler:
    """Тип document для knowledge.py: завантаження, допуск, вміст, документ, файл."""
    doc_type = DOC_TYPE

    def load(self, pks):
        from .models import KnowledgeItem
        return {i.pk: i for i in KnowledgeItem.objects.filter(pk__in=pks)
                .select_related("owner", "customer", "project", "order", "product")}

    def admitted(self, scope):
        from .models import KnowledgeItem
        return KnowledgeItem.objects.select_related("owner", "customer", "project", "order", "product")

    def extras(self, items):
        cache = {}
        return {i.pk: {"users": cache} for i in items}

    def reason(self, item, scope, extra=None) -> str:
        toggle = AUTO_TOGGLES.get(item.kind)
        if item.auto and toggle and not getattr(scope, toggle, False):
            return "out_of_scope"
        if not (item.file or item.text.strip() or item.note.strip()):
            return "out_of_scope"           # посилання ще не завантажено
        if not access_user_ids(item, (extra or {}).get("users")):
            return "access_revoked"
        return ""

    def content(self, item, scope, own, extra) -> dict:
        from .knowledge import TEXT_MAX, _iso
        body = "\n\n".join(p for p in [item.note.strip(), item.text.strip()] if p)
        return {
            "kind": item.kind,
            "title": item.title[:500],
            "created_at": _iso(item.created_at),
            "source_url": item.url[:2000] or None,
            "text": body[:TEXT_MAX],
            "text_truncated": len(body) > TEXT_MAX,
            "customer": {"id": item.customer_id, "name": (item.customer.company or item.customer.name or "")[:300]}
            if item.customer_id else None,
            "projects": [{"id": item.project_id, "code": item.project.code, "name": (item.project.name or "")[:200]}]
            if item.project_id else [],
            "orders": [{"id": item.order_id, "number": f"{item.order.source}:{item.order.order_number}"[:200]}]
            if item.order_id else [],
            "products": [{"id": item.product_id, "sku": (item.product.sku or "")[:100]}] if item.product_id else [],
            "attachments": self._attachments(item, scope),
            "access": {"scope": "users", "user_ids": access_user_ids(item, extra.get("users"))},
        }

    def _attachments(self, item, scope) -> list:
        if not item.file:
            return []
        name = (item.file_name or os.path.basename(item.file.name) or "document")[:300]
        path, reason = file_path(item), ""
        if _ext(name) not in scope.attachment_types:
            reason = "type_not_allowed"
        elif item.file_size > scope.attachment_max:
            reason = "too_large"
        elif not path:
            reason = "not_downloaded"
        entry = {"index": 0, "name": name, "content_type": (item.content_type or "application/octet-stream")[:200],
                 "size": int(item.file_size or 0), "content_hash": (file_hash(item) or None) if not reason else
                 (item.file_sha256 or None), "indexable": not reason}
        if reason:
            entry["reason"] = reason
        return [entry]

    def render(self, item, content, doc, generation) -> dict:
        from .knowledge import _iso, instance_id
        out = {"document_id": f"{DOC_TYPE}:{item.pk}", "document_type": DOC_TYPE,
               "document_revision": doc.revision, "content_version": doc.feed_version,
               "source_instance_id": instance_id(), "generation": generation,
               "url": f"/rag/knowledge/{item.pk}/", **{k: (list(v) if isinstance(v, list) else v)
                                                       for k, v in content.items()},
               "updated_at": _iso(timezone.now())}
        out["attachments"] = [dict(a) for a in content["attachments"]]
        for a in out["attachments"]:
            if a["indexable"]:
                a["url"] = f"/api/v1/knowledge/documents/{DOC_TYPE}:{item.pk}/attachments/{a['index']}/" \
                           f"?revision={doc.revision}"
        return out

    def attachment_path(self, item, index) -> str:
        return file_path(item) if index == 0 else ""

    def reset_hash(self, item, index):
        from .models import KnowledgeItem
        KnowledgeItem.objects.filter(pk=item.pk).update(file_sha256="")
        item.file_sha256 = ""


HANDLER = Handler()

