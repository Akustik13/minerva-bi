"""Підказки в пошуку сайту магазину — сторона Minerva (режим — Shop.search_assist).

Розумний пошук і підказки за правилами (частота, роз'єм і стандарт у запиті, схожі товари, підходящі кабелі,
наступник) рахує сам сайт (shop.js) з даних каталогу — миттєво й без Minerva. Minerva додає:
- журнал «що шукають і не знаходять» (ShopSearchQuery — лише текст запиту й лічильники, без персональних даних);
- «часто купують разом» — з історії замовлень;
- AI-підказки (режим «ai»): модель вибирає з каталогу магазину до 3 товарів з коротким поясненням; кожен
  артикул перевіряється (є в магазині й видимий, не з поточних результатів), відповіді кешуються на добу,
  є денний ліміт і облік токенів (Shop.search_ai_usage).
Будь-який збій — порожня відповідь, і сайт шукає як звичайно.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import timedelta

from django.core.cache import cache
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
    """Лічильники за сьогодні (обнуляються з новою добою) і загалом (total)."""
    u = dict(shop.search_ai_usage or {})
    if u.get("day") != timezone.localdate().isoformat():
        u = {"day": timezone.localdate().isoformat(), "total": u.get("total") or {}}
    return u


def _count(shop: Shop, **inc) -> None:
    with transaction.atomic():
        raw = Shop.objects.select_for_update().values_list("search_ai_usage", flat=True).get(pk=shop.pk)
        shop.search_ai_usage = raw or {}
        u = usage(shop)
        total = dict(u.get("total") or {})
        for k, v in inc.items():
            u[k] = int(u.get(k) or 0) + int(v or 0)
            total[k] = int(total.get(k) or 0) + int(v or 0)
        u["total"] = total
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
    if shop.search_assist != Shop.SEARCH_AI:
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
        logger.warning("shop search AI failed (%s): %s", shop.slug, e)
        cache.set(f"shop-search-ai-down:{shop.pk}", 1, AI_DOWN)
        _count(shop, errors=1)
        return {"status": "unavailable", "tips": []}
    u = getattr(response, "usage", None)
    _count(shop, calls=1, input=getattr(u, "input_tokens", 0) or 0, output=getattr(u, "output_tokens", 0) or 0,
           cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
           cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0)
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
    return ""
