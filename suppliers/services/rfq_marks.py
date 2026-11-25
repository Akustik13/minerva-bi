"""🏷 Позначки рядків запиту постачальникам (RFQ): колір клітинки «№», наліпки, свій короткий текст — правий клік на
рядку таблиці пропозицій (request_work.html, _request_talk.html). Спільні для всіх, хто бачить запит; хто й коли
поставив — у підказці. Ставити — право change запиту (suppliers/admin.py, дія mark).

  COLORS / STICKERS — довідники (ключ, значок, назва[, колір]);
  view(label)       — для шаблону й JS: {color, hex, stickers: [{key, icon, name}], text, by, at};
  apply(req, rids, user, op) — op: {color: ключ | ""} · {sticker: ключ, on: bool} · {text: …} · {clear: true}
                      → {rid: view}; кілька рядків разом — одна дія для всіх вибраних. «Зняти все» місця не знімає;
  set_rank(req, rid, place, user) — 📌 закріпити рядок на місці 1, 2, 3… (0 — відкріпити): закріплені йдуть
                      поспіль 1…N, решта зсуваються; → {rid: view} усіх рядків, чиє місце змінилось.
"""
from django.db import transaction
from django.utils import timezone

COLORS = [("red", "🔴", "Червона", "#e53935"), ("orange", "🟠", "Помаранчева", "#fb8c00"),
          ("yellow", "🟡", "Жовта", "#fdd835"), ("green", "🟢", "Зелена", "#43a047"),
          ("blue", "🔵", "Синя", "#1e88e5"), ("purple", "🟣", "Фіолетова", "#8e24aa")]
STICKERS = [("star", "⭐", "Важливо"), ("like", "👍", "Подобається"), ("ask", "❓", "Уточнити"),
            ("wait", "⏳", "Чекаємо"), ("talk", "💬", "Обговорити"), ("sample", "📦", "Зразки"),
            ("no", "🚫", "Не підходить")]
TEXT_MAX = 60
RANK_MAX = 99
_COLOR = {k: (i, n, h) for k, i, n, h in COLORS}
_STICKER = {k: (i, n) for k, i, n in STICKERS}


def _rank(label) -> int:
    try:
        n = int((label or {}).get("rank") or 0) if isinstance(label, dict) else 0
    except (TypeError, ValueError):
        return 0
    return n if 0 < n <= RANK_MAX else 0


def view(label) -> dict:
    lab = label if isinstance(label, dict) else {}
    color = lab.get("color") if lab.get("color") in _COLOR else ""
    return {"color": color, "rank": _rank(lab), "hex": _COLOR[color][2] if color else "",
            "color_name": _COLOR[color][1] if color else "",
            "stickers": [{"key": k, "icon": _STICKER[k][0], "name": _STICKER[k][1]}
                         for k in (lab.get("stickers") or []) if k in _STICKER],
            "text": str(lab.get("text") or "")[:TEXT_MAX], "by": str(lab.get("by") or ""), "at": str(lab.get("at") or "")}


def _empty(lab) -> bool:
    return not (lab.get("color") or lab.get("stickers") or lab.get("text") or _rank(lab))


def apply(req, rids, user, op) -> dict:
    from suppliers.models import SupplierRequestRecipient as R
    ids = {int(x) for x in rids if str(x).isdigit()}
    who = (user.get_full_name() or user.username) if user is not None else ""
    out = {}
    with transaction.atomic():               # два швидкі кліки одночасно — другий не затирає перший
        for r in R.objects.select_for_update().filter(request=req, pk__in=ids).order_by("pk"):
            out[r.pk] = _apply_one(r, op, who)
    return out


def _apply_one(r, op, who) -> dict:
    from suppliers.models import SupplierRequestRecipient as R
    lab = dict(r.label or {})
    if op.get("clear"):                              # 🧹 колір, наліпки, текст; 📌 місце лишається
        lab = {"rank": _rank(lab)} if _rank(lab) else {}
    if "color" in op:
        lab["color"] = op["color"] if op["color"] in _COLOR else ""
    if op.get("sticker") in _STICKER:
        st = [k for k in (lab.get("stickers") or []) if k in _STICKER and k != op["sticker"]]
        if op.get("on"):
            st.append(op["sticker"])
        lab["stickers"] = [k for k, _i, _n in STICKERS if k in st]       # порядок — як у довіднику
    if "text" in op:
        lab["text"] = " ".join(str(op["text"] or "").split())[:TEXT_MAX]
    if _empty(lab):
        lab = {}
    else:
        lab.update(by=who, at=timezone.localtime().strftime("%d.%m.%Y %H:%M"))
    R.objects.filter(pk=r.pk).update(label=lab)
    return view(lab)


def set_rank(req, rid, place, user) -> dict:
    """📌 Рядок rid — на місце place (1…) серед закріплених; 0 — відкріпити. Закріплені — поспіль 1…N."""
    from suppliers.models import SupplierRequestRecipient as R
    try:
        place = max(0, min(int(place or 0), RANK_MAX))
    except (TypeError, ValueError):
        place = 0
    who = (user.get_full_name() or user.username) if user is not None else ""
    out = {}
    with transaction.atomic():
        rows = list(R.objects.select_for_update().filter(request=req).order_by("pk"))
        target = next((r for r in rows if r.pk == int(rid)), None) if str(rid).isdigit() else None
        if target is None:
            return {}
        pinned = sorted((r for r in rows if _rank(r.label) and r.pk != target.pk), key=lambda r: (_rank(r.label), r.pk))
        if place:
            pinned.insert(min(place, len(pinned) + 1) - 1, target)
        want = {r.pk: i for i, r in enumerate(pinned, 1)}
        for r in rows:
            new = want.get(r.pk, 0)
            if new == _rank(r.label) and r.pk != target.pk:
                continue
            lab = dict(r.label or {})
            lab.pop("rank", None)
            if new:
                lab["rank"] = new
            if r.pk == target.pk:
                lab.update(by=who, at=timezone.localtime().strftime("%d.%m.%Y %H:%M"))
            if _empty(lab):
                lab = {}
            R.objects.filter(pk=r.pk).update(label=lab)
            out[r.pk] = view(lab)
    return out
