"""
bots/services/dk_publish.py — 🧭 Дорожня карта публікації товару на DigiKey Marketplace.

Кроки (як у DigiKey Marketplace2 API — Products v1 «stage/upsert» → PIM → Offers v1 «offers»):
  card        📝 Картка лістингу в Minerva (джерело: копія з DigiKey / зі складу / з таблиці)
  checked     ✅ Заповнено все обов'язкове (problems() порожній — ті самі ліміти, що в API DigiKey)
  sent        📤 Товар надіслано (POST /products/stage/upsert прийнято)
  approved    🔎 DigiKey затвердив товар (є в GET /products нашого постачальника — справжній productId)
  offer       💲 Офер створено (POST /offers)
  live        🟢 На DigiKey (офер isActive і offerStatusReason = Active)

Коли крок уперше пройдено — DigiKeyListing.pub_events {крок: ISO-час}; повторне проходження не перезаписує.
Стан офера DigiKey (offerStatusReason) — pub_events["offer_status"]: «QuantityAvailableLessThanMOQ» тощо
показуються на дорожній карті як причина, чому офер не видно.
"""
from django.utils import timezone

STAGES = [
    ("card", "📝", "Картка"),
    ("checked", "✅", "Заповнено"),
    ("sent", "📤", "Надіслано"),
    ("approved", "🔎", "Затверджено"),
    ("offer", "💲", "Офер"),
    ("live", "🟢", "На DigiKey"),
]
STAGE_KEYS = [k for k, _i, _l in STAGES]

SOURCES = {"digikey": "копія з DigiKey", "stock": "зі складу Minerva", "copy": "копія іншого лістингу",
           "file": "з таблиці"}

# offerStatusReason (Offers v1) → людською мовою
OFFER_REASONS = {
    "Active": "активний",
    "Inactive": "вимкнено",
    "QuantityAvailableLessThanMOQ": "залишок менший за MOQ — DigiKey не показує офер",
    "SupplierInactive": "постачальник неактивний",
    "SupplierSuspended": "постачальника призупинено",
    "SupplierSuspendedBlocked": "постачальника заблоковано",
    "NoActiveShippingMethods": "немає активних способів доставки",
    "ProductInactivated": "товар деактивовано в DigiKey",
    "ProductNotWebDisplayed": "товар ще не показується на сайті DigiKey",
}

# Ліміти зі специфікації (UpsertProductStaged, NewOffer / UpdateOffer)
LIMITS = {"partNumber": 48, "manufacturer": 50, "description": 2048, "categoryId": 256,
          "title": 50, "supplierSku": 50}


def problems(listing, config=None) -> list:
    """Що не дасть опублікувати (порожньо — можна надсилати). Ті самі правила, що в API DigiKey."""
    from bots.models import DigiKeyConfig
    config = config or DigiKeyConfig.get()
    out = []
    sku = (listing.get_supplier_sku() or "").strip()
    title = (listing.dk_title or "").strip()
    desc = (listing.dk_description or "").strip()
    if not getattr(config, "marketplace_supplier_id", ""):
        out.append("Не вказано Supplier UUID у налаштуваннях DigiKey («🪪 Отримати Supplier UUID»)")
    if not (listing.dk_category_id or "").strip():
        out.append("Категорія DigiKey (DK Category ID) не заповнена")
    elif len(listing.dk_category_id) > LIMITS["categoryId"]:
        out.append(f"DK Category ID довший за {LIMITS['categoryId']} символів")
    if not sku:
        out.append("Немає артикула (Supplier SKU)")
    elif len(sku) > LIMITS["partNumber"]:
        out.append(f"Артикул «{sku}» довший за {LIMITS['partNumber']} символів (Part Number DigiKey)")
    if not title:
        out.append("Назва (DK Title) не заповнена")
    elif len(title) > LIMITS["title"]:
        out.append(f"Назва довша за {LIMITS['title']} символів ({len(title)})")
    if not desc:
        out.append("Опис (DK Description) не заповнений")
    elif len(desc) > LIMITS["description"]:
        out.append(f"Опис довший за {LIMITS['description']} символів")
    if len(listing.dk_manufacturer or "") > LIMITS["manufacturer"]:
        out.append(f"Виробник довший за {LIMITS['manufacturer']} символів")
    prices = listing.get_prices_api() if hasattr(listing, "get_prices_api") else []
    if not prices:
        out.append("Немає цін (цінові тири порожні)")
    elif any((p.get("quantityBreak") or 0) < 1 or (p.get("price") or 0) <= 0 for p in prices):
        out.append("У цінових тирах є кількість < 1 або ціна ≤ 0")
    if (listing.dk_min_order_qty or 0) < 1:
        out.append("MOQ має бути щонайменше 1")
    return out


def _now():
    return timezone.now()


def mark(listing, stage, when=None, save=True, **info) -> bool:
    """Крок уперше пройдено — записати час (повтор не перезаписує). info — додаткові ключі (offer_status…).
    Повертає True, якщо щось змінилось."""
    events = dict(listing.pub_events or {})
    changed = False
    if stage and stage not in events:
        events[stage] = (when or _now()).isoformat()
        changed = True
    for key, value in info.items():
        if events.get(key) != value:
            events[key] = value
            changed = True
    if changed:
        listing.pub_events = events
        if save and listing.pk:
            type(listing).objects.filter(pk=listing.pk).update(pub_events=events)
    return changed


def note_offer(listing, offer: dict, save=True) -> bool:
    """Офер з GET /offers / відповіді PUT/POST → крок «офер» і «на DigiKey» (якщо активний)."""
    if not offer:
        return False
    reason = offer.get("offerStatusReason") or ""
    created = _parse(offer.get("createDateUtc"))
    changed = mark(listing, "offer", when=created, save=False, offer_status=reason)
    if offer.get("isActive") and reason == "Active":
        changed = mark(listing, "live", save=False) or changed
    if changed and save and listing.pk:
        type(listing).objects.filter(pk=listing.pk).update(pub_events=listing.pub_events)
    return changed


def _parse(value):
    from django.utils.dateparse import parse_datetime
    if not value:
        return None
    try:
        dt = parse_datetime(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt is not None and timezone.is_naive(dt):
        from datetime import timezone as dt_tz
        dt = dt.replace(tzinfo=dt_tz.utc)
    return dt


def _fmt_span(seconds) -> str:
    if seconds is None:
        return ""
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} хв"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} год"
    return f"{hours // 24} дн"


def roadmap(listing, config=None) -> dict:
    """{steps: [{key, icon, label, state: done|current|todo|error|warn, at, span, hint}], total, source, next}."""
    events = dict(listing.pub_events or {})
    created = _parse(events.get("card")) or listing.created_at
    events.setdefault("card", created.isoformat() if created else None)
    todo = problems(listing, config)
    error = (listing.last_error or "").strip() if listing.sync_status == "error" else ""
    reason = events.get("offer_status") or ""
    steps, prev, current = [], None, None
    for key, icon, label in STAGES:
        at = _parse(events.get(key))
        done = at is not None or (key == "checked" and not todo)   # «заповнено» — і без дати, якщо все є
        state = "done" if done else "todo"
        hint = ""
        if key == "checked" and todo:
            hint = "; ".join(todo[:3]) + (f" … ще {len(todo) - 3}" if len(todo) > 3 else "")
        if not done and current is None:
            current = key
            state = "error" if error else "current"
            hint = hint or error[:200]
        if key == "live" and current == "live" and reason and reason != "Active":
            state = "warn"
            hint = OFFER_REASONS.get(reason, reason)
        span = (at - prev).total_seconds() if at and prev else None
        steps.append({"key": key, "icon": icon, "label": label, "state": state,
                      "at": timezone.localtime(at).strftime("%d.%m.%y %H:%M") if at else "",
                      "span": _fmt_span(span) if span is not None and key != "card" else "", "hint": hint})
        if at:
            prev = at
    live_at = _parse(events.get("live"))
    total = _fmt_span((live_at - created).total_seconds()) if live_at and created else ""
    last_done = max((i for i, s in enumerate(steps) if s["state"] == "done"), default=0)
    return {"steps": steps, "current": current, "total": total,
            "current_step": next((s for s in steps if s["key"] == current), None),
            "next": NEXT_ACTION.get(current, ""), "fill": round(last_done / (len(steps) - 1), 3),
            "stage": steps[last_done]["key"],
            "source": SOURCES.get(listing.card_source, ""), "problems": todo,
            "offer_status": OFFER_REASONS.get(reason, reason) if reason else "",
            "done": sum(1 for s in steps if s["state"] == "done"), "count": len(steps)}


NEXT_ACTION = {
    "checked": "Заповніть поля нижче (категорія, назва ≤ 50, опис, ціни) і збережіть",
    "sent": "«🚀 Опублікувати» — надіслати товар у DigiKey",
    "approved": "DigiKey перевіряє товар (зазвичай 1–3 робочі дні); Minerva перевіряє сама — «🔄 Перевірити статус»",
    "offer": "«📦 Створити Offer» — товар затверджено, можна виставляти",
    "live": "Офер створено; чекаємо, поки DigiKey покаже його (залишок ≥ MOQ, доставка налаштована)",
}


def stage_of(events) -> str:
    """Останній пройдений крок за записаними подіями (для фільтра й зведення)."""
    events = events or {}
    return next((k for k in reversed(STAGE_KEYS) if events.get(k)), "card")


def summary(qs) -> list:
    """Скільки лістингів зараз на кожному кроці (для панелі над списком)."""
    counts = {k: 0 for k in STAGE_KEYS}
    for events in qs.values_list("pub_events", flat=True):
        counts[stage_of(events)] += 1
    return [{"key": k, "icon": i, "label": l, "count": counts[k]} for k, i, l in STAGES]
