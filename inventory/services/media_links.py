"""Посилання на фото й даташити за правилом (Налаштування складу → «🔗 Посилання за правилом»).

Правило — шаблон з {key}: https://…/download.php?file={key}.pdf. {key} = «Спільний файл» товару (image_key /
datasheet_key), а якщо порожньо — артикул (SKU). Однаковий спільний файл у кількох товарах = одне посилання.

Розширення: фото бувають .jpg і .png — під час перевірки Minerva пробує спершу розширення з правила, далі інші
(EXTS) і бере те посилання, що відповідає. Спільний файл, вписаний уже з розширенням («X.png»), береться як є.

Перевірка йде у фоні (стан і звіт — MediaLinkCheck, жива панель): для кожного товару нове посилання, поточне,
чи відповідають вони (лише початок файлу: справжнє фото / PDF, а не сторінка помилки) і що з цим робити.
Записуються лише вибрані людиною рядки; потім «🖼 Фото й даташити» магазину перевірить їх як змінені.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import timedelta
from urllib.parse import quote, unquote

from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from inventory.models import InventorySettings, MediaLinkCheck, Product
from inventory.services.media_files import DATASHEET, IMAGE, probe

logger = logging.getLogger(__name__)

KINDS = (IMAGE, DATASHEET)
FIELDS = {IMAGE: ("image_url", "image_key", "image_url_template"),
          DATASHEET: ("datasheet_url", "datasheet_key", "datasheet_url_template")}
LABEL = {IMAGE: "фото", DATASHEET: "даташит"}
EXTS = {IMAGE: ("jpg", "png", "jpeg", "webp"), DATASHEET: ("pdf",)}
_TPL_EXT = re.compile(r"\{key\}\.([A-Za-z0-9]{2,5})")
STALE_RUN = timedelta(minutes=3)
_lock = threading.Lock()

# Дія для рядка звіту і чи вибрано її за замовчуванням
SAME, FILL, REPLACE, KEEP, BROKEN = "same", "fill", "replace", "keep", "broken"
ACTION_TEXT = {
    SAME: "без змін",
    FILL: "заповнити",
    REPLACE: "замінити",
    KEEP: "залишити поточне — нове не відповідає",
    BROKEN: "нове не відповідає",
}


def check_obj() -> MediaLinkCheck:
    return MediaLinkCheck.objects.get_or_create(pk=1)[0]


def templates() -> dict[str, str]:
    s = InventorySettings.get()
    return {kind: (getattr(s, FIELDS[kind][2]) or "").strip() for kind in KINDS}


def key_for(product, kind) -> str:
    return (getattr(product, FIELDS[kind][1]) or "").strip() or product.sku


def candidates(template: str, key: str, kind: str) -> list[str]:
    """Можливі посилання: спершу з розширенням правила, далі інші розширення цього виду.
    Спільний файл уже з розширенням («X.png») — лише він, без розширення правила (не «X.png.jpg»)."""
    if not template or not key:
        return []
    name = quote(key, safe="-_.~()")
    m = _TPL_EXT.search(template)
    if m is None:
        return [template.replace("{key}", name)]
    head, tail = template[:m.start()], template[m.end():]
    if re.search(r"\.(?:" + "|".join(EXTS[kind]) + r")$", key, re.I):
        return [head + name + tail]
    first = m.group(1)
    return [head + name + "." + ext + tail for ext in [first] + [e for e in EXTS[kind] if e != first.lower()]]


def build_url(template: str, key: str, kind: str) -> str:
    """Основне посилання за правилом (без перевірки)."""
    found = candidates(template, key, kind)
    return found[0] if found else ""


def resolve(found: list[str], kind: str, checked) -> tuple[str, str | None]:
    """Перше посилання, що відповідає → (url, ''); жодне — (основне, помилка); без перевірки — (основне, None).
    checked(url, kind) → '' / текст помилки / None."""
    first_error = None
    for i, url in enumerate(found):
        error = checked(url, kind)
        if error is None:
            return found[0], None
        if error == "":
            return url, ""
        if i == 0:
            first_error = error
    if len(found) > 1:
        others = ", ".join("." + u.rsplit(".", 1)[-1].split("&")[0] for u in found[1:])
        first_error = f"{first_error} (також немає {others})"
    return found[0], first_error


def example(kind: str, product=None) -> str:
    """Приклад для налаштувань: перший активний товар (або «AN050201-01C-175-MHF1»)."""
    t = templates()[kind]
    if not t:
        return ""
    product = product or Product.objects.filter(is_active=True).order_by("sku").first()
    return build_url(t, key_for(product, kind) if product else "AN050201-01C-175-MHF1", kind)


def scope_products(scope: str, ids=None):
    qs = Product.objects.filter(is_active=True)
    if scope == "selected" and ids:
        qs = Product.objects.filter(pk__in=ids)
    elif scope == "shop":
        qs = qs.filter(shop_listings__isnull=False).distinct()
    return qs.order_by("sku")


# ── Звіт ───────────────────────────────────────────────────────────────────────

def decide(current: str, new: str, new_error: str | None, current_error: str | None) -> tuple[str, bool]:
    """(дія, вибрано за замовчуванням). new_error=None — без перевірки посилань."""
    if new == current:
        return SAME, False
    if new_error is None or new_error == "":
        return (FILL if not current else REPLACE), True
    if current and current_error == "":
        return KEEP, False
    return BROKEN, False


def _save_state(state: dict, rows: list | None = None):
    data = {"state": state}
    if rows is not None:
        data["rows"] = rows
    MediaLinkCheck.objects.filter(pk=1).update(**data)


def is_running(state: dict | None) -> bool:
    state = state or {}
    if state.get("state") != "running":
        return False
    beat = parse_datetime(state.get("heartbeat") or "") if state.get("heartbeat") else None
    return bool(beat and timezone.now() - beat < STALE_RUN)


def run(scope="all", ids=None, kinds=KINDS, check_links=True, by="") -> dict:
    """Будує звіт: нове посилання за правилом, поточне, чи відповідають, що зробити."""
    check_obj()
    tpl = templates()
    kinds = [k for k in kinds if tpl.get(k)]
    products = list(scope_products(scope, ids))
    shared = {kind: {} for kind in KINDS}
    for p in Product.objects.filter(is_active=True).only("sku", "image_key", "datasheet_key"):
        for kind in KINDS:
            key = key_for(p, kind)
            shared[kind][key] = shared[kind].get(key, 0) + 1
    total = len(products) * len(kinds)
    now = timezone.now().isoformat()
    state = {"state": "running", "scope": scope, "ids": list(ids or []), "kinds": kinds, "check": check_links,
             "templates": tpl, "by": by, "started_at": now, "heartbeat": now, "finished_at": None,
             "total": total, "done": 0, "current": "підготовка…", "message": ""}
    _save_state(state, [])
    cache, rows, done = {}, [], 0

    def checked(url, kind):
        """'' — відповідає, текст — помилка, None — без перевірки. Спільні посилання перевіряються раз."""
        if not check_links:
            return None
        if (url, kind) not in cache:
            cache[(url, kind)] = probe(url, kind)
        return cache[(url, kind)]

    for p in products:
        for kind in kinds:
            done += 1
            key = key_for(p, kind)
            current = (getattr(p, FIELDS[kind][0]) or "").strip()
            found = candidates(tpl[kind], key, kind)
            state.update(done=done, current=f"{p.sku} · {LABEL[kind]}", heartbeat=timezone.now().isoformat())
            if done % 2 == 0 or done == total:
                _save_state(state)
            if current and current in found and checked(current, kind) in (None, ""):
                new, new_error = current, checked(current, kind)   # поточне вже за правилом і відповідає (.png теж)
            else:
                new, new_error = resolve(found, kind, checked)
            current_error = checked(current, kind) if current and current != new else None
            action, selected = decide(current, new, new_error, current_error)
            rows.append({"id": p.pk, "sku": p.sku, "name": (p.name or "")[:80], "kind": kind, "key": key,
                         "shared": shared[kind].get(key, 1), "current": current, "new": new,
                         "new_error": new_error, "current_error": current_error,
                         "action": action, "selected": selected})
    counts = {}
    for r in rows:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    state.update(state="done", current="", done=total, heartbeat=timezone.now().isoformat(),
                 finished_at=timezone.now().isoformat(), counts=counts)
    _save_state(state, rows)
    return state


def _start_background(fn):
    threading.Thread(target=fn, daemon=True).start()


def start(scope="all", ids=None, kinds=KINDS, check_links=True, by="") -> bool:
    check_obj()
    with transaction.atomic():
        c = MediaLinkCheck.objects.select_for_update().get(pk=1)
        if is_running(c.state):
            return False
        now = timezone.now().isoformat()
        _save_state({"state": "running", "scope": scope, "started_at": now, "heartbeat": now, "total": 0,
                     "done": 0, "current": "підготовка…", "by": by}, [])
    caller = threading.current_thread()

    def work():
        if not _lock.acquire(blocking=False):
            return
        try:
            run(scope, ids, kinds, check_links, by)
        except Exception as e:  # noqa: BLE001
            logger.exception("media links check failed: %s", e)
            _save_state({**(check_obj().state or {}), "state": "error", "current": "",
                         "finished_at": timezone.now().isoformat(), "message": f"перервано ({type(e).__name__})"})
        finally:
            _lock.release()
            if threading.current_thread() is not caller:
                connection.close()
    _start_background(work)
    return True


def progress() -> dict:
    c = check_obj()
    state = dict(c.state or {})
    if state.get("state") == "running" and not is_running(state):
        state["state"] = "interrupted"
    state["templates_now"] = templates()
    state["stale_rules"] = bool(state.get("templates")) and state["templates"] != state["templates_now"]
    return state


# ── Запис ──────────────────────────────────────────────────────────────────────

def apply(selected: list[str], user=None) -> tuple[int, list[str]]:
    """Записує вибрані рядки звіту ("<id>:<kind>"). Повертає (змінено, попередження)."""
    c = check_obj()
    warnings = []
    if (c.state or {}).get("templates") != templates():
        warnings.append("Правило змінилось після перевірки — записано посилання зі звіту; перевірте заново.")
    wanted = set(selected)
    rows = list(c.rows or [])
    changed_products = {}
    for r in rows:
        if f'{r["id"]}:{r["kind"]}' not in wanted or r["action"] == SAME or not r["new"]:
            continue
        changed_products.setdefault(r["id"], {})[FIELDS[r["kind"]][0]] = r["new"]
    n = 0
    for p in Product.objects.filter(pk__in=changed_products):
        fields = [f for f, v in changed_products[p.pk].items() if getattr(p, f) != v]
        for f in fields:
            setattr(p, f, changed_products[p.pk][f])
        if fields:
            p.save(update_fields=fields)   # по одному — сигнали (вебхук каталогу, фото для сайту)
            n += 1
    for r in rows:   # записане тепер «без змін»
        if r["id"] in changed_products and FIELDS[r["kind"]][0] in changed_products[r["id"]]:
            r["current"], r["current_error"], r["action"], r["selected"] = r["new"], r["new_error"], SAME, False
    MediaLinkCheck.objects.filter(pk=1).update(rows=rows)
    return n, warnings


def set_shared(products, kinds, key: str) -> dict:
    """Дія «🖼 Спільне фото / даташит»: один файл для вибраних товарів + посилання за правилом одразу.
    Повертає {kind: (url, помилка перевірки або None — правило не задано)}."""
    key = (key or "").strip()
    tpl = templates()
    out = {}
    for kind in kinds:
        url_field, key_field, _ = FIELDS[kind]
        found = candidates(tpl[kind], key, kind)
        url, error = resolve(found, kind, probe) if found else ("", None)
        for p in products:
            fields = []
            own = "" if key == p.sku else key
            if getattr(p, key_field) != own:
                setattr(p, key_field, own)
                fields.append(key_field)
            if url and getattr(p, url_field) != url:
                setattr(p, url_field, url)
                fields.append(url_field)
            if fields:
                p.save(update_fields=fields)
        out[kind] = (url, error if found else None)
    return out


def key_from_url(template: str, url: str, kind: str) -> str | None:
    """Назва файлу з посилання, якщо воно за правилом («…?file=X.png» → «X.png», «…?file=X.jpg» при правилі
    {key}.jpg → «X»); інакше None."""
    if not template or "{key}" not in template or not url:
        return None
    m = _TPL_EXT.search(template)
    head, tail = (template[:m.start()], template[m.end():]) if m else template.split("{key}", 1)
    if not url.startswith(head) or (tail and not url.endswith(tail)) or len(url) <= len(head) + len(tail):
        return None
    name = unquote(url[len(head):len(url) - len(tail)] if tail else url[len(head):])
    if m and name.lower().endswith("." + m.group(1).lower()):
        name = name[:-len(m.group(1)) - 1]   # розширення правила — допише саме правило
    return name if name and "/" not in name and "?" not in name else None


def set_link(products, kind, url: str) -> tuple[str, str, bool]:
    """Дія «🔗 Спільне фото / даташит»: одне готове посилання для вибраних товарів.
    Посилання за правилом — ще й «спільний файл», щоб перевірка «за правилом» потім його не міняла.
    Повертає (url, помилка перевірки або '', чи посилання за правилом)."""
    url = (url or "").strip()
    url_field, key_field, _ = FIELDS[kind]
    key = key_from_url(templates()[kind], url, kind)
    for p in products:
        fields = []
        if getattr(p, url_field) != url:
            setattr(p, url_field, url)
            fields.append(url_field)
        if key is not None:
            own = "" if key == p.sku else key
            if getattr(p, key_field) != own:
                setattr(p, key_field, own)
                fields.append(key_field)
        if fields:
            p.save(update_fields=fields)
    return url, probe(url, kind), key is not None


def shared_groups() -> dict[str, list[tuple[str, list[str]]]]:
    """{вид: [(спільний файл, [артикули])]} — файли, що використовують кілька товарів."""
    out = {}
    for kind in KINDS:
        groups = {}
        for p in Product.objects.filter(is_active=True).only("sku", "image_key", "datasheet_key").order_by("sku"):
            groups.setdefault(key_for(p, kind), []).append(p.sku)
        out[kind] = sorted(((k, v) for k, v in groups.items() if len(v) > 1), key=lambda x: x[0])
    return out
