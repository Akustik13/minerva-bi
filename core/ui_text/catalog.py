"""Каталог написів інтерфейсу в базі (core.models.UIText): синхронізація з кодом і словник для браузера.

`sync()` — зібрати написи з коду (extract.collect) і записати: нові — додати, наявні — оновити «де / скільки», зниклі з
коду — `active=False` (переклади лишаються: напис може повернутись). `dictionary(lang)` — {exact: {напис: переклад},
patterns: [[напис з {0}, переклад з {0}], …]} лише з активних перекладених; кеш у core.shared_cache за версією
(`version()` змінюється з кожною зміною каталогу чи перекладів — адреса словника в сторінці з ?v=, тож браузер
кешує його надовго).
"""
from __future__ import annotations

import hashlib
import json

from django.conf import settings
from django.db.models import Max
from django.utils import timezone

TARGETS = {"en": "English", "de": "Deutsch"}      # російську за рішенням власника не перекладаємо
VERSION_KEY = "ui-text:version"
DICT_KEY = "ui-text:dict:{lang}:{ver}"


def _cache():
    from core.shared_cache import cache
    return cache


def key_of(source: str) -> str:
    return hashlib.sha1(source.encode("utf-8")).hexdigest()


def sync(base_dir=None) -> dict:
    """Каталог ← код. {"total", "new", "gone", "back"}."""
    from core.models import UIText
    from core.ui_text.extract import collect
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
    return {"total": len(by_key), "new": len(new), "gone": gone, "back": back}


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
    """Версія словника: нова з кожною зміною (bump); кеш загубився — з бази (кількість + остання зміна)."""
    cache = _cache()
    ver = cache.get(VERSION_KEY)
    if not ver:
        from core.models import UIText
        agg = UIText.objects.aggregate(last=Max("updated_at"))
        stamp = int(agg["last"].timestamp() * 1e6) if agg["last"] else 0
        ver = f"{UIText.objects.count()}-{stamp:x}"
        cache.set(VERSION_KEY, ver, 30 * 24 * 3600)
    return ver


def bump():
    import uuid
    _cache().set(VERSION_KEY, uuid.uuid4().hex[:12], 30 * 24 * 3600)


def dictionary(lang: str) -> dict:
    """Словник для браузера: лише активні написи з перекладом цією мовою."""
    if lang not in TARGETS:
        return {"exact": {}, "patterns": []}
    cache = _cache()
    key = DICT_KEY.format(lang=lang, ver=version())
    hit = cache.get(key)
    if hit is not None:
        return hit
    from core.models import UIText
    exact, patterns = {}, []
    for src, tr in UIText.objects.filter(active=True).values_list("source", "translations").iterator():
        text = ((tr or {}).get(lang) or {}).get("text")
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
