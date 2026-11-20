"""🧠 Витрати AI на запит постачальникам (RFQ): яка модель і скільки — для смуги «🧠 AI» над таблицею пропозицій.

  add(req, kind, model, usd)  — дописати виклик (атомарно, рядок запиту select_for_update): разом, по моделях, по діях
                                (KINDS: parse — розбір відповіді, compare — порада, brief — «Що відповісти?»,
                                letters — «Написати кожному»);
  summary(req, rows)          — для сторінки: разом $, по моделях і діях, хто якою моделлю розібраний (з рядків).
  label(model)                — «Sonnet 5.5» з ai.MODELS.
"""
KINDS = {"parse": "розбір відповідей", "compare": "порада", "brief": "«Що відповісти?»",
         "letters": "«Написати кожному»", "reply": "💬 відповідь з AI"}


def label(model) -> str:
    from suppliers.services.ai import MODELS
    return dict(MODELS).get(model, model or "").split(" — ")[0]


def add(req, kind, model, usd):
    from django.db import transaction
    from suppliers.models import SupplierRequest
    try:
        usd = float(usd or 0)
    except (TypeError, ValueError):
        usd = 0.0
    with transaction.atomic():
        row = SupplierRequest.objects.select_for_update().filter(pk=req.pk).first()
        if row is None:
            return
        u = dict(row.ai_usage or {})
        u["usd"] = round(float(u.get("usd") or 0) + usd, 6)
        u["calls"] = int(u.get("calls") or 0) + 1
        for key, name in (("models", model or "?"), ("kinds", kind)):
            part = dict(u.get(key) or {})
            cur = dict(part.get(name) or {})
            cur["calls"] = int(cur.get("calls") or 0) + 1
            cur["usd"] = round(float(cur.get("usd") or 0) + usd, 6)
            part[name] = cur
            u[key] = part
        SupplierRequest.objects.filter(pk=req.pk).update(ai_usage=u)
    req.ai_usage = u


def _usd(v) -> str:
    v = float(v or 0)
    return f"${v:.3f}" if v < 1 else f"${v:.2f}"


def summary(req, rows) -> dict:
    u = req.ai_usage or {}
    parsed = {}
    for row in rows:
        ai = row["recipient"].ai or {}
        if ai.get("summary") and ai.get("model"):
            parsed[label(ai["model"])] = parsed.get(label(ai["model"]), 0) + 1
    return {
        "usd": _usd(u.get("usd")), "calls": int(u.get("calls") or 0), "any": bool(u.get("calls")),
        "models": [{"name": label(m), "calls": v.get("calls", 0), "usd": _usd(v.get("usd"))}
                   for m, v in sorted((u.get("models") or {}).items(), key=lambda x: -float(x[1].get("usd") or 0))],
        "kinds": [{"name": KINDS.get(k, k), "calls": v.get("calls", 0), "usd": _usd(v.get("usd"))}
                  for k, v in (u.get("kinds") or {}).items()],
        "parsed": [{"name": n, "count": c} for n, c in sorted(parsed.items(), key=lambda x: -x[1])],
    }


def row_info(r) -> dict:
    """Під рядком: яка модель розібрала відповідь, скільки коштувало, коли."""
    import datetime
    ai = r.ai or {}
    if not ai.get("model"):
        return {}
    try:
        at = datetime.datetime.fromisoformat(str(ai.get("at") or ""))
    except ValueError:
        at = None
    return {"model": label(ai["model"]), "usd": _usd(ai.get("cost_usd")), "at": at}
