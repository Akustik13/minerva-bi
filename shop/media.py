"""Фото й даташити з Minerva для сайтів (сторінка «🖼 Фото й даташити», API /shop/media/…).

Порядок джерел для кожного товару й виду (фото / даташит):
  1. посилання з картки товару (image_url / datasheet_url) — якщо відповідає і вміст правильний;
  2. файл, завантажений у картку товару (image / datasheet_file);
  3. нічого придатного — сайт показує власний файл (assets/products, assets/datasheets).
Тип перевіряється за вмістом (inventory/services/media_files.py). Перевірена копія лежить за SHA-256
(MEDIA_ROOT/shop_media/<sha256>.<ext>); сайт забирає її через GET /api/v1/shop/media/<sha256>/, зберігає
в себе і звітує POST /shop/media/status/ — так видно, що вже є на сайті.

Джерело тимчасово недоступне, а посилання й файл у картці не змінювались — сайт і далі отримує
останню перевірену копію (stale). Змінили посилання / файл — перевіряється заново.

Індексація: кнопки сторінки (у фоні, стан у ShopMediaSettings.run — живий прогрес) і cron
`index_shop_media --if-due` (нові, змінені в картці та давно не перевірені посилання; лише коли ввімкнено).
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from datetime import timedelta
from urllib.parse import urlsplit

import requests
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from inventory.models import Product
from inventory.services.media_files import DATASHEET, IMAGE, USER_AGENT, MediaError, check, fetch, read_field

from .models import ProductMedia, Shop, ShopMediaSettings, ShopMediaSite

logger = logging.getLogger(__name__)

KINDS = (IMAGE, DATASHEET)
KIND_LABEL = {IMAGE: "фото", DATASHEET: "даташит"}
STORE_DIR = "shop_media"
STALE_RUN = timedelta(minutes=3)     # немає «пульсу» довше — запуск перервано (наприклад, перезапуск сервера)
AUTO_EVERY = timedelta(minutes=5)    # cron: не частіше
AUTO_LIMIT = 100                     # cron: скільки перевірок за прохід
_SHA = re.compile(r"[0-9a-f]{64}")
_lock = threading.Lock()


def settings_obj() -> ShopMediaSettings:
    return ShopMediaSettings.objects.get_or_create(pk=1)[0]


def _sources(product, kind):
    if kind == IMAGE:
        return (product.image_url or "").strip(), product.image
    return (product.datasheet_url or "").strip(), product.datasheet_file


def fingerprint(product, kind) -> str:
    """Посилання й ім'я файлу з картки: інше значення → перевірити знову."""
    url, field = _sources(product, kind)
    return f"{url}|{field.name if field else ''}"[:700]


def site_products():
    """Товари, які отримують сайти: активні, у видимому асортименті хоча б одного активного магазину."""
    return Product.objects.filter(is_active=True, shop_listings__is_visible=True,
                                  shop_listings__shop__is_active=True).distinct()


# ── Перевірка одного товару ─────────────────────────────────────────────────────

def _clear(row, now):
    if row.sha256:
        row.changed_at = now
    row.sha256, row.size, row.content_type, row.stored, row.source, row.stale = "", None, "", "", "", False


def index_one(product, kind, row=None) -> ProductMedia:
    """Перевіряє джерела за порядком і зберігає результат (+ копію файлу)."""
    if row is None:
        row = ProductMedia.objects.get_or_create(product=product, kind=kind)[0]
    url, field = _sources(product, kind)
    has_file = bool(field and field.name)
    inputs = fingerprint(product, kind)
    data, source, url_error, file_error = None, "", "", ""
    if url:
        try:
            data, source = fetch(url, kind), "url"
        except MediaError as e:
            url_error = str(e)
    if data is None and has_file:
        try:
            data, source = read_field(field, kind), "file"
        except MediaError as e:
            file_error = str(e)
    now = timezone.now()
    row.url_error, row.file_error, row.checked_at = url_error[:300], file_error[:300], now
    if data is not None:
        ext, ctype = check(data, kind)
        sha = hashlib.sha256(data).hexdigest()
        name = f"{STORE_DIR}/{sha}.{ext}"
        if not default_storage.exists(name):
            name = default_storage.save(name, ContentFile(data))
        if sha != row.sha256:
            row.changed_at = now
        row.sha256, row.size, row.content_type, row.stored = sha, len(data), ctype, name
        row.source, row.stale = source, False
        row.status = ProductMedia.FALLBACK if (source == "file" and url) else ProductMedia.OK
    elif not url and not has_file:
        _clear(row, now)
        row.status = ProductMedia.NONE
    elif row.usable and row.inputs == inputs and default_storage.exists(row.stored):
        row.status, row.stale = ProductMedia.ERROR, True   # тимчасово недоступно — лишаємо перевірену копію
    else:
        _clear(row, now)
        row.status = ProductMedia.ERROR
    row.inputs = inputs
    row.save()
    return row


def index_product(product) -> list[ProductMedia]:
    """Кнопка «🔄 Перевірити зараз» у картці товару."""
    rows = {r.kind: r for r in ProductMedia.objects.filter(product=product)}
    return [index_one(product, kind, rows.get(kind)) for kind in KINDS]


# ── Що перевіряти ──────────────────────────────────────────────────────────────

def due(mode="changed", products=None, limit=None) -> list[tuple]:
    """[(товар, вид, рядок або None)] для перевірки.
    changed — ще не перевірені, змінені в картці, «чекає перевірки», давно не перевірені посилання / помилки;
    all — усе заново."""
    s = settings_obj()
    qs = products if products is not None else site_products()
    recheck = timezone.now() - timedelta(days=s.recheck_days) if s.recheck_days else None
    out = []
    for p in qs.prefetch_related("shop_media").order_by("sku"):
        rows = {r.kind: r for r in p.shop_media.all()}
        for kind in KINDS:
            r = rows.get(kind)
            if (mode == "all" or r is None or r.status == ProductMedia.PENDING
                    or r.inputs != fingerprint(p, kind)):
                out.append((p, kind, r))
            elif (recheck and r.checked_at and r.checked_at < recheck
                  and (r.source == "url" or r.status in (ProductMedia.ERROR, ProductMedia.FALLBACK))):
                out.append((p, kind, r))
            if limit and len(out) >= limit:
                return out
    return out


# ── Запуск і стан ──────────────────────────────────────────────────────────────

def _iso(dt):
    return dt.isoformat() if dt else None


def _save_run(run: dict):
    ShopMediaSettings.objects.filter(pk=1).update(run=run)


def is_running(run: dict | None) -> bool:
    run = run or {}
    if run.get("state") != "running":
        return False
    beat = parse_datetime(run.get("heartbeat") or "") if run.get("heartbeat") else None
    return bool(beat and timezone.now() - beat < STALE_RUN)


def run(mode="changed", products=None, limit=None, by="") -> dict:
    """Перевіряє due(...) по черзі, після кожного — стан для живої панелі."""
    settings_obj()
    items = due(mode, products, limit)
    counts = {"ok": 0, "fallback": 0, "none": 0, "error": 0, "changed": 0}
    now = timezone.now()
    state = {"state": "running", "mode": mode, "by": by, "started_at": _iso(now), "heartbeat": _iso(now),
             "finished_at": None, "total": len(items), "done": 0, "current": "підготовка…", "counts": counts,
             "message": ""}
    _save_run(state)
    for i, (product, kind, row) in enumerate(items, 1):
        state["current"] = f"{product.sku} · {KIND_LABEL[kind]}"
        state["heartbeat"] = _iso(timezone.now())
        _save_run(state)
        before = row.sha256 if row else ""
        try:
            row = index_one(product, kind, row)
        except Exception:  # noqa: BLE001 — один товар не зупиняє прохід
            logger.exception("shop media: %s %s failed", product.sku, kind)
            counts["error"] += 1
        else:
            counts[row.status if row.status in counts else "error"] += 1
            if row.sha256 != before:
                counts["changed"] += 1
        state["done"] = i
    removed = cleanup_store()
    state.update(state="done", current="", heartbeat=_iso(timezone.now()), finished_at=_iso(timezone.now()),
                 message=f"видалено старих копій: {removed}" if removed else "")
    _save_run(state)
    if counts["changed"]:
        ping_sites()
    return state


def _start_background(fn):
    threading.Thread(target=fn, daemon=True).start()


def start(mode="changed", product_ids=None, by="") -> bool:
    """Кнопки сторінки: запуск у фоні. False — уже йде інша індексація."""
    settings_obj()
    with transaction.atomic():
        s = ShopMediaSettings.objects.select_for_update().get(pk=1)
        if is_running(s.run):
            return False
        now = _iso(timezone.now())
        _save_run({"state": "running", "mode": mode, "by": by, "started_at": now, "heartbeat": now,
                   "finished_at": None, "total": 0, "done": 0, "current": "підготовка…", "counts": {}, "message": ""})
    caller = threading.current_thread()

    def work():
        if not _lock.acquire(blocking=False):
            return
        try:
            products = Product.objects.filter(pk__in=product_ids) if product_ids else None
            run(mode, products=products, by=by)
        except Exception as e:  # noqa: BLE001
            logger.exception("shop media indexing failed: %s", e)
            _save_run({**(settings_obj().run or {}), "state": "error", "current": "",
                       "finished_at": _iso(timezone.now()), "message": f"перервано ({type(e).__name__})"})
        finally:
            _lock.release()
            if threading.current_thread() is not caller:   # власне з'єднання фонового потоку
                connection.close()
    _start_background(work)
    return True


def auto(limit=AUTO_LIMIT) -> dict | None:
    """cron (`index_shop_media --if-due`): нові, змінені й давно не перевірені — лише коли ввімкнено."""
    s = settings_obj()
    now = timezone.now()
    if not s.enabled or is_running(s.run) or (s.auto_at and now - s.auto_at < AUTO_EVERY):
        return None
    ShopMediaSettings.objects.filter(pk=1).update(auto_at=now)
    if not due("changed", limit=1):
        return None   # нічого робити — останній ручний запуск лишається на панелі
    return run("changed", limit=limit, by="автоматично")


def cleanup_store() -> int:
    """Копії, на які вже не посилається жоден рядок."""
    try:
        _dirs, files = default_storage.listdir(STORE_DIR)
    except (FileNotFoundError, NotADirectoryError, OSError, NotImplementedError):
        return 0
    used = set(ProductMedia.objects.exclude(stored="").values_list("stored", flat=True))
    removed = 0
    for f in files:
        name = f"{STORE_DIR}/{f}"
        if name not in used and re.fullmatch(r"[0-9a-f]{64}(?:_\w+)?\.[a-z]{3,4}", f):
            default_storage.delete(name)
            removed += 1
    return removed


# ── Для API ────────────────────────────────────────────────────────────────────

def payload(row: ProductMedia | None, request=None) -> dict | None:
    """Поле media.image / media.datasheet у GET /shop/products/."""
    if row is None or not row.usable:
        return None
    url = reverse("api-shop-media-file", args=[row.sha256])
    return {"sha256": row.sha256, "size": row.size, "content_type": row.content_type,
            "ext": row.stored.rsplit(".", 1)[-1], "source": row.source, "stale": row.stale,
            "updated_at": row.changed_at, "url": request.build_absolute_uri(url) if request else url}


def file_for(shop: Shop, sha: str) -> ProductMedia | None:
    """Копія для завантаження сайтом: лише товари, видимі в його магазині, і лише коли ввімкнено."""
    if not _SHA.fullmatch(sha or "") or not settings_obj().enabled:
        return None
    row = (ProductMedia.objects.filter(sha256=sha, product__is_active=True, product__shop_listings__shop=shop,
                                       product__shop_listings__is_visible=True).exclude(stored="").first())
    if row is None or not default_storage.exists(row.stored):
        return None
    return row


def wanted(shop: Shop) -> dict[str, ProductMedia]:
    """{sha256: рядок} — що сайт магазину має отримати."""
    if not settings_obj().enabled:
        return {}
    rows = (ProductMedia.objects.filter(product__is_active=True, product__shop_listings__shop=shop,
                                        product__shop_listings__is_visible=True).exclude(stored="")
            .select_related("product").distinct())
    return {r.sha256: r for r in rows}


def _same_site(shop: Shop, url: str) -> bool:
    if not url or not shop.url:
        return False
    a, b = urlsplit(url), urlsplit(shop.url)
    return a.scheme in ("http", "https") and bool(a.hostname) and a.hostname == b.hostname


def record_report(shop: Shop, data: dict) -> dict:
    """POST /shop/media/status/ — сайт повідомляє, що зберіг у себе."""
    have = [h for h in (data.get("have") or []) if isinstance(h, str) and _SHA.fullmatch(h)][:5000]
    failed = {k: str(v)[:200] for k, v in (data.get("failed") or {}).items()
              if isinstance(k, str) and _SHA.fullmatch(k)} if isinstance(data.get("failed"), dict) else {}
    failed = dict(list(failed.items())[:500])
    sync_url = str(data.get("sync_url") or "").strip()[:200]
    site, _ = ShopMediaSite.objects.get_or_create(shop=shop)
    site.reported_at, site.have, site.failed = timezone.now(), have, failed
    if _same_site(shop, sync_url):
        site.sync_url = sync_url
    site.save()
    want = wanted(shop)
    return {"ok": True, "wanted": len(want), "have": len(set(have) & set(want)), "sync_url_accepted": site.sync_url == sync_url}


def ping_site(shop: Shop) -> str:
    """Попросити сайт забрати нові файли (GET адреси, яку сайт повідомив сам). '' — успішно."""
    site = ShopMediaSite.objects.filter(shop=shop).first()
    if not site or not _same_site(shop, site.sync_url):
        return "сайт ще не повідомив адресу синхронізації"
    error = ""
    try:
        r = requests.get(site.sync_url, params={"from": "minerva"}, timeout=(5, 90),
                         headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        if r.status_code != 200:
            error = f"сайт відповів HTTP {r.status_code}"
    except requests.exceptions.RequestException as e:
        error = f"немає відповіді від сайту ({type(e).__name__})"
    ShopMediaSite.objects.filter(pk=site.pk).update(ping_at=timezone.now(), ping_error=error[:255])
    return error


def ping_sites(background=True):
    shops = list(Shop.objects.filter(is_active=True, media_site__sync_url__gt=""))
    if not shops:
        return
    caller = threading.current_thread()

    def work():
        try:
            for shop in shops:
                ping_site(shop)
        finally:
            if threading.current_thread() is not caller:
                connection.close()
    if background:
        _start_background(work)
    else:
        work()


# ── Для сторінки й картки товару ───────────────────────────────────────────────

def summary() -> dict:
    """Підсумок за видами: скільки готово / з файлу / немає / помилок / не перевірено."""
    products = site_products()
    n_products = products.count()
    out = {}
    for kind in KINDS:
        rows = ProductMedia.objects.filter(kind=kind, product__in=products)
        c = {"ok": 0, "fallback": 0, "none": 0, "error": 0, "pending": 0, "stale": 0, "url": 0, "file": 0}
        total = 0
        for status, source, stale in rows.values_list("status", "source", "stale"):
            total += 1
            c[status] = c.get(status, 0) + 1
            if source:
                c[source] += 1
            if stale:
                c["stale"] += 1
        c["unchecked"] = max(n_products - total, 0)
        out[kind] = c
    out["products"] = n_products
    return out


def sites_status() -> list[dict]:
    out = []
    for shop in Shop.objects.filter(is_active=True).order_by("-is_default", "name"):
        want = wanted(shop)
        site = ShopMediaSite.objects.filter(shop=shop).first()
        have = set(site.have) if site else set()
        failed = (site.failed or {}) if site else {}
        problems = [f"{want[h].product.sku} · {KIND_LABEL.get(want[h].kind, want[h].kind)}: {msg}"
                    for h, msg in failed.items() if h in want][:5]
        out.append({"shop": shop.pk, "name": str(shop), "wanted": len(want), "have": len(have & set(want)),
                    "failed": len([h for h in failed if h in want]), "problems": problems,
                    "reported_at": _iso(site.reported_at) if site else None,
                    "sync_url": site.sync_url if site else "", "ping_at": _iso(site.ping_at) if site else None,
                    "ping_error": site.ping_error if site else ""})
    return out


def progress() -> dict:
    s = settings_obj()
    run_state = dict(s.run or {})
    if run_state.get("state") == "running" and not is_running(run_state):
        run_state["state"] = "interrupted"
    return {"run": run_state, "enabled": s.enabled, "recheck_days": s.recheck_days,
            "auto_at": _iso(s.auto_at), "summary": summary(), "sites": sites_status()}


def on_site(row: ProductMedia, sites=None) -> list[tuple[str, str]]:
    """[(магазин, ✅/⏳/❌ …)] — чи забрав сайт цю копію. sites — звіти, уже прочитані для списку."""
    if not row.usable:
        return []
    if sites is None:
        sites = ShopMediaSite.objects.filter(shop__is_active=True).select_related("shop")
    out = []
    for site in sites:
        if row.sha256 in (site.have or []):
            out.append((str(site.shop), "✅ на сайті"))
        elif row.sha256 in (site.failed or {}):
            out.append((str(site.shop), "❌ " + site.failed[row.sha256]))
        else:
            out.append((str(site.shop), "⏳ ще не забрав"))
    return out
