"""Переклади в комплекті з кодом (core/ui_text/bundled.jsonl) — без AI і без оплати.

Файл — рядок на напис: {"uk": напис, "en": …, "de": …}, за абеткою. `apply()` заповнює ними написи каталогу
(core.models.UIText), яким бракує перекладу цією мовою: людські правки й наявні переклади (AI чи свої) не
змінюються, переклад з іншими місцями вставок ({0}, {1}…) не береться. Викликає `catalog.sync()` — тобто
«🔄 Зібрати» і запуск контейнера (entrypoint.sh → `manage.py ui_texts collect`), тож після оновлення нові написи
вже перекладені. Позначка — by «ai», model «Claude Code (у комплекті)»; виправити можна, як будь-який переклад.

Нові написи в коді → `manage.py ui_texts bundled --todo <файл>` (чого бракує в комплекті) → перекласти й дописати
сюди (`write`).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from django.utils import timezone

PATH = Path(__file__).with_name("bundled.jsonl")
MODEL = "Claude Code (у комплекті)"
_cache = {"stamp": None, "data": {}}


def load(path=None) -> dict:
    """{напис: {мова: переклад}} (кеш до зміни файлу); файлу немає — {}."""
    path = Path(path or PATH)
    try:
        stat = os.stat(path)
    except OSError:
        return {}
    stamp = (str(path), stat.st_mtime_ns, stat.st_size)
    if _cache["stamp"] == stamp:
        return _cache["data"]
    from core.ui_text.catalog import TARGETS
    data = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            src = row.get("uk") if isinstance(row, dict) else None
            if isinstance(src, str) and src:
                data[src] = {lang: str(row[lang]) for lang in TARGETS if isinstance(row.get(lang), str) and row[lang]}
    _cache.update(stamp=stamp, data=data)
    return data


def write(entries: dict, path=None) -> int:
    """Записати комплект: {напис: {мова: переклад}} → файл за абеткою (наявні рядки файлу лишаються, нові
    доповнюють / замінюють). → скільки написів у файлі."""
    from core.ui_text.catalog import TARGETS
    path = Path(path or PATH)
    data = {k: dict(v) for k, v in load(path).items()}
    for src, langs in entries.items():
        data.setdefault(src, {}).update({l: t for l, t in langs.items() if l in TARGETS and t})
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for src in sorted(data):
            fh.write(json.dumps({"uk": src, **{l: data[src][l] for l in TARGETS if data[src].get(l)}},
                                ensure_ascii=False) + "\n")
    _cache["stamp"] = None
    return len(data)


def apply(path=None) -> int:
    """Заповнити переклади з комплекту там, де їх немає. → скільки перекладів додано (усі мови разом)."""
    from core.models import UIText
    from core.ui_text import catalog
    from core.ui_text.translate import valid
    data = load(path)
    if not data:
        return 0
    now = timezone.now()
    at = now.isoformat(timespec="seconds")
    changed, added = [], 0
    for row in UIText.objects.filter(active=True).only("pk", "source", "translations").iterator(chunk_size=2000):
        langs = data.get(row.source)
        if not langs:
            continue
        tr = dict(row.translations or {})
        touched = False
        for lang, text in langs.items():
            if (tr.get(lang) or {}).get("text") or not valid(row.source, text):
                continue
            tr[lang] = {"text": text[:2000], "by": "ai", "model": MODEL, "at": at}
            touched = True
            added += 1
        if touched:
            row.translations = tr
            changed.append(row)
    if changed:
        UIText.objects.bulk_update(changed, ["translations"], batch_size=500)
        UIText.objects.filter(pk__in=[r.pk for r in changed]).update(updated_at=now)
        catalog.bump()
    return added


def missing(found=None, path=None) -> list:
    """Написи коду (extract.collect), яких у комплекті немає чи бракує мови — що перекласти до наступного оновлення."""
    from django.conf import settings

    from core.ui_text.catalog import TARGETS
    from core.ui_text.extract import collect
    found = found if found is not None else collect(settings.BASE_DIR)
    data = load(path)
    return sorted(src for src in found if any(not (data.get(src) or {}).get(lang) for lang in TARGETS))
