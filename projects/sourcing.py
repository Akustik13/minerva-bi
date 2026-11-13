"""📨 Запити постачальникам у проєкті: кому робили запит (RFQ, застосунок suppliers), хто відповів і з якою ціною,
кого не взяли й чому, кого обрали — на яку партію чого.

Дані — лише suppliers.SupplierRequest (project) і SupplierRequestRecipient (стан, ціна, строк, chosen, award,
reject_reason); тут — читання для вкладки «🔗 Сторони», діаграми сторін і карти зв'язків портфеля, і рішення з пульта
(`decide` → suppliers.services.rfq.award / reject / undecide). Нового сховища немає.
"""
from django.urls import reverse

# Підсумок для людини й діаграм: chosen — обрано, quoted — є відповідь / пропозиція, asked — чекаємо,
# rejected — не взяли (ми), declined — відмовився (вони), silent — не відповів, planned — ще не надіслано
OUTCOME_TEXT = {"chosen": "✅ обрано", "quoted": "💶 є пропозиція", "replied": "📥 відповів", "asked": "📤 чекаємо",
                "rejected": "✖ не взяли", "declined": "🙅 відмовився", "silent": "🔕 не відповів",
                "planned": "⏳ ще не надіслано"}
CLS = {"chosen": "ok", "quoted": "warn", "replied": "warn", "asked": "open", "planned": "open",
       "rejected": "off", "declined": "off", "silent": "off"}
OFF = ("rejected", "declined", "silent")


class SourcingError(ValueError):
    pass


def available() -> bool:
    try:
        from django.apps import apps
        return apps.is_installed("suppliers")
    except Exception:  # noqa: BLE001
        return False


def outcome(r) -> str:
    if r.chosen:
        return "chosen"
    if r.reject_reason:
        return "rejected"
    return {"declined": "declined", "silent": "silent", "quoted": "quoted", "replied": "replied", "sent": "asked",
            "planned": "planned", "failed": "planned"}.get(r.state, "asked")


def _price(r) -> str:
    from suppliers.services.rfq import price_text
    parts = []
    if r.price is not None:
        parts.append(f"{price_text(r.price)} {r.currency}".strip() + " / шт")
    elif r.total is not None:
        parts.append(f"{r.total} {r.currency}".strip())
    if r.lead_days:
        parts.append(f"{r.lead_days} дн.")
    return " · ".join(parts)


def requests(project):
    if not available() or project is None or not project.pk:
        return []
    from suppliers.models import SupplierRequest
    return list(SupplierRequest.objects.filter(project=project).exclude(status=SupplierRequest.CANCELLED)
                .prefetch_related("recipients__supplier").order_by("-created_at", "-pk"))


def requests_for(project_ids):
    """Запити кількох проєктів (карта зв'язків портфеля)."""
    from suppliers.models import SupplierRequest
    return list(SupplierRequest.objects.filter(project_id__in=project_ids).exclude(status=SupplierRequest.CANCELLED)
                .prefetch_related("recipients__supplier"))


def qty_text(req) -> str:
    """«5» → «5 шт», «10; 100» → «10 / 100 шт»; інше — як написано."""
    parts = [p.strip() for p in str(req.quantities or "").split(";") if p.strip()]
    if parts and all(p.replace(" ", "").isdigit() for p in parts):
        return " / ".join(parts) + " шт"
    return str(req.quantities or "").strip()


def what(req) -> str:
    q = qty_text(req)
    return req.title + (f" · {q}" if q else "")


def short(r, req) -> str:
    """Підпис під постачальником на карті: «✅ партія 1 · 500 шт», «📤 Прототип LTE · 5 шт · чекаємо»."""
    o = outcome(r)
    if o == "chosen":
        return ("✅ " + (r.award or req.title))[:60]
    icon, text = OUTCOME_TEXT[o].split(" ", 1)
    return f"{icon} {what(req)} · {text}"


def line(r, req) -> str:
    """Рядок для вузла діаграми й підказки: «✅ PCBA · партія 1», «💶 RFQ-…: 12,40 USD / шт», «✖ не взяли: ціна»."""
    o = outcome(r)
    if o == "chosen":
        return f"✅ {req.title}" + (f" · {r.award}" if r.award else "")
    if o == "rejected":
        return f"✖ {req.title}: не взяли" + (f" — {r.reject_reason}" if r.reject_reason not in ("", "—") else "")
    if o in ("quoted", "replied"):
        price = _price(r)
        return f"💶 {req.code} «{what(req)}»: {price}" if price else f"📥 {req.code} «{what(req)}»: відповів"
    return f"{OUTCOME_TEXT[o].split(' ', 1)[0]} {req.code} «{what(req)}»: {OUTCOME_TEXT[o].split(' ', 1)[1]}"


def context(project, user) -> dict:
    """Для вкладки «🔗 Сторони»: запити проєкту з отримувачами, права, адреси."""
    if not available():
        return {"on": False}
    rows = []
    for req in requests(project):
        recs = []
        for r in req.recipients.all():
            o = outcome(r)
            recs.append({"id": r.pk, "supplier": r.supplier.name, "supplier_url":
                         reverse("admin:inventory_supplier_change", args=[r.supplier_id]),
                         "outcome": o, "label": OUTCOME_TEXT[o], "cls": CLS[o], "price": _price(r),
                         "award": r.award, "reason": "" if r.reject_reason == "—" else r.reject_reason,
                         "can_decide": r.state not in ("planned", "failed") or r.chosen or bool(r.reject_reason)})
        order = {"chosen": 0, "quoted": 1, "replied": 1, "asked": 2, "planned": 3, "rejected": 4, "declined": 5,
                 "silent": 6}
        recs.sort(key=lambda x: order.get(x["outcome"], 9))
        rows.append({"id": req.pk, "code": req.code, "title": req.title, "qty": req.quantities,
                     "status": req.get_status_display(), "url": reverse("admin:suppliers_supplierrequest_work",
                                                                       args=[req.pk]),
                     "recipients": recs, "chosen": sum(1 for x in recs if x["outcome"] == "chosen")})
    can = bool(user and user.has_perm("suppliers.change_supplierrequest"))
    return {"on": True, "rows": rows, "can_decide": can,
            "can_add": bool(user and user.has_perm("suppliers.add_supplierrequest")),
            "new_url": reverse("admin:suppliers_supplierrequest_new") + f"?project={project.pk}"}


def by_supplier(project) -> dict:
    """{pk постачальника: {"name", "lines": [(outcome, текст)], "best": outcome}} — для діаграми сторін."""
    out = {}
    for req in requests(project):
        for r in req.recipients.all():
            o = outcome(r)
            it = out.setdefault(r.supplier_id, {"name": r.supplier.name, "lines": [], "outcomes": []})
            it["lines"].append((o, line(r, req)))
            it["outcomes"].append(o)
    for it in out.values():
        oc = it["outcomes"]
        it["best"] = next((o for o in ("chosen", "quoted", "replied", "asked", "planned") if o in oc),
                          oc[0] if oc else "asked")
    return out


def decide(project, recipient_id, action, text="", user=None) -> str:
    """✅ Доручити (award) / ✖ Не брати (reject) / ↺ Ще не вирішено (reset) — лише отримувачі запитів цього проєкту."""
    if not available():
        raise SourcingError("Модуль запитів постачальникам не встановлено")
    from suppliers.models import SupplierRequestRecipient
    from suppliers.services import rfq
    r = (SupplierRequestRecipient.objects.select_related("request", "supplier")
         .filter(pk=recipient_id, request__project=project).first())
    if r is None:
        raise SourcingError("Цього постачальника немає серед запитів проєкту")
    if action == "award":
        rfq.award(r, text, user)
        return f"✅ {r.supplier.name}: {r.request.title}" + (f" · {' '.join(str(text).split())[:200]}" if text else "")
    if action == "reject":
        rfq.reject(r, text, user)
        return f"✖ Не беремо {r.supplier.name}" + (f": {' '.join(str(text).split())[:255]}" if text else "")
    if action == "reset":
        rfq.undecide(r)
        return f"↺ {r.supplier.name}: ще не вирішено"
    raise SourcingError("Невідома дія")


def facts_text(project) -> str:
    """Для AI (ризики, план): запити й рішення — без цін, якщо їх немає."""
    out = []
    for req in requests(project):
        out.append(f"- {req.code} «{req.title}»" + (f" ({req.quantities})" if req.quantities else "") + ": "
                   + "; ".join(f"{r.supplier.name} — {line(r, req)}" for r in req.recipients.all()))
    return "ЗАПИТИ ПОСТАЧАЛЬНИКАМ (RFQ):\n" + "\n".join(out) if out else ""
