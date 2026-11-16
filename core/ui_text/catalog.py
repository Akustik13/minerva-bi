"""Каталог написів інтерфейсу в базі (core.models.UIText): синхронізація з кодом і словник для браузера.

`sync()` — зібрати написи з коду (extract.collect) і записати: нові — додати, наявні — оновити «де / скільки», зниклі з
коду — `active=False` (переклади лишаються: напис може повернутись). `dictionary(lang)` — {exact: {напис: переклад},
patterns: [[напис з {0}, переклад з {0}], …]} з активних перекладених і комплекту (bundled.jsonl); кеш у
core.shared_cache за версією (`version()` змінюється з кожною зміною каталогу, перекладів чи файлу комплекту —
адреса словника в сторінці з ?v=, тож браузер кешує його надовго).

`ensure_synced()` — код оновили без перезапуску контейнера («🚀 Повне оновлення» на сторінці «Резервні копії»: git pull
і reload gunicorn, entrypoint.sh не виконується) → перша ж сторінка запускає `sync()` у фоні, раз на зміну коду
(`code_stamp`: комплект перекладів і коміт git). Без цього нові написи й переклади з комплекту не доходили до каталогу,
і сторінка змішувала мови.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time

from django.conf import settings
from django.db.models import Max
from django.utils import timezone

TARGETS = {"en": "English", "de": "Deutsch"}      # російську за рішенням власника не перекладаємо
VERSION_KEY = "ui-text:version"
DICT_KEY = "ui-text:dict:{lang}:{ver}"
SYNCED_KEY = "ui-text:synced"                     # code_stamp, з яким каталог зібрано востаннє
SYNC_LOCK = "ui-text:sync-lock"
SYNC_LOCK_SECONDS = 15 * 60                       # збій — наступна спроба не раніше
STAMP_MEMO = 30                                   # с: code_stamp у процесі не частіше
BACKGROUND = True                                 # тести — False: sync одразу
logger = logging.getLogger(__name__)
_stamp = {"at": 0.0, "value": ""}


def _cache():
    from core.shared_cache import cache
    return cache


def key_of(source: str) -> str:
    return hashlib.sha1(source.encode("utf-8")).hexdigest()


def sync(base_dir=None, bundled_path=None) -> dict:
    """Каталог ← код, далі переклади з комплекту (bundled.apply — лише там, де перекладу немає).
    {"total", "new", "gone", "back", "bundled"}."""
    from core.models import UIText
    from core.ui_text import bundled
    from core.ui_text.extract import collect
    stamp = code_stamp(fresh=True)
    found = collect(base_dir or settings.BASE_DIR)
    by_key = {key_of(src): (src, meta) for src, meta in found.items()}
    existing = {r.key: r for r in UIText.objects.only("pk", "key", "where", "uses", "active")}
    new, changed, back = [], [], 0
    for key, (src, meta) in by_key.items():
        row = existing.get(key)
        if row is None:
            new.append(UIText(key=key, source=src, where=meta["where"][:300], uses=meta["count"]))
            continue
        if not row.active:
            back += 1
        if (row.where, row.uses, row.active) != (meta["where"][:300], meta["count"], True):
            row.where, row.uses, row.active = meta["where"][:300], meta["count"], True
            changed.append(row)
    UIText.objects.bulk_create(new, batch_size=500)
    UIText.objects.bulk_update(changed, ["where", "uses", "active"], batch_size=500)
    gone = UIText.objects.filter(active=True).exclude(key__in=list(by_key)).update(active=False)
    bump()
    res = {"total": len(by_key), "new": len(new), "gone": gone, "back": back,
           "bundled": bundled.apply(bundled_path)}
    if stamp and base_dir is None and bundled_path is None:
        _cache().set(SYNCED_KEY, stamp, 365 * 24 * 3600)
    return res


def _git_head(base) -> str:
    """Коміт, на якому код (.git/HEAD → гілка → коміт; packed-refs); не вдалося — ''."""
    try:
        git = os.path.join(str(base), ".git")
        if os.path.isfile(git):                                    # worktree: «gitdir: …»
            with open(git, encoding="utf-8") as fh:
                line = fh.read().strip()
            git = line.split(":", 1)[1].strip() if line.startswith("gitdir:") else git
        with open(os.path.join(git, "HEAD"), encoding="utf-8") as fh:
            head = fh.read().strip()
        if not head.startswith("ref:"):
            return head
        ref = head[4:].strip()
        common = git
        if os.path.isfile(os.path.join(git, "commondir")):
            with open(os.path.join(git, "commondir"), encoding="utf-8") as fh:
                common = os.path.normpath(os.path.join(git, fh.read().strip()))
        for where in (git, common):
            path = os.path.join(where, *ref.split("/"))
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    return fh.read().strip()
        with open(os.path.join(common, "packed-refs"), encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    except (OSError, IndexError, ValueError):
        pass
    return ""


def code_stamp(fresh: bool = False) -> str:
    """Що зараз у коді: комплект перекладів (розмір і час зміни) + коміт git. Змінилось — каталог треба зібрати."""
    from core.ui_text import bundled
    now = time.monotonic()
    if not fresh and _stamp["value"] and now - _stamp["at"] < STAMP_MEMO:
        return _stamp["value"]
    try:
        st = os.stat(bundled.PATH)
        files = f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        files = "-"
    value = f"{files}|{_git_head(settings.BASE_DIR)}"
    _stamp.update(at=now, value=value)
    return value


def ensure_synced() -> bool:
    """Каталог зібрано з поточним кодом? Ні — `sync()` у фоні (один процес, один раз на зміну коду). → True — запущено.
    Вимкнути — settings.UI_TEXT_AUTOSYNC = False (тести)."""
    if not getattr(settings, "UI_TEXT_AUTOSYNC", True):
        return False
    cache = _cache()
    stamp = code_stamp()
    if not stamp or cache.get(SYNCED_KEY) == stamp:
        return False
    if not cache.add(SYNC_LOCK, stamp, SYNC_LOCK_SECONDS):
        return False

    def work():
        from django.db import close_old_connections
        try:
            res = sync()
            cache.set(SYNCED_KEY, stamp, 365 * 24 * 3600)
            cache.delete(SYNC_LOCK)
            logger.info("ui_text: каталог зібрано після оновлення коду: %s", res)
        except Exception:                          # наступна спроба — коли мине SYNC_LOCK_SECONDS
            logger.warning("ui_text: не вдалося зібрати каталог після оновлення коду", exc_info=True)
        finally:
            if BACKGROUND:
                close_old_connections()

    if BACKGROUND:
        threading.Thread(target=work, name="ui-text-sync", daemon=True).start()
    else:
        work()
    return True


def counts() -> dict:
    """{"total": активних, "missing": {мова: без перекладу}, "human": {мова: виправлено людиною}}."""
    from core.models import UIText
    rows = list(UIText.objects.filter(active=True).values_list("translations", flat=True))
    out = {"total": len(rows), "missing": {}, "human": {}}
    for lang in TARGETS:
        out["missing"][lang] = sum(1 for t in rows if not ((t or {}).get(lang) or {}).get("text"))
        out["human"][lang] = sum(1 for t in rows if ((t or {}).get(lang) or {}).get("by") == "human")
    return out


def version() -> str:
    """Версія словника: нова з кожною зміною (bump); кеш загубився — з бази (кількість + остання зміна);
    плюс відбиток файлу комплекту (оновили код — новий словник і без «🔄 Зібрати»)."""
    from core.ui_text import bundled
    cache = _cache()
    ver = cache.get(VERSION_KEY)
    if not ver:
        from core.models import UIText
        agg = UIText.objects.aggregate(last=Max("updated_at"))
        stamp = int(agg["last"].timestamp() * 1e6) if agg["last"] else 0
        ver = f"{UIText.objects.count()}-{stamp:x}"
        cache.set(VERSION_KEY, ver, 30 * 24 * 3600)
    return f"{ver}.{bundled.tag()}"


def bump():
    import uuid
    _cache().set(VERSION_KEY, uuid.uuid4().hex[:12], 30 * 24 * 3600)


def dictionary(lang: str) -> dict:
    """Словник для браузера: активні написи каталогу з перекладом цією мовою + комплект (bundled.jsonl).

    Комплект — для написів без перекладу, для перекладів, що прийшли з комплекту раніше (його виправили), і для
    написів, яких каталог ще не має (код оновили, а «🔄 Зібрати» не було): інакше фрази бракує цілої, і браузер
    лишає її українською. Людські правки й «🤖 Перекласти» — головніші за комплект."""
    if lang not in TARGETS:
        return {"exact": {}, "patterns": []}
    cache = _cache()
    key = DICT_KEY.format(lang=lang, ver=version())
    hit = cache.get(key)
    if hit is not None:
        return hit
    from core.models import UIText
    from core.ui_text import bundled
    from core.ui_text.translate import valid
    pack = bundled.load()

    def packed(src):
        text = (pack.get(src) or {}).get(lang)
        return text if text and valid(src, text) else None

    exact, patterns, known = {}, [], set()
    for src, tr in UIText.objects.filter(active=True).values_list("source", "translations").iterator():
        known.add(src)
        entry = (tr or {}).get(lang) or {}
        text = entry.get("text")
        if not text or bundled.from_bundle(entry):
            text = packed(src) or text
        if not text:
            continue
        if "{0}" in src:
            patterns.append([src, text])
        else:
            exact[src] = text
    for src in pack:
        text = None if src in known else packed(src)
        if not text:
            continue
        if "{0}" in src:
            patterns.append([src, text])
        else:
            exact[src] = text
    patterns.sort(key=lambda p: -len(p[0]))            # довші шаблони — точніші, пробуються першими
    data = {"exact": exact, "patterns": patterns}
    cache.set(key, data, 7 * 24 * 3600)
    return data


def page_info(lang: str) -> dict | None:
    """Для <head>: версія словника, якщо ця мова перекладається і переклади вже є; інакше None."""
    if lang not in TARGETS:
        return None
    ver = version()
    cache = _cache()
    ck = f"ui-text:has:{lang}:{ver}"
    has = cache.get(ck)
    if has is None:
        d = dictionary(lang)
        has = bool(d["exact"] or d["patterns"])
        cache.set(ck, has, 24 * 3600)
    return {"ver": ver} if has else None


def as_js(lang: str) -> str:
    return "window.MV_UI_DICT=" + json.dumps(dictionary(lang), ensure_ascii=False,
                                             separators=(",", ":")).replace("</", "<\\/") + ";"


def set_text(row, lang: str, text: str, by: str = "human", model: str = "") -> bool:
    """Переклад одного напису (людина або AI). Порожньо — прибрати. True — змінилось."""
    tr = dict(row.translations or {})
    text = (text or "").strip()
    old = (tr.get(lang) or {}).get("text", "")
    if text == old and (not text or (tr.get(lang) or {}).get("by") == by):
        return False
    if text:
        tr[lang] = {"text": text, "by": by, "at": timezone.now().isoformat(timespec="seconds"),
                    **({"model": model} if model else {})}
    else:
        tr.pop(lang, None)
    row.translations = tr
    row.save(update_fields=["translations", "updated_at"])
    bump()
    return True
