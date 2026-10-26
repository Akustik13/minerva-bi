"""Підказки в пошуку сайту магазину — сторона Minerva (режим — Shop.search_assist).

Розумний пошук і підказки за правилами (частота, роз'єм і стандарт у запиті, схожі товари, підходящі кабелі,
наступник) рахує сам сайт (shop.js) з даних каталогу — миттєво й без Minerva. Minerva додає:
- журнал «що шукають і не знаходять» (ShopSearchQuery — лише текст запиту й лічильники, без персональних даних);
- «часто купують разом» — з історії замовлень;
- AI-підказки (режими «ai», «ai_rag»): модель вибирає з каталогу магазину до 3 товарів з коротким поясненням;
  кожен артикул перевіряється (є в магазині й видимий, не з поточних результатів), відповіді кешуються на добу,
  є денний ліміт і облік токенів (Shop.search_ai_usage);
- підказки нашого RAG (режими «rag», «ai_rag»): запит відвідувача → RAG API v1 (/v1/ask, той самий, що «Помічник
  по продукції») → товари з відповіді RAG, перевірені за асортиментом магазину, + коротке речення. RAG відповідає
  5–20 с, тому сайт опитує Minerva (rag=1) кілька разів; Minerva тримає одне завдання до RAG одночасно, кешує
  відповідь на добу (спільно для всіх магазинів — RAG знає всі товари), лічильники rag_* у Shop.search_ai_usage.
Будь-який збій — порожня відповідь, і сайт шукає як звичайно.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import timedelta

from core.shared_cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from .models import Shop, ShopListing, ShopSearchQuery

logger = logging.getLogger("shop")

MAX_QUERY = 100
MAX_ROWS = 5000              # запитів на магазин у журналі; найдавніші прибираються
AI_TIPS = 3
AI_CACHE = 24 * 3600         # однаковий запит з тими самими результатами — з кешу, без токенів
AI_DOWN = 300                # після помилки моделі — 5 хв без звернень
AI_TIMEOUT = 15
AI_MAX_TOKENS = 4000         # коротка JSON-відповідь + роздуми моделі
TOGETHER_CACHE = 3600
TOGETHER_MIN = 2             # «часто» — щонайменше у двох замовленнях разом
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
# Ціни Anthropic API, $ за 1 млн токенів: запит, відповідь, читання кешу (запис у кеш на 5 хв = 1,25 × запит).
# Haiku 5.5 — для запитів до 100K токенів (каталог магазину значно менший).
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20), "claude-sonnet-5-5": (2.0, 10.0, 0.20),
          "claude-haiku-5-5": (0.10, 0.50, 0.01)}
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
LANGS = {"de": "Deutsch", "en": "English", "uk": "Українська", "ar": "العربية", "es": "Español", "fr": "Français",
         "it": "Italiano", "ja": "日本語", "ko": "한국어", "tr": "Türkçe", "zh": "中文"}
_SKU = re.compile(r"^[A-Za-z0-9._\-/]{1,64}$")

INSTRUCTIONS = (
    "Du bist Produktberater eines B2B-Online-Shops für HF-Komponenten (Antennen, HF-Filter, Kabelkonfektion, "
    "Messtechnik). Ein Besucher hat im Shop gesucht. Schlage bis zu 3 weitere Produkte aus dem KATALOG vor, die er "
    "sich zusätzlich ansehen sollte: technisch passende Alternativen, passendes Zubehör (z. B. Kabel mit dem "
    "gleichen Stecker) oder ergänzende Produkte für seine Anwendung.\n"
    "Regeln: nur Artikel (SKU) aus dem KATALOG; keine Artikel aus der Liste «results»; nichts erfinden; keine Preise. "
    "Je Vorschlag eine Begründung: ein kurzer sachlicher Satz (höchstens 20 Wörter) in der Sprache «language». "
    "Passt nichts sinnvoll, gib eine leere Liste zurück.\n"
    "Antworte ausschließlich mit JSON: {\"tips\": [{\"sku\": \"…\", \"reason\": \"…\"}]}"
)


def normalize(q) -> str:
    return " ".join(str(q or "").lower().split())[:MAX_QUERY]


def clean_skus(value) -> list[str]:
    items = value.split(",") if isinstance(value, str) else (value or [])
    return list(dict.fromkeys(s.strip() for s in items if isinstance(s, str) and _SKU.match(s.strip())))[:10]


# ── Журнал запитів ─────────────────────────────────────────────────────────────

def record(shop: Shop, q, results: int) -> None:
    """Пошук на сайті (після паузи в наборі): +1 до запиту, без результатів — окремий лічильник."""
    query = normalize(q)
    if len(query) < 2:
        return
    results = max(0, int(results))
    zero = 1 if results == 0 else 0
    fields = dict(searches=F("searches") + 1, zero_results=F("zero_results") + zero, last_results=results,
                  last_at=timezone.now())
    if ShopSearchQuery.objects.filter(shop=shop, query=query).update(**fields):
        return
    try:
        with transaction.atomic():
            ShopSearchQuery.objects.create(shop=shop, query=query, searches=1, zero_results=zero, last_results=results)
    except IntegrityError:   # паралельний запит створив рядок першим
        ShopSearchQuery.objects.filter(shop=shop, query=query).update(**fields)
        return
    excess = ShopSearchQuery.objects.filter(shop=shop).count() - MAX_ROWS
    if excess > 0:
        old = list(ShopSearchQuery.objects.filter(shop=shop).order_by("last_at").values_list("pk", flat=True)[:excess])
        ShopSearchQuery.objects.filter(pk__in=old).delete()


# ── Часто купують разом ────────────────────────────────────────────────────────

def _visible(shop: Shop):
    return ShopListing.objects.filter(shop=shop, is_visible=True, product__is_active=True)


def together(shop: Shop, skus, limit: int = 4) -> list[dict]:
    """Товари магазину, які за 3 роки щонайменше двічі були в одному замовленні з цими: [{sku, orders}]."""
    skus = clean_skus(skus)
    if not skus:
        return []
    key = "shop-search-together:%s:%s" % (shop.pk, hashlib.sha256("|".join(sorted(skus)).encode()).hexdigest()[:24])
    hit = cache.get(key)
    if hit is not None:
        return hit
    from sales.models import SalesOrderLine
    since = timezone.localdate() - timedelta(days=3 * 365)
    orders = (SalesOrderLine.objects.filter(product__sku__in=skus)
              .filter(Q(order__order_date__gte=since) | Q(order__order_date__isnull=True))
              .exclude(order__status="cancelled").values("order_id"))
    visible = set(_visible(shop).values_list("product__sku", flat=True))
    rows = (SalesOrderLine.objects.filter(order_id__in=orders, product__isnull=False).exclude(product__sku__in=skus)
            .values("product__sku").annotate(n=Count("order", distinct=True)).order_by("-n", "product__sku")[:30])
    out = [{"sku": r["product__sku"], "orders": r["n"]} for r in rows
           if r["product__sku"] in visible and r["n"] >= TOGETHER_MIN][:limit]
    cache.set(key, out, TOGETHER_CACHE)
    return out


# ── AI-підказки ────────────────────────────────────────────────────────────────

def catalog_text(shop: Shop) -> tuple[str, set]:
    """Каталог магазину для моделі: «SKU | назва | категорія | параметри» — стабільний порядок (кеш промпту)."""
    from api.serializers import split_attributes
    lines, skus = [], set()
    for listing in _visible(shop).select_related("product").order_by("product__sku"):
        p = listing.product
        params, _codes = split_attributes(p.tech_attributes)
        attrs = "; ".join(f"{k}: {v}" for k, v in list(params.items())[:10])
        category = str(getattr(p, "category", "") or "")
        lines.append(" | ".join(x for x in (p.sku, (p.name or "").strip(), category, attrs[:260]) if x))
        skus.add(p.sku)
    return "\n".join(lines), skus


def usage(shop: Shop) -> dict:
    """Лічильники за сьогодні (обнуляються з новою добою), загалом (total) і останній збій AI (ai_problem)."""
    u = dict(shop.search_ai_usage or {})
    if u.get("day") != timezone.localdate().isoformat():
        u = {"day": timezone.localdate().isoformat(), "total": u.get("total") or {},
             **{k: u[k] for k in ("ai_problem", "rag_model") if u.get(k)}}
    return u


def cost_usd(model, input=0, output=0, cache_read=0, cache_write=0) -> float:
    """Вартість одного виклику за цінами Anthropic API (без ПДВ); невідома модель — 0."""
    price = next((p for m, p in PRICES.items() if str(model or "").startswith(m)), None)
    if price is None:
        return 0.0
    inp, out, read = price
    return (input * inp + cache_write * inp * 1.25 + cache_read * read + output * out) / 1_000_000


def spent(d: dict) -> str:
    """Витрачено, $ — з лічильника cost_musd (мільйонні долара)."""
    value = int((d or {}).get("cost_musd") or 0) / 1_000_000
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def _count(shop: Shop, problem=None, rag_model=None, **inc) -> None:
    """+inc до лічильників; problem — записати збій AI, False — зняти (успішний виклик); rag_model — чим відповів RAG."""
    with transaction.atomic():
        raw = Shop.objects.select_for_update().values_list("search_ai_usage", flat=True).get(pk=shop.pk)
        shop.search_ai_usage = raw or {}
        u = usage(shop)
        total = dict(u.get("total") or {})
        if "cost_musd" in inc:   # лічильники до обліку вартості — оцінка за поточною моделлю
            for d in (u, total):
                if "cost_musd" not in d and any(d.get(k) for k in TOKEN_KEYS):
                    d["cost_musd"] = round(cost_usd(shop.search_ai_model,
                                                    **{k: int(d.get(k) or 0) for k in TOKEN_KEYS}) * 1_000_000)
        for k, v in inc.items():
            u[k] = int(u.get(k) or 0) + int(v or 0)
            total[k] = int(total.get(k) or 0) + int(v or 0)
        u["total"] = total
        if problem:
            u["ai_problem"] = problem
        elif problem is False:
            u.pop("ai_problem", None)
        if rag_model:
            u["rag_model"] = rag_model
        Shop.objects.filter(pk=shop.pk).update(search_ai_usage=u)
        shop.search_ai_usage = u


def _call(shop: Shop, catalog: str, payload: dict):
    """Один запит до моделі: інструкція й каталог — у system (кешуються), запит — у повідомленні."""
    import anthropic
    from strategy.models import AISettings
    key = AISettings.get().anthropic_api_key
    if not key:
        raise RuntimeError("no_api_key")
    client = anthropic.Anthropic(api_key=key, timeout=AI_TIMEOUT, max_retries=0)
    model = shop.search_ai_model or "claude-opus-5-5"
    extra = {"extra_body": {"output_config": {"effort": "low"}}}   # коротка відповідь — швидко й мало токенів
    if model in FALLBACK_MODELS:   # відмова класифікатора → сервер сам пробує іншу модель
        extra["extra_body"]["fallbacks"] = "default"
        extra["extra_headers"] = {"anthropic-beta": FALLBACK_BETA}
    return client.messages.create(
        model=model, max_tokens=AI_MAX_TOKENS,
        system=[{"type": "text", "text": INSTRUCTIONS},
                {"type": "text", "text": "KATALOG (SKU | Name | Kategorie | Merkmale):\n" + catalog,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        **extra)


def problem_of(e) -> dict:
    """Збій звернення до Anthropic → {code, text, at}: що сталося і що зробити (для картки магазину)."""
    status = getattr(e, "status_code", None)
    body = getattr(e, "body", None)
    kind = str(getattr(e, "type", "") or "")
    if not kind and isinstance(body, dict) and isinstance(body.get("error"), dict):
        kind = str(body["error"].get("type") or "")
    message = str(getattr(e, "message", "") or e).lower()
    if str(e) == "no_api_key":
        code, text = "no_key", "не задано ключ Anthropic (Minerva AI → Налаштування)"
    elif status == 402 or kind == "billing_error" or "credit balance" in message:
        code, text = "billing", ("на рахунку API Anthropic закінчились кредити — поповніть у Claude Console → "
                                 "«Abrechnung» → «Credits kaufen» (це окремий рахунок, не підписка Claude)")
    elif status == 401 or kind == "authentication_error":
        code, text = "auth", "Anthropic не приймає ключ API — перевірте ключ у «Minerva AI → Налаштування»"
    elif status == 403 or kind == "permission_error":
        code, text = "permission", "ключ API не має доступу до цієї моделі — виберіть іншу модель або перевірте ключ"
    elif status == 404 or kind == "not_found_error":
        code, text = "model", "модель недоступна для цього рахунку API — виберіть іншу модель"
    elif status == 400 and "anthropic-beta" in message:
        code, text = "beta", "рахунок API не приймає резервні моделі (fallbacks) — виберіть модель Haiku 5.5"
    elif status == 429 or kind == "rate_limit_error":
        code, text = "rate", "Anthropic обмежив кількість запитів — Minerva повторить за 5 хв"
    else:
        code, text = "unavailable", "Anthropic не відповів (мережа, тайм-аут або перевантаження) — Minerva повторить за 5 хв"
    return {"code": code, "text": text, "at": timezone.now().isoformat()}


def _parse(text: str) -> list:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    tips = data.get("tips") if isinstance(data, dict) else None
    return tips if isinstance(tips, list) else []


def ai_tips(shop: Shop, q, results, lang: str = "") -> dict:
    """{status: ok|off|short|limit|unavailable, tips[{sku, reason}], cached}. Збій — status без підказок."""
    if shop.search_assist not in Shop.SEARCH_WITH_AI:
        return {"status": "off", "tips": []}
    query = normalize(q)
    if len(query) < 3:
        return {"status": "short", "tips": []}
    lang = lang if lang in LANGS else "en"
    results = clean_skus(results)
    catalog, skus = catalog_text(shop)
    if not skus:
        return {"status": "unavailable", "tips": []}
    digest = hashlib.sha256("|".join([shop.search_ai_model, lang, query, ",".join(sorted(results)),
                                      hashlib.sha256(catalog.encode()).hexdigest()]).encode()).hexdigest()[:32]
    key = f"shop-search-ai:{shop.pk}:{digest}"
    hit = cache.get(key)
    if hit is not None:
        _count(shop, cached=1)
        return {**hit, "status": "ok", "cached": True}
    if cache.get(f"shop-search-ai-down:{shop.pk}"):
        return {"status": "unavailable", "tips": []}
    if int(usage(shop).get("calls") or 0) >= shop.search_ai_daily_limit:
        return {"status": "limit", "tips": []}
    try:
        response = _call(shop, catalog, {"query": query, "language": LANGS[lang], "results": results})
    except Exception as e:  # noqa: BLE001 — модель недоступна: сайт лишається з підказками за правилами
        problem = problem_of(e)
        logger.warning("shop search AI failed (%s, %s): %s", shop.slug, problem["code"], e)
        cache.set(f"shop-search-ai-down:{shop.pk}", 1, AI_DOWN)
        _count(shop, problem=problem, errors=1)
        return {"status": "unavailable", "tips": []}
    u = getattr(response, "usage", None)
    tokens = {"input": getattr(u, "input_tokens", 0) or 0, "output": getattr(u, "output_tokens", 0) or 0,
              "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
              "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0}
    served = getattr(response, "model", "") or shop.search_ai_model   # резервна модель має свою ціну
    _count(shop, problem=False, calls=1, cost_musd=round(cost_usd(served, **tokens) * 1_000_000), **tokens)
    text = "" if getattr(response, "stop_reason", "") == "refusal" else "".join(
        getattr(b, "text", "") for b in (response.content or []) if getattr(b, "type", "") == "text")
    tips, seen = [], set(results)
    for tip in _parse(text):
        sku = str((tip or {}).get("sku") or "").strip() if isinstance(tip, dict) else ""
        reason = " ".join(str(tip.get("reason") or "").split())[:240] if isinstance(tip, dict) else ""
        if sku in skus and sku not in seen and reason:   # лише справжні товари магазину
            seen.add(sku)
            tips.append({"sku": sku, "reason": reason})
        if len(tips) >= AI_TIPS:
            break
    data = {"tips": tips, "model": getattr(response, "model", "") or shop.search_ai_model}
    cache.set(key, data, AI_CACHE)
    return {**data, "status": "ok", "cached": False}


def ai_ready(shop: Shop) -> str:
    """Чому AI-підказки не працюють ('' — усе гаразд)."""
    try:
        from strategy.models import AISettings
        if not AISettings.get().anthropic_api_key:
            return "не задано ключ Anthropic (Minerva AI → Налаштування)"
    except Exception:  # noqa: BLE001
        return "налаштування Minerva AI недоступні"
    if int(usage(shop).get("calls") or 0) >= shop.search_ai_daily_limit:
        return "денний ліміт вичерпано — до кінця доби лише підказки за правилами"
    problem = usage(shop).get("ai_problem")
    if isinstance(problem, dict) and problem.get("text"):
        from django.utils.dateparse import parse_datetime
        at = parse_datetime(str(problem.get("at") or ""))
        when = f" (останній збій {timezone.localtime(at):%d.%m %H:%M})" if at else ""
        return f"{problem['text']}{when}; після першого успішного запиту попередження зникне"
    return ""


# ── Підказки нашого RAG ────────────────────────────────────────────────────────

RAG_TIPS = 3
RAG_CACHE = 24 * 3600
RAG_EMPTY_CACHE = 3600       # RAG і запасний пошук нічого не знайшли — годину не питати знову
RAG_FAIL_CACHE = 120         # завдання не вдалося / загубилось (перезапуск RAG, тайм-аут) — повторити незабаром
RAG_JOB_MAX = 180            # завдання довше — вважається втраченим (локальна модель на RAG-ПК відповідає до 1–2 хв)
RAG_PARALLEL = 1             # RAG обробляє один запит за раз (ним користуються й співробітники в Minerva)
RAG_TIMEOUT = 4              # одне звернення Minerva → RAG: сайт чекає відповіді Minerva
RAG_DOWN = 300
RAG_ABANDONED = 15           # завдання, яке сайт не опитував стільки секунд, — відвідувач пішов: зупинити на RAG-ПК
RAG_ACTIVE = "shop-search-rag-active"
RAG_DOWN_KEY = "shop-search-rag-down"
RAG_LANGS = {"de", "en", "uk"}   # мови відповідей RAG; інші — англійською
RAG_QUESTION = {
    "de": "Kundensuche in unserem Online-Shop: «{q}». Welche unserer Produkte passen am besten? Nenne höchstens 3 "
          "Artikelnummern aus unserem Sortiment, zu jeder einen kurzen Satz, warum (laut Datenblatt). Kurz, ohne Preise.",
    "en": "Customer search in our online shop: \"{q}\". Which of our products fit best? Name at most 3 part numbers "
          "from our range, each with one short sentence why (according to the datasheet). Brief, no prices.",
    "uk": "Пошук покупця в нашому інтернет-магазині: «{q}». Які наші товари підходять найкраще? Назви щонайбільше 3 "
          "артикули з нашого асортименту, до кожного одне коротке речення чому (за даташитом). Коротко, без цін.",
}
_MARKUP = re.compile(r"\*\*|__|`|\[\d+(?:\s*[,–-]\s*\d+)*\]|^\s*(?:#+|[-*•]|\d+[.)])\s+", re.M)
_TOKEN = re.compile(r"(?<![\w./-])([A-Za-z0-9][A-Za-z0-9._/-]{2,62}[A-Za-z0-9])(?![\w-])")
_EMPTY = {"items": [], "note": ""}


def _plain(text) -> str:
    return re.sub(r"\s+([.,;:!?])", r"\1", " ".join(_MARKUP.sub("", text or "").split()))


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:–—-") + " …"


def _reason(answer: str, names) -> str:
    """Речення відповіді RAG про цей товар (без артикула й розмітки)."""
    for line in (answer or "").splitlines():
        low = line.lower()
        name = next((n for n in names if n and n.lower() in low), None)
        if name is None or "|" in line:          # рядок таблиці — не речення
            continue
        text = _plain(re.sub(r"\(\s*\)", "", re.sub(re.escape(name), "", _plain(line), flags=re.I)))
        text = re.sub(r"^[\s\-–—:;,.()]+", "", text).strip()
        if len(text) >= 8:
            return _cut(text[:1].upper() + text[1:], 200)
    return ""


def _note(answer: str, names) -> str:
    """Перше звичайне речення відповіді (не про конкретний товар) — «пару слів» над підказками."""
    for line in (answer or "").splitlines():
        text = _plain(line)
        low = text.lower()
        if len(text) < 20 or "|" in text or any(n and n.lower() in low for n in names):
            continue
        out = ""
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if out and len(out) + len(sentence) > 220:
                break
            out = (out + " " + sentence).strip()
        return _cut(out, 220)
    return ""


def _rag_result(result: dict) -> dict:
    """Відповідь RAG → артикули Minerva з поясненням: спершу товари з products, потім артикули з тексту."""
    from inventory.services.stock import resolve_sku
    answer = str(result.get("answer") or "")
    items, seen = [], set()

    def add(product, raw):
        if product is not None and product.sku not in seen and len(items) < 8:
            seen.add(product.sku)
            items.append({"sku": product.sku, "reason": _reason(answer, {raw, product.sku})})

    for p in (result.get("products") or [])[:15]:
        raw = str((p.get("product") or p.get("sku") or "") if isinstance(p, dict) else "").strip()
        if raw:
            add(resolve_sku(raw), raw)
    tokens = [t for t in dict.fromkeys(_TOKEN.findall(answer)) if any(ch.isdigit() for ch in t)][:20]
    if tokens:
        from inventory.models import Product
        found = {p.sku.lower(): p for p in Product.objects.filter(sku__in=tokens)}
        for t in tokens:
            add(found.get(t.lower()), t)
    return {"items": items, "note": _note(answer, {i["sku"] for i in items})}


def _rag_settings():
    from rag_assistant.models import RagSettings
    return RagSettings.get()


def _rag_client(settings):
    from rag_assistant.client import RagClient
    client = RagClient(settings, client_id="shop-search")
    client.timeout = RAG_TIMEOUT
    return client


def _active() -> dict:
    now = time.time()
    return {k: t for k, t in (cache.get(RAG_ACTIVE) or {}).items() if now - t < RAG_JOB_MAX}


def _set_active(digest: str, on: bool) -> None:
    active = _active()
    if on:
        active[digest] = time.time()
    else:
        active.pop(digest, None)
    cache.set(RAG_ACTIVE, active, RAG_JOB_MAX * 2)


def _rag_out(shop: Shop, item: dict, cached: bool) -> dict:
    visible = set(_visible(shop).values_list("product__sku", flat=True))
    tips = [dict(t) for t in item.get("items") or [] if t.get("sku") in visible][:RAG_TIPS]
    if cached:
        _count(shop, rag_cached=1)
    return {"status": "ok", "tips": tips, "note": item.get("note", "") if tips else "", "cached": cached}


def _rag_poll(shop: Shop, settings, key: str, digest: str, job: dict) -> dict:
    from rag_assistant.client import RagError
    jobkey = f"shop-search-rag-job:{digest}"

    def finish(item, ttl):
        cache.delete(jobkey)
        _set_active(digest, False)
        cache.set(key, item, ttl)

    if time.time() - float(job.get("at") or 0) > RAG_JOB_MAX:
        finish(_EMPTY, RAG_FAIL_CACHE)
        _count(shop, rag_errors=1)
        return {"status": "unavailable", "tips": []}
    cache.set(jobkey, {**job, "polled": time.time()}, RAG_JOB_MAX + 60)   # сайт ще чекає — не покинуте
    try:
        data = _rag_client(settings).job(job["id"])
    except RagError as e:
        if e.code in ("not_found", "auth", "bad_request"):
            finish(_EMPTY, RAG_FAIL_CACHE)
            _count(shop, rag_errors=1)
            return {"status": "unavailable", "tips": []}
        return {"status": "pending", "tips": []}   # мережа на мить — сайт спитає ще раз
    state = data.get("status")
    if state not in ("completed", "failed"):   # сайт показує, що Minerva працює: етап RAG і скільки минуло
        return {"status": "pending", "tips": [], "stage": str(data.get("stage") or "")[:80],
                "elapsed": int(time.time() - float(job.get("at") or time.time()))}
    if state == "failed":
        finish(_EMPTY, RAG_FAIL_CACHE)
        _count(shop, rag_errors=1)
        return {"status": "unavailable", "tips": []}
    result = data.get("result") or {}
    item = _rag_result(result)
    if not item["items"] and job.get("kind") != "search" and job.get("question"):
        # аналізатор RAG інколи будує надто вузький план (лише каталог із фільтрами) і відповідає «немає даних» без
        # моделі — тоді пошук по даташитах без LLM (/v1/search): швидко, без оплати, товари з даташитів
        try:
            search_id = _rag_client(settings).search(job["question"], [], language=job.get("lang") or "en",
                                                     latest=settings.latest_only)
        except RagError:
            search_id = None
        if search_id:
            now = time.time()
            cache.set(jobkey, {**job, "id": search_id, "at": now, "polled": now, "kind": "search"}, RAG_JOB_MAX + 60)
            _count(shop, rag_fallback=1)
            return {"status": "pending", "tips": [], "stage": "", "elapsed": 0}
    finish(item, RAG_CACHE if item["items"] else RAG_EMPTY_CACHE)
    if job.get("kind") != "search" and (result.get("provider") or result.get("model")):   # чим відповідав: локально чи хмарою
        _count(shop, rag_model={"provider": str(result.get("provider") or "")[:40],
                                "model": str(result.get("model") or "")[:80], "at": timezone.now().isoformat()})
    return _rag_out(shop, item, cached=False)


def _cancel(settings, job: dict) -> bool:
    """Зупинити покинуте завдання на RAG-ПК (POST /v1/jobs/{id}/cancel). False — RAG ще не вміє або вже не має його."""
    from rag_assistant.client import RagError
    try:
        data = _rag_client(settings).cancel_job(job["id"])
    except RagError:
        return False
    return data.get("status") in ("cancelling", "completed", "failed")


def rag_tips(shop: Shop, q, lang: str = "") -> dict:
    """{status: ok|pending|busy|limit|unavailable|off|short, tips[{sku, reason}], note, cached}.

    Перший виклик ставить завдання RAG (pending), наступні (сайт опитує) — забирають відповідь."""
    from rag_assistant.client import RagError
    if shop.search_assist not in Shop.SEARCH_WITH_RAG:
        return {"status": "off", "tips": []}
    query = normalize(q)
    if len(query) < 3:
        return {"status": "short", "tips": []}
    lang = lang if lang in RAG_LANGS else "en"
    digest = hashlib.sha256(f"{lang}|{query}".encode()).hexdigest()[:32]
    key = f"shop-search-rag:{digest}"
    hit = cache.get(key)
    if hit is not None:
        return _rag_out(shop, hit, cached=True)
    try:
        settings = _rag_settings()
    except Exception:  # noqa: BLE001
        return {"status": "unavailable", "tips": []}
    if not settings.configured:
        return {"status": "unavailable", "tips": []}
    job = cache.get(f"shop-search-rag-job:{digest}")
    if job:
        return _rag_poll(shop, settings, key, digest, job)
    if cache.get(RAG_DOWN_KEY):
        return {"status": "unavailable", "tips": []}
    if int(usage(shop).get("rag_calls") or 0) >= shop.search_ai_daily_limit:
        return {"status": "limit", "tips": []}
    for other in list(_active()) if len(_active()) >= RAG_PARALLEL else []:
        # завдання, яке більше ніхто не опитує (відвідувач змінив запит): зупинити на RAG-ПК і звільнити чергу;
        # RAG API без зупинки — дочитати, коли буде готове
        other_job = cache.get(f"shop-search-rag-job:{other}")
        if not other_job:
            _set_active(other, False)
        elif (time.time() - float(other_job.get("polled") or other_job.get("at") or 0) > RAG_ABANDONED
              and _cancel(settings, other_job)):
            cache.delete(f"shop-search-rag-job:{other}")
            _set_active(other, False)
            _count(shop, rag_cancelled=1)
        else:
            _rag_poll(shop, settings, f"shop-search-rag:{other}", other, other_job)
    if len(_active()) >= RAG_PARALLEL:
        _count(shop, rag_busy=1)
        return {"status": "busy", "tips": []}
    try:
        job_id = _rag_client(settings).ask(RAG_QUESTION[lang].format(q=query), [], language=lang,
                                           latest=settings.latest_only)
    except RagError as e:
        if e.code == "busy":     # RAG відповідає комусь іншому — сайт спитає ще раз
            _count(shop, rag_busy=1)
            return {"status": "busy", "tips": []}
        logger.warning("shop search RAG failed (%s): %s", shop.slug, e.code)
        cache.set(RAG_DOWN_KEY, 1, RAG_DOWN)
        _count(shop, rag_errors=1)
        return {"status": "unavailable", "tips": []}
    cache.set(f"shop-search-rag-job:{digest}", {"id": job_id, "at": time.time(), "lang": lang,
                                                 "question": RAG_QUESTION[lang].format(q=query)}, RAG_JOB_MAX + 60)
    _set_active(digest, True)
    _count(shop, rag_calls=1)
    return {"status": "pending", "tips": [], "stage": "", "elapsed": 0}


RAG_LOCAL = {"ollama"}   # постачальники RAG, що працюють на самому RAG-ПК (без оплати)


def rag_is_local(shop: Shop):
    """True — RAG відповідає без оплати (відповідь і аналізатор локальні), False — щось іде в хмару, None — невідомо
    (результат завдання називає лише модель відповіді; аналізатор показує «🔄 Запитати RAG-ПК»)."""
    m = usage(shop).get("rag_model")
    if not isinstance(m, dict) or not m.get("provider"):
        return None
    if m.get("provider") not in RAG_LOCAL or (m.get("analyzer_provider") and m["analyzer_provider"] not in RAG_LOCAL):
        return False
    return True if m.get("source") == "health" else None


def rag_model_text(shop: Shop) -> str:
    """Чим RAG відповідає (останнє завдання або запит «яка модель зараз») і чи це платно; '' — ще невідомо."""
    m = usage(shop).get("rag_model")
    if not isinstance(m, dict) or not (m.get("provider") or m.get("model")):
        return ""
    from django.utils.dateparse import parse_datetime
    provider, model = m.get("provider") or "?", m.get("model") or "?"
    analyzer = (f" · аналізатор запиту: {m['analyzer_provider']} / {m.get('analyzer_model') or '?'}"
                if m.get("analyzer_provider") else "")
    at = parse_datetime(str(m.get("at") or ""))
    source = "запит до RAG-ПК" if m.get("source") == "health" else "останнє завдання"
    when = f" · {source} {timezone.localtime(at):%d.%m %H:%M}" if at else ""
    local = rag_is_local(shop)
    if local:
        return f"RAG відповідає: {provider} / {model}{analyzer} — локально на RAG-ПК, без оплати{when}"
    if local is None:
        return (f"RAG відповідає: {provider} / {model} — модель відповіді локальна, без оплати; чи платний аналізатор "
                f"запиту — «🔄 Запитати RAG-ПК, яка модель зараз»{when}")
    cloud = sorted({p for p in (provider, m.get("analyzer_provider")) if p and p not in RAG_LOCAL})
    return (f"⚠ RAG відповідає: {provider} / {model}{analyzer} — хмарна модель: кожна RAG-підказка оплачується ключем "
            f"{', '.join(cloud)} на RAG-ПК (не ключем Minerva, у витратах вище не враховано). Безкоштовно — RAG Documents → "
            f"Налаштування → постачальник ollama (qwen2.5:7b){when}")


def rag_now(shop: Shop) -> dict:
    """Кнопка «Запитати RAG-ПК, яка модель зараз»: GET /v1/health → llm (без ключів); результат — у rag_model."""
    from rag_assistant.client import RagError
    try:
        settings = _rag_settings()
    except Exception:  # noqa: BLE001
        return {"ok": False, "text": "налаштування помічника по продукції недоступні"}
    if not settings.configured:
        return {"ok": False, "text": "не задано адресу або ключ RAG API (Minerva AI → «📚 Помічник по продукції»)"}
    try:
        data = _rag_client(settings).health()
    except RagError as e:
        return {"ok": False, "text": f"RAG-ПК не відповів: {e.message}"}
    llm = data.get("llm") if isinstance(data, dict) else None
    if not isinstance(llm, dict) or not llm.get("provider"):
        return {"ok": False, "text": "RAG-ПК працює, але ще не повідомляє модель — перезапустіть на RAG-ПК задачу "
                                     "«RAG-Integration-API» (оновлений rag_api.py уже там)"}
    clean = lambda v, n: str(v or "")[:n] or None  # noqa: E731
    _count(shop, rag_model={"provider": clean(llm.get("provider"), 40), "model": clean(llm.get("model"), 80),
                            "analyzer_provider": clean(llm.get("analyzer_provider"), 40),
                            "analyzer_model": clean(llm.get("analyzer_model"), 80),
                            "at": timezone.now().isoformat(), "source": "health"})
    return {"ok": True, "text": rag_model_text(shop), "local": rag_is_local(shop)}


def rag_ready(shop: Shop) -> str:
    """Чому RAG-підказки не працюють ('' — усе гаразд)."""
    try:
        settings = _rag_settings()
    except Exception:  # noqa: BLE001
        return "налаштування помічника по продукції недоступні"
    if not settings.configured:
        return "не задано адресу або ключ RAG API (Minerva AI → «📚 Помічник по продукції»)"
    if cache.get(RAG_DOWN_KEY):
        return "RAG щойно не відповів — 5 хв пауза, сайт показує підказки за правилами"
    if int(usage(shop).get("rag_calls") or 0) >= shop.search_ai_daily_limit:
        return "денний ліміт RAG вичерпано — до кінця доби лише інші підказки"
    return ""
