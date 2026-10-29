"""Ліва колонка скриньки: ⭐ обране зверху й порядок решти — для кожного користувача окремо.

EmailSettings.nav_pinned — ключі обраного в порядку закріплення; nav_sort — як впорядкувати решту
(за вживанням / кількістю листів / назвою / як у налаштуваннях категорій); nav_usage — {ключ: [бал, дата]}:
кожен клік у колонці +1, бал «старіє» вдвічі за HALF_LIFE_DAYS — рідко потрібне само опускається вниз.
Ключі: wc:<pk> — своя категорія, kind:<тип> — «Хто пише», nocat — без своєї категорії, quote — запити
пропозицій, sort — «Розібрати пошту».
"""
import re
from datetime import date

from django.utils import timezone

KEY_RE = re.compile(r"^(?:wc:\d{1,9}|kind:[a-z_]{1,30}|nocat|quote|sort)$")
PIN_MAX = 30
USAGE_MAX = 200          # ключів у nav_usage (найменш уживані відкидаються)
HALF_LIFE_DAYS = 30      # клік місяць тому важить половину


def valid(key) -> bool:
    return bool(KEY_RE.match(str(key or "")))


def _decayed(value, today) -> float:
    try:
        score, day = float(value[0]), date.fromisoformat(value[1])
    except (TypeError, ValueError, IndexError):
        return 0.0
    return score * 0.5 ** (max((today - day).days, 0) / HALF_LIFE_DAYS)


def scores(es) -> dict:
    """{ключ: бал вживання на сьогодні}."""
    today = timezone.localdate()
    return {k: _decayed(v, today) for k, v in (es.nav_usage or {}).items()}


def record_use(es, key) -> None:
    """Клік у лівій колонці: +1 до балу ключа (попередній бал — зі «старінням» до сьогодні)."""
    if not valid(key):
        return
    today = timezone.localdate()
    usage = dict(es.nav_usage or {})
    usage[key] = [round(_decayed(usage.get(key), today) + 1, 3), today.isoformat()]
    if len(usage) > USAGE_MAX:
        keep = sorted(usage, key=lambda k: _decayed(usage[k], today), reverse=True)[:USAGE_MAX]
        usage = {k: usage[k] for k in keep}
    es.nav_usage = usage
    es.save(update_fields=["nav_usage"])


def change(es, action, key) -> list:
    """⭐ pin / unpin / up / down → новий список обраного (збережено)."""
    pinned = [k for k in (es.nav_pinned or []) if valid(k)]
    if action == "pin" and key not in pinned and len(pinned) < PIN_MAX:
        pinned.append(key)
    elif action == "unpin":
        pinned = [k for k in pinned if k != key]
    elif action in ("up", "down") and key in pinned:
        i = pinned.index(key)
        j = i - 1 if action == "up" else i + 1
        if 0 <= j < len(pinned):
            pinned[i], pinned[j] = pinned[j], pinned[i]
    es.nav_pinned = pinned
    es.save(update_fields=["nav_pinned"])
    return pinned


def ordered(items, es, key_of, n_of=None, name_of=None) -> list:
    """Елементи в порядку колонки: ⭐ обране (як закріплено), далі за es.nav_sort; рівні — як були."""
    pos = {k: i for i, k in enumerate(es.nav_pinned or [])}
    mode = es.nav_sort
    sc = scores(es) if mode == "usage" else {}

    def rest(pair):
        i, item = pair
        if mode == "usage":
            return (-sc.get(key_of(item), 0.0), i)
        if mode == "count" and n_of is not None:
            return (-(n_of(item) or 0), i)
        if mode == "name" and name_of is not None:
            return (str(name_of(item)).casefold(), i)
        return (i,)
    indexed = list(enumerate(items))
    pins = sorted((p for p in indexed if key_of(p[1]) in pos), key=lambda p: pos[key_of(p[1])])
    others = sorted((p for p in indexed if key_of(p[1]) not in pos), key=rest)
    return [item for _i, item in pins + others]
