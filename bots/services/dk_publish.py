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
    "NotFound": "офера немає на DigiKey (видалено?) — «🔄 Етапи з DigiKey» ще раз або «📦 Створити Offer»",
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


# Звідки дата кроку (pub_events["src"]) — показується в панелі кроку
SRC_TEXT = {
    "minerva": "Minerva записала в момент події",
    "digikey": "дата створення офера на DigiKey (createDateUtc)",
    "digikey_clamp": "уточнено з DigiKey — не пізніше за створення офера",
    "digikey_live": "офер активний на DigiKey; коли саме DigiKey його показав, API не повідомляє — дата офера",
}
OLD_SRC = ("з даних Minerva; кроки, пройдені до 30.10.2026, — приблизно (дата створення або останньої "
           "синхронізації лістингу)")


def mark(listing, stage, when=None, save=True, how=None, **info) -> bool:
    """Крок уперше пройдено — записати час (повтор не перезаписує) і звідки він (how — ключ SRC_TEXT,
    за замовчуванням «minerva»). info — додаткові ключі (offer_status…). Повертає True, якщо щось змінилось."""
    events = dict(listing.pub_events or {})
    changed = False
    if stage and stage not in events:
        events[stage] = (when or _now()).isoformat()
        events["src"] = {**(events.get("src") or {}), stage: how or "minerva"}
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
    changed = mark(listing, "offer", when=created, save=False, how="digikey" if created else None,
                   offer_status=reason)
    if offer.get("isActive") and reason == "Active":
        changed = mark(listing, "live", save=False) or changed
    if changed and save and listing.pk:
        type(listing).objects.filter(pk=listing.pk).update(pub_events=listing.pub_events)
    return changed


BEFORE_OFFER = ("card", "checked", "sent", "approved")


def sync_offer(listing, offer: dict, save=True) -> bool:
    """Етапи вже виставленого лістингу за офером DigiKey (GET /offers): «офер» — справжня дата створення
    (createDateUtc), кроки до нього не пізніше за неї (картку, скопійовану з DigiKey, могли створити вже після
    офера; міграція bots 0038 ставила дату останньої синхронізації), «на DigiKey» — якщо офер активний."""
    if not offer:
        return False
    events = dict(listing.pub_events or {})
    before = dict(events)
    src = dict(events.get("src") or {})
    created = _parse(offer.get("createDateUtc"))
    if created:
        events["offer"], src["offer"] = created.isoformat(), "digikey"
        for key in BEFORE_OFFER:
            at = _parse(events.get(key))
            if at is None and key != "checked" or at is not None and at > created:
                events[key], src[key] = created.isoformat(), "digikey_clamp"
    reason = offer.get("offerStatusReason") or ""
    events["offer_status"] = reason
    if offer.get("isActive") and reason == "Active" and not events.get("live"):
        events["live"], src["live"] = (created or _now()).isoformat(), "digikey_live"
    if offer.get("id") and offer.get("id") != listing.dk_offer_id:
        events["found_offer"] = offer["id"]          # офер є на DigiKey, а в лістингу не записаний
    else:
        events.pop("found_offer", None)
    events["src"] = src
    events["dk_dates"] = True
    if events == before:
        return False
    listing.pub_events = events
    if save and listing.pk:
        type(listing).objects.filter(pk=listing.pk).update(pub_events=events)
    return True


def sync_stages(listings=None, offers=None) -> dict:
    """«🔄 Етапи з DigiKey»: один GET /offers на всіх, кожен лістинг — sync_offer; лістинг з ID офера, якого
    на DigiKey вже немає, — причина «NotFound». {total, updated, not_found: [sku], without_offer}."""
    from bots.models import DigiKeyConfig, DigiKeyListing
    if offers is None:
        from bots.services.dk_marketplace import fetch_offers
        offers = fetch_offers(DigiKeyConfig.get(), max_count=5000)
    by_id = {o.get("id"): o for o in offers if o.get("id")}
    by_sku = {(o.get("supplierSku") or "").strip(): o for o in offers if o.get("supplierSku")}
    qs = listings if listings is not None else DigiKeyListing.objects.select_related("product")
    out = {"total": 0, "updated": 0, "not_found": [], "without_offer": 0}
    for listing in qs:
        out["total"] += 1
        offer = by_id.get(listing.dk_offer_id) or by_sku.get(listing.get_supplier_sku())
        if offer:
            out["updated"] += sync_offer(listing, offer)
        elif listing.dk_offer_id:
            out["not_found"].append(listing.get_supplier_sku())
            out["updated"] += mark(listing, None, offer_status="NotFound")
        else:
            out["without_offer"] += 1
    return out


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


def roadmap(listing, config=None, detail=False) -> dict:
    """{steps: [{key, icon, label, state: done|current|todo|error|warn, at, span, hint}], total, source, next}.
    detail — ще й панель кожного кроку (step_details: звідки дата, що зроблено, ID, кнопки дій)."""
    events = dict(listing.pub_events or {})
    created = _parse(events.get("card")) or listing.created_at
    events.setdefault("card", created.isoformat() if created else None)
    todo = problems(listing, config)
    error = _error(listing)
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
    if detail:
        step_details(listing, steps, events, todo, error, current)
    return {"steps": steps, "current": current, "total": total,
            "current_step": next((s for s in steps if s["key"] == current), None),
            "next": NEXT_ACTION.get(current, ""), "fill": round(last_done / (len(steps) - 1), 3),
            "stage": steps[last_done]["key"],
            "source": SOURCES.get(listing.card_source, ""), "problems": todo,
            "offer_status": OFFER_REASONS.get(reason, reason) if reason else "",
            "dk_dates": bool(events.get("dk_dates")),
            "done": sum(1 for s in steps if s["state"] == "done"), "count": len(steps)}


def _error(listing) -> str:
    """Остання відмова DigiKey, поки товар не опубліковано (авто-перевірка staged → офер пише її в last_error,
    а стан лишає «⏳ Очікує затвердження»)."""
    text = (listing.last_error or "").strip()
    return text if text and listing.sync_status != "published" else ""


# Що означає крок — перше речення панелі
MEANING = {
    "card": "Лістинг у Minerva — усе, що піде на DigiKey: товар, категорія, назва, опис, ціни, параметри.",
    "checked": "Перевірка Minerva тими самими правилами, що й DigiKey: категорія, назва ≤ 50, опис ≤ 2048, "
               "виробник ≤ 50, артикул ≤ 48, ціни, MOQ, Supplier UUID.",
    "sent": "Товар надіслано в DigiKey на перевірку (Products API: stage/upsert). DigiKey відповідає ID чернетки, "
            "сам товар з'являється після затвердження.",
    "approved": "DigiKey перевірив і прийняв товар — він є серед товарів вашого постачальника (Products API). "
                "Зазвичай 1–3 робочі дні.",
    "offer": "Пропозиція з цінами, залишком, MOQ і строком (Offers API) на затверджений товар.",
    "live": "DigiKey показує офер покупцям (офер активний, причина «Active»).",
}
# Причина, чому офер не показується → що зробити
REASON_ACTIONS = {
    "QuantityAvailableLessThanMOQ": "Залишок на DigiKey менший за MOQ — оновіть залишок або зменште MOQ.",
    "Inactive": "Офер вимкнено — увімкніть його на supplier.digikey.com (або «Активне на DigiKey» тут і "
                "оновіть офер).",
    "NoActiveShippingMethods": "На supplier.digikey.com не налаштовано жодного способу доставки.",
    "ProductNotWebDisplayed": "DigiKey ще не показує товар на сайті — зазвичай минає саме за кілька днів.",
    "ProductInactivated": "DigiKey деактивував товар — питання до підтримки DigiKey Marketplace.",
    "SupplierInactive": "Акаунт постачальника неактивний — supplier.digikey.com.",
    "SupplierSuspended": "Акаунт постачальника призупинено — supplier.digikey.com.",
    "SupplierSuspendedBlocked": "Акаунт постачальника заблоковано — supplier.digikey.com.",
    "NotFound": "Офера з цим ID на DigiKey немає — звірте офери або створіть новий.",
}
SUPPLIER_PORTAL = "https://supplier.digikey.com/"
# Що не заповнено → поле картки (id форми адмінки) або сторінка налаштувань
PROBLEM_FIELDS = [("Supplier UUID", "config"), ("Категорія", "dk_category_id"), ("DK Category", "dk_category_id"),
                  ("Артикул", "dk_supplier_sku"), ("артикула", "dk_supplier_sku"), ("Назва", "dk_title"),
                  ("Опис", "dk_description"), ("Виробник", "dk_manufacturer"), ("цін", "dk_prices"),
                  ("MOQ", "dk_min_order_qty")]


def _fmt(at) -> str:
    return timezone.localtime(at).strftime("%d.%m.%Y %H:%M") if at else ""


def step_details(listing, steps, events, todo, error, current):
    """Панель кожного кроку: how (звідки дата), facts [(назва, значення)], text, error, actions
    [{label, url, post, confirm, external, anchor}] — дії лише для кроку, де вони зараз потрібні."""
    from django.urls import reverse
    src = events.get("src") or {}
    pk = listing.pk
    url = lambda name: reverse(f"admin:bots_digikeylisting_{name}", args=[pk])      # noqa: E731
    sync = {"label": "🔄 Етапи з DigiKey", "url": reverse("admin:bots_digikeylisting_pub_sync") + f"?pk={pk}",
            "post": True, "confirm": "Прочитати офери з DigiKey й оновити етапи? На DigiKey нічого не змінюється."}
    reason = events.get("offer_status") or ""
    for s in steps:
        key, actions, facts = s["key"], [], []
        if s["at"]:
            s["how"] = SRC_TEXT.get(src.get(key), OLD_SRC) if key in src or key != "card" else (
                "дата створення лістингу в Minerva")
        s["meaning"] = MEANING[key]
        if key == "card":
            facts.append(("Звідки картка", SOURCES.get(listing.card_source, "не відомо (створено до 30.10.2026)")))
            if listing.product_id:
                actions.append({"label": f"📦 Товар {listing.product.sku}", "url": reverse(
                    "admin:inventory_product_change", args=[listing.product_id])})
        elif key == "checked":
            if todo:
                for problem in todo:
                    field = next((f for word, f in PROBLEM_FIELDS if word in problem), "")
                    if field == "config":
                        actions.append({"label": f"⚙️ {problem}",
                                        "url": reverse("admin:bots_digikeyconfig_changelist")})
                    else:
                        actions.append({"label": f"✏️ {problem}", "anchor": f"id_{field}" if field else ""})
            else:
                facts.append(("Категорія DigiKey", f"{listing.dk_category_name or ''} ({listing.dk_category_id})"
                              .strip()))
                facts.append(("Ціни", f"{len(listing.dk_prices or [])} ступ., MOQ {listing.dk_min_order_qty}"))
        elif key == "sent":
            if events.get("staged_id"):
                facts.append(("ID чернетки DigiKey (staged)", events["staged_id"]))
            if s["state"] in ("current", "error"):
                actions.append({"label": "🚀 Опублікувати на DigiKey", "url": url("publish"), "post": True,
                                "confirm": "Надіслати товар на DigiKey на перевірку?"})
        elif key == "approved":
            if listing.dk_product_id:
                facts.append(("DK Product ID", listing.dk_product_id))
            if s["state"] in ("current", "error"):
                s["text"] = ("DigiKey ще перевіряє товар. Minerva перевіряє сама («🔍 Авто-перевірка статусу» на "
                             "сторінці DigiKey) або кнопкою нижче — затверджено, то одразу створить офер.")
                actions.append({"label": "🔄 Перевірити статус DigiKey", "url": url("check_staged"), "post": True})
        elif key == "offer":
            if listing.dk_offer_id:
                facts.append(("DK Offer ID", listing.dk_offer_id))
            if events.get("found_offer"):
                facts.append(("На DigiKey є офер з цим артикулом", events["found_offer"]))
                actions.append({"label": "🔗 Звірити офери й записати ID",
                                "url": reverse("admin:bots_digikeypricerun_offers") + f"?q={listing.get_supplier_sku()}"})
            if s["state"] in ("current", "error"):
                s["text"] = ("Товар затверджено — Minerva створить офер з цінами, залишком (зараз "
                             f"{listing.get_dk_quantity()} шт), MOQ {listing.dk_min_order_qty} і строком "
                             f"{listing.dk_lead_time_days} дн.")
                actions.append({"label": "📦 Створити Offer", "url": url("create_offer"), "post": True,
                                "confirm": "Створити офер на DigiKey з поточними цінами й залишком?"})
                actions.append(sync)
        elif key == "live":
            if reason:
                facts.append(("Стан офера на DigiKey", OFFER_REASONS.get(reason, reason)))
            if listing.dk_quantity_available is not None:
                facts.append(("Залишок на DigiKey", f"{listing.dk_quantity_available} шт (MOQ {listing.dk_min_order_qty})"))
            if s["state"] in ("warn", "current", "error") and events.get("offer"):
                s["text"] = REASON_ACTIONS.get(reason, "Чекаємо, поки DigiKey покаже офер.")
                if reason == "QuantityAvailableLessThanMOQ":
                    actions.append({"label": "📤 Оновити залишок на DigiKey", "url": url("sync_qty"), "post": True})
                    actions.append({"label": "✏️ MOQ", "anchor": "id_dk_min_order_qty"})
                elif reason == "NotFound":
                    actions.append({"label": "🔍 Звірити офери", "url": reverse("admin:bots_digikeypricerun_offers")
                                    + f"?q={listing.get_supplier_sku()}"})
                elif reason:
                    actions.append({"label": "supplier.digikey.com ↗", "url": SUPPLIER_PORTAL, "external": True})
                actions.append(sync)
        if s["state"] == "error" and key == current:
            s["error"] = error
        s["facts"], s["actions"] = facts, actions
        s["open"] = key == current


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
