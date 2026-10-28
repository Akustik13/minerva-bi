"""
«📂 Джерела знань» (етап 4): папки й сайти, які Minerva сама обходить і передає в базу знань (KnowledgeSource →
KnowledgeItem kind=source → тип document у стрічці для RAG; у Minerva — «❓ Запитати», огляди, картки).

Папка:
- лише всередині дозволених коренів (allowed_roots): <проєкт>/knowledge_sources (на NAS — у теці Minerva, у Docker уже
  видно), MEDIA/knowledge_sources і змонтовані шляхи з MINERVA_KNOWLEDGE_ROOTS; посилання (symlink) за межі — ні;
- файли НЕ копіюються: KnowledgeItem.source_path — шлях відносно папки, читається на місці (resolve);
- перечитується лише змінене (source_stamp = розмір + час зміни), зниклий файл → документ прибирається (tombstone);
- PDF / DOCX / TXT / CSV — текст витягує RAG (як вкладення листа), Excel / HTML / Markdown — Minerva сама (item.text).
Сайт: sitemap.xml або сторінка з посиланнями того самого сайту, до max_files сторінок; сторінку Minerva забирає сама
(лише публічні адреси, knowledge_items._download), HTML → текст, PDF → файл.
Прив'язки: клієнт / проєкт / товар з джерела для всіх документів + «артикул у назві файлу → товар».
Розклад: scan_due (cron `knowledge_sources --if-due`), «🔄 Перевірити зараз» — start_scan у фоні.
"""
import hashlib
import logging
import mimetypes
import os
import re
import threading
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

from django.conf import settings
from django.db import connection
from django.utils import timezone

logger = logging.getLogger(__name__)

DEFAULT_TYPES = ["pdf", "docx", "txt", "csv", "md", "html", "htm", "xlsx"]
MINERVA_TEXT = {"md", "markdown", "html", "htm", "xlsx"}     # текст витягує Minerva, а не RAG
TEXT_MAX = 100_000
SCAN_STALE = timedelta(hours=1)
BATCH = 300
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"'#]+)""", re.I)
_SKU_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{2,}")


class SourceError(Exception):
    """Зрозуміла людині причина (шлях поза коренями, папки немає, сайт не відповів…)."""


# ── Дозволені корені й шляхи ─────────────────────────────────────────────────

def allowed_roots() -> list:
    roots = [Path(settings.BASE_DIR) / "knowledge_sources", Path(settings.MEDIA_ROOT) / "knowledge_sources"]
    roots += [Path(p) for p in getattr(settings, "KNOWLEDGE_SOURCE_ROOTS", [])]
    out = []
    for r in roots:
        real = os.path.realpath(str(r))
        if real not in out:
            out.append(real)
    return out


def _inside(path, root) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:            # різні диски Windows
        return False


def check_folder(path) -> str:
    """Перевірена справжня адреса папки або SourceError з поясненням."""
    if not path or not os.path.isabs(path):
        raise SourceError("Потрібен повний шлях до папки, напр. /app/knowledge_sources/Dokumentation")
    real = os.path.realpath(path)
    roots = allowed_roots()
    if not any(_inside(real, r) for r in roots):
        raise SourceError("Папка поза дозволеними: " + ", ".join(roots)
                          + ". Інші папки NAS — змонтувати в Docker і додати в MINERVA_KNOWLEDGE_ROOTS.")
    if not os.path.isdir(real):
        raise SourceError("Папки немає (або Minerva її не бачить) — перевірте шлях і монтування в Docker.")
    return real


def resolve(source, rel) -> str:
    """Шлях файлу джерела — лише всередині його папки й дозволених коренів; '' — якщо файлу вже немає."""
    try:
        root = check_folder(source.path)
    except SourceError:
        return ""
    path = os.path.realpath(os.path.join(root, rel))
    return path if _inside(path, root) and os.path.isfile(path) else ""


def _types(source) -> set:
    return {t.strip().lower().lstrip(".") for t in (source.file_types or DEFAULT_TYPES) if str(t).strip()}


def _ext(name) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


# ── Текст, який Minerva витягує сама ─────────────────────────────────────────

def extract_text(path, ext) -> str:
    """Excel / HTML / Markdown → текст (PDF / DOCX / TXT / CSV читає RAG)."""
    if ext == "xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        out, size = [], 0
        try:
            for ws in wb.worksheets:
                out.append(f"## {ws.title}")
                for row in ws.iter_rows(values_only=True):
                    cells = [str(c).strip() for c in row if c not in (None, "")]
                    if cells:
                        line = " | ".join(cells)
                        out.append(line)
                        size += len(line)
                    if size > TEXT_MAX:
                        break
                if size > TEXT_MAX:
                    break
        finally:
            wb.close()
        return "\n".join(out)[:TEXT_MAX]
    with open(path, "rb") as fh:
        raw = fh.read(TEXT_MAX * 4).decode("utf-8", errors="replace")
    if ext in ("html", "htm"):
        from email_assistant.imap_client import html_to_text
        raw = html_to_text(raw)
    return re.sub(r"\n{3,}", "\n\n", raw).strip()[:TEXT_MAX]


# ── Прив'язки ────────────────────────────────────────────────────────────────

class SkuIndex:
    """Артикули каталогу для «артикул у назві файлу → товар» (один запит на перевірку джерела)."""

    def __init__(self):
        from inventory.models import Product
        self.by_sku = {}
        for pk, sku, short in Product.objects.filter(is_active=True).values_list("pk", "sku", "sku_short"):
            for code in (sku, short):
                if code and len(code) >= 4:
                    self.by_sku.setdefault(code.lower(), pk)

    def find(self, text):
        for token in _SKU_TOKEN_RE.findall(text or ""):
            for t in (token.lower(), token.lower().strip("._-/")):
                if t in self.by_sku:
                    return self.by_sku[t]
            for part in re.split(r"[_\s]+", token):          # «FT220407_Datenblatt» → FT220407
                if part.lower() in self.by_sku:
                    return self.by_sku[part.lower()]
        return None


def _links(source, name, skus):
    product_id = source.product_id or (skus.find(name) if skus else None)
    return {"customer_id": source.customer_id, "project_id": source.project_id, "product_id": product_id}


# ── Застосування ─────────────────────────────────────────────────────────────

def _apply(source, desired, fill) -> dict:
    """desired {source_path: spec}; fill(item, spec) → чи змінилось. Події стрічки — пачками в кінці."""
    from . import knowledge, knowledge_items
    from .models import KnowledgeItem
    existing = {i.source_path: i for i in source.items.all()}
    touched, created, updated = [], 0, 0
    with knowledge_items.quiet():
        for path, spec in desired.items():
            item = existing.pop(path, None)
            fresh = item is None
            if fresh:
                item = KnowledgeItem(kind=KnowledgeItem.SOURCE, auto=True, knowledge_source=source, source_path=path,
                                     owner_id=source.owner_id, source_key=f"src:{source.pk}:{path}"[:300])
            changed = fill(item, spec) or fresh
            if item.visibility != source.visibility or item.owner_id != source.owner_id:
                item.visibility, item.owner_id, changed = source.visibility, source.owner_id, True
            if changed:
                item.save()
                created += fresh
                updated += not fresh
                touched.append(item.pk)
        removed = list(existing.values())
        for item in removed:
            if item.file:                     # копія сторінки сайту (PDF) належить джерелу
                item.file.delete(save=False)
        KnowledgeItem.objects.filter(pk__in=[i.pk for i in removed]).delete()
    pks = touched + [i.pk for i in removed]
    for i in range(0, len(pks), BATCH):
        knowledge.touch_docs(knowledge_items.DOC_TYPE, pks[i:i + BATCH])
    return {"created": created, "updated": updated, "removed": len(removed)}


def _set(item, **values) -> bool:
    changed = False
    for field, value in values.items():
        if getattr(item, field) != value:
            setattr(item, field, value)
            changed = True
    return changed


def scan_folder(source) -> dict:
    root = check_folder(source.path)
    types, limit = _types(source), source.max_file_mb * 1024 * 1024
    skus = SkuIndex() if source.link_products_by_sku and not source.product_id else None
    files, skipped = {}, 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "@", "#")))   # @eaDir Synology, .git…
        if not source.recursive:
            dirnames[:] = []
        for name in sorted(filenames):
            if name.startswith((".", "~$")) or _ext(name) not in types:
                continue
            full = os.path.join(dirpath, name)
            if not _inside(os.path.realpath(full), root):
                continue
            st = os.stat(full)
            if st.st_size > limit or st.st_size == 0:
                skipped += 1
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            files[rel] = (full, st)
            if len(files) >= source.max_files:
                break
        if len(files) >= source.max_files:
            break

    def fill(item, spec):
        full, st = spec
        name = os.path.basename(full)
        stamp = f"{st.st_size}:{int(st.st_mtime)}"
        links = _links(source, item.source_path, skus)
        changed = _set(item, title=os.path.splitext(name)[0][:300] or name, file_name=name[:300], file_size=st.st_size,
                       content_type=(mimetypes.guess_type(name)[0] or "")[:200], **links)
        if item.source_stamp != stamp:
            item.source_stamp, item.file_sha256, changed = stamp, "", True
            ext = _ext(name)
            if ext in MINERVA_TEXT:
                try:
                    item.text = extract_text(full, ext)
                    item.fetch_error = ""
                except Exception as e:  # noqa: BLE001 — пошкоджений файл не зупиняє перевірку
                    item.text, item.fetch_error = "", f"Не вдалося прочитати: {type(e).__name__}"[:300]
        return changed

    result = _apply(source, files, fill)
    result["skipped"] = skipped
    return result


def _page_urls(source) -> list:
    """Сторінки сайту: зі sitemap.xml (у т. ч. вкладених) або посилання стартової сторінки того самого сайту."""
    from .knowledge_items import _download
    start = source.url.strip()
    host = urlparse(start).hostname
    final, data, ctype = _download(start)
    urls = []
    if data.lstrip().startswith(b"<?xml") or "xml" in ctype:
        queue, seen = [data], set()
        while queue and len(urls) < source.max_files:
            root = ET.fromstring(queue.pop(0))
            for loc in root.iter():
                if not loc.tag.endswith("loc") or not (loc.text or "").strip():
                    continue
                u = loc.text.strip()
                if u.endswith(".xml") and u not in seen and len(seen) < 20:     # індекс sitemap → вкладені
                    seen.add(u)
                    try:
                        queue.append(_download(u)[1])
                    except Exception:  # noqa: BLE001
                        continue
                elif urlparse(u).hostname == host:
                    urls.append(u)
    else:
        urls.append(final)
        for href in _HREF_RE.findall(data.decode("utf-8", errors="replace")):
            u = urljoin(final, href.strip())
            if urlparse(u).scheme in ("http", "https") and urlparse(u).hostname == host:
                urls.append(u.split("#")[0])
    return list(dict.fromkeys(urls))[:source.max_files]


def scan_website(source) -> dict:
    from . import knowledge_items
    skus = SkuIndex() if source.link_products_by_sku and not source.product_id else None
    pages = {u: u for u in _page_urls(source)}

    def fill(item, url):
        changed = _set(item, url=url[:2000], **_links(source, url, skus))
        previous = (item.text, item.file_sha256, item.title)
        knowledge_items.fetch(item, title_from_page=True)       # публічні адреси; HTML → текст, PDF → файл
        stamp = hashlib.sha256((item.text or item.file_sha256 or "").encode()).hexdigest()[:40]
        if item.source_stamp != stamp:
            item.source_stamp, changed = stamp, True
        return changed or previous != (item.text, item.file_sha256, item.title)

    return _apply(source, pages, fill)


# ── Запуск ───────────────────────────────────────────────────────────────────

def scan(source) -> dict:
    """Перевірити джерело зараз → лічильники; результат і помилка — у полях джерела."""
    from .models import KnowledgeSource
    started = timezone.now()
    KnowledgeSource.objects.filter(pk=source.pk).update(scan_started_at=started)
    try:
        result = scan_folder(source) if source.kind == KnowledgeSource.FOLDER else scan_website(source)
        text = (f"{timezone.localtime(started):%d.%m.%Y %H:%M}: документів {source.items.count()} · нових "
                f"{result['created']} · змінено {result['updated']} · прибрано {result['removed']}"
                + (f" · пропущено (розмір) {result['skipped']}" if result.get("skipped") else ""))
        KnowledgeSource.objects.filter(pk=source.pk).update(last_scan_at=started, last_scan_result=text[:300],
                                                            last_error="", scan_started_at=None)
        return result
    except Exception as e:  # noqa: BLE001
        message = str(e) if isinstance(e, (SourceError,)) else f"{type(e).__name__}: {e}"
        logger.warning("knowledge source %s scan failed: %s", source.pk, message)
        KnowledgeSource.objects.filter(pk=source.pk).update(last_scan_at=started, last_error=message[:300],
                                                            scan_started_at=None)
        return {"error": message}


def running(source) -> bool:
    return bool(source.scan_started_at and timezone.now() - source.scan_started_at < SCAN_STALE)


def _start_background(fn):
    threading.Thread(target=fn, daemon=True).start()


def start_scan(source) -> bool:
    """«🔄 Перевірити зараз» у фоні; False — перевірка вже йде."""
    from .models import KnowledgeSource
    if running(source):
        return False
    caller, pk = threading.current_thread(), source.pk

    def run():
        try:
            obj = KnowledgeSource.objects.filter(pk=pk).first()
            if obj is not None:
                scan(obj)
        finally:
            if threading.current_thread() is not caller:   # власне з'єднання фонового потоку
                connection.close()
    _start_background(run)
    return True


def scan_due() -> list:
    """Увімкнені джерела, які давно не перевіряли (cron: knowledge_sources --if-due)."""
    from .models import KnowledgeSource
    done = []
    now = timezone.now()
    for source in KnowledgeSource.objects.filter(enabled=True).order_by("pk"):
        if running(source) or (source.last_scan_at and now - source.last_scan_at < timedelta(hours=source.rescan_hours)):
            continue
        scan(source)
        done.append(source.pk)
    return done
