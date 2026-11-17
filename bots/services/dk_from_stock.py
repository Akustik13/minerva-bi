"""
bots/services/dk_from_stock.py — 📦 → 🔷 картка DigiKey Marketplace зі складу Minerva.

Що потрібно DigiKey для товару:
  • поля товару з Custom v1 (`GET /custom/fields?Owner=Product`: code, name, fieldType, required, допустимі значення;
    `field_defs`, кеш на добу в core.shared_cache, збій — останній збережений список);
  • параметри категорії — числові коди, які вже мають наші лістинги в тій самій категорії DigiKey
    (`category_codes`: dk_attributes + поля фільтра fa_*), — DigiKey не дає шаблону категорії через API,
    тож «як у схожих товарів».
  • категорії й параметри категорії — з каталогу DigiKey (ProductSearch v4, той самий ключ, що пошук товарів):
    `dk_categories` — дерево `/search/categories`, `category_params` — параметри категорії з найчастішими значеннями
    (`/search/keyword` з фільтром категорії → FilterOptions.ParametricFilters; ParameterId = числовий код
    additionalFields Marketplace: 16 Package / Case, 69 Mounting Type…). Кеш на тиждень; DigiKey не відповідає —
    як раніше, лише з наших лістингів (`ps_down` — 5 хв без повторів).
Шаблон (`template`) = обов'язкові поля DigiKey + пакування й життєвий цикл + параметри категорії. `autofill` бере
значення з товару (tech_attributes за назвою поля й синонімами, статус життєвого циклу, пакування як у наших
лістингів) і повертає, чого бракує. `build_draft` — уся картка (назва ≤ 50, опис ≤ 2048, виробник, фото / даташит,
категорія, ціни за правилами «💲 Ціни DigiKey ← Minerva», MOQ, строк, атрибути); `create_listing` — лістинг
(card_source = stock), а значення, які людина дописала, зберігаються й у товар (tech_attributes — наступного разу
не питатимемо).
"""
import logging
import re
from collections import Counter

logger = logging.getLogger(__name__)

FIELDS_CACHE_KEY = "dk-product-field-defs"
FIELDS_TTL = 24 * 3600
CATEGORIES_KEY = "dk-ps-categories"
PARAMS_KEY = "dk-ps-params:{}"
CATALOG_TTL = 7 * 24 * 3600
PS_DOWN_KEY, PS_DOWN_SECONDS = "dk-ps-down", 300
PARAM_VALUES = 25
CATEGORIES_PATH = "/products/v4/search/categories"
# вид товару → назва категорії-листка в каталозі DigiKey
KIND_CATEGORIES = {"filter": ["RF Filters"], "antenna": ["RF Antennas"],
                   "cable": ["Coaxial Cables (RF)", "RF Cable Assemblies"],
                   "connector": ["Coaxial Connector (RF) Adapters", "Coaxial Connectors (RF)"]}
CONNECTOR_WORDS = ("stecker", "connector", "adapter", "buchse", "kupplung", "jack", "plug", "роз'єм", "перехідник")
LIFECYCLE = {"active": "Active", "nrnd": "Not For New Designs", "discontinued": "Obsolete"}
DEFAULT_PACKAGING = "Bulk"
COMMON = ("packaging", "productLifecycleStatus")
COMMON_NAMES = {"packaging": "Packaging", "productLifecycleStatus": "Product Life Cycle Status"}
TITLE_MAX, DESC_MAX, MANUFACTURER_MAX, SKU_MAX = 50, 2048, 50, 50
DESC_ATTRS = 20
# числовий код DigiKey → поле фільтра лістингу (як get_filter_attributes_api)
FILTER_FIELDS = {"139": "fa_frequency", "398": "fa_bandwidth", "21": "fa_filter_type", "428": "fa_ripple",
                 "327": "fa_insertion_loss", "69": "fa_mounting_type", "16": "fa_package_case",
                 "46": "fa_size_dimension", "966": "fa_height_max"}
FILTER_NAMES = {"139": "Frequency", "398": "Bandwidth", "21": "Filter Type", "428": "Ripple",
                "327": "Insertion Loss", "69": "Mounting Type", "16": "Package / Case", "46": "Size / Dimension",
                "966": "Height - Max"}
# назва поля DigiKey → інші назви того самого параметра в tech_attributes складу
SYNONYMS = {
    "frequency": ["frequency range", "frequency band", "center frequency", "frequenz"],
    "connectortype": ["connector", "connector type", "termination", "anschluss"],
    "impedance": ["impedanz"],
    "gain": ["antenna gain", "gewinn"],
    "vswr": ["swr"],
    "length": ["cable length", "länge"],
    "packagecase": ["package", "case", "gehäuse"],
    "mountingtype": ["mounting", "montage"],
}


def _norm(text) -> str:
    return re.sub(r"[^0-9a-zа-яіїєґ]+", "", str(text or "").lower())


def _cache():
    from core.shared_cache import cache
    return cache


# ── Поля DigiKey ──────────────────────────────────────────────────────────────

def field_defs(config=None, refresh=False) -> dict:
    """{code: {code, name, type, required, values}} полів товару DigiKey, які постачальник може заповнити.
    refresh — запитати DigiKey зараз; без мережі / токена — останній збережений список (або {})."""
    cached = _cache().get(FIELDS_CACHE_KEY)
    if cached is not None and not refresh:
        return cached
    from bots.models import DigiKeyConfig
    from bots.services.dk_marketplace import fetch_custom_fields
    try:
        raw = fetch_custom_fields(config or DigiKeyConfig.get())
    except Exception as exc:  # noqa: BLE001 — DKMarketplaceError, мережа, токен: що було
        logger.warning("DK field_defs: %s", exc)
        if refresh:
            raise
        return cached or {}
    out = {}
    for f in raw:
        code = str(f.get("code") or "").strip()
        if not code or f.get("permissionLevel") in ("Invisible", "ReadOnly"):
            continue
        values = [str(v.get("value") if isinstance(v, dict) else v) for v in (f.get("fieldValues") or [])]
        values += [str(p.get("label") or p.get("code")) for p in (f.get("fieldLabelCodePairs") or [])
                   if isinstance(p, dict)]
        out[code] = {"code": code, "name": f.get("name") or code, "type": f.get("fieldType") or "Text",
                     "required": bool(f.get("required")),
                     "values": list(dict.fromkeys(v for v in values if v and v != "None"))}
    _cache().set(FIELDS_CACHE_KEY, out, FIELDS_TTL)
    return out


def writable_codes() -> set:
    """Нечислові коди полів, які DigiKey дозволяє заповнити (з останнього отриманого списку; без мережі).
    DigiKeyListing.get_all_attributes_api надсилає їх з dk_attributes разом із числовими параметрами."""
    return {c for c in (_cache().get(FIELDS_CACHE_KEY) or {}) if not c.isdigit() and c not in COMMON}


def fields_state() -> dict:
    """Для сторінки: чи є список полів DigiKey і скільки обов'язкових."""
    defs = _cache().get(FIELDS_CACHE_KEY) or {}
    return {"known": bool(defs), "count": len(defs), "required": sum(1 for d in defs.values() if d["required"])}


# ── Каталог DigiKey (ProductSearch v4) ───────────────────────────────────────

def _ps(method, path, body=None):
    """Запит до ProductSearch v4 (OAuth client credentials з налаштувань DigiKey)."""
    import requests

    from bots.models import DigiKeyConfig
    from bots.services.digikey import _base_url, _headers, get_token
    config = DigiKeyConfig.get()
    if not (config.client_id and config.client_secret):
        raise RuntimeError("ProductSearch DigiKey не налаштовано (Client ID / Secret у налаштуваннях DigiKey)")
    token = get_token(config)
    call = requests.post if method == "post" else requests.get
    resp = call(f"{_base_url(config)}{path}", headers=_headers(config, token), timeout=20,
                **({"json": body} if body is not None else {}))
    resp.raise_for_status()
    return resp.json()


def _ps_cached(key, fetch, refresh):
    """Кеш на тиждень; збій — останнє збережене, і 5 хв без нових спроб (refresh — помилка назовні)."""
    cached = _cache().get(key)
    if cached is not None and not refresh:
        return cached
    if not refresh and _cache().get(PS_DOWN_KEY):
        return cached or []
    try:
        value = fetch()
    except Exception as exc:  # noqa: BLE001 — мережа, ключ ProductSearch
        logger.warning("DK ProductSearch %s: %s", key, exc)
        _cache().set(PS_DOWN_KEY, str(exc)[:200], PS_DOWN_SECONDS)
        if refresh:
            raise
        return cached or []
    _cache().set(key, value, CATALOG_TTL)
    _cache().delete(PS_DOWN_KEY)
    return value


def dk_categories(refresh=False) -> list:
    """[{id, name, path, top, leaf, count}] — усі категорії каталогу DigiKey (дерево в один список)."""
    def fetch():
        out = []

        def walk(nodes, path):
            for c in nodes or []:
                name = str(c.get("Name") or "").strip()
                if not name or c.get("CategoryId") is None:
                    continue
                trail = path + [name]
                children = c.get("Children") or []
                out.append({"id": str(c["CategoryId"]), "name": name, "path": " › ".join(trail), "top": trail[0],
                            "leaf": not children, "count": int(c.get("ProductCount") or 0)})
                walk(children, trail)
        walk(_ps("get", CATEGORIES_PATH).get("Categories"), [])
        return out
    return _ps_cached(CATEGORIES_KEY, fetch, refresh)


def category_params(category_id, refresh=False) -> list:
    """[{code, name, type, values}] — параметри категорії каталогу DigiKey і найчастіші значення (за кількістю
    товарів). Лише для числових ID категорій каталогу."""
    category_id = str(category_id or "").strip()
    if not category_id.isdigit():
        return []

    def fetch():
        from bots.services.digikey import KEYWORD_SEARCH_PATH
        data = _ps("post", KEYWORD_SEARCH_PATH, {
            "Keywords": "", "Limit": 1, "Offset": 0,
            "FilterOptionsRequest": {"CategoryFilter": [{"Id": category_id}]}})
        out = []
        for p in (data.get("FilterOptions") or {}).get("ParametricFilters") or []:
            code = p.get("ParameterId")
            if not isinstance(code, int) or code <= 0:
                continue
            values = sorted(p.get("FilterValues") or [], key=lambda v: -(v.get("ProductCount") or 0))
            out.append({"code": str(code), "name": str(p.get("ParameterName") or code), "type": p.get("ParameterType")
                        or "", "values": [str(v["ValueName"]) for v in values[:PARAM_VALUES]
                                          if v.get("ValueName") not in (None, "", "-")]})
        return out
    return _ps_cached(PARAMS_KEY.format(category_id), fetch, refresh)


def catalog_state() -> dict:
    cats = _cache().get(CATEGORIES_KEY) or []
    return {"categories": len(cats), "down": _cache().get(PS_DOWN_KEY) or ""}


def _catalog_by_id() -> dict:
    return {c["id"]: c for c in (_cache().get(CATEGORIES_KEY) or [])}


def category_choices() -> dict:
    """Вибір категорії на сторінці: ваші (з лістингів) і каталог DigiKey (листки, згруповані за розділом)."""
    catalog = _catalog_by_id()
    ours = [{"id": cid, "name": catalog[cid]["path"] if cid in catalog else name, "n": n, "known": cid in catalog}
            for cid, name, n in known_categories()]
    groups = {}
    for c in catalog.values():
        if c["leaf"]:
            groups.setdefault(c["top"], []).append(c)
    return {"ours": ours, "groups": [(top, sorted(items, key=lambda c: c["path"]))
                                     for top, items in sorted(groups.items())],
            "matched": sum(1 for o in ours if o["known"])}


def _listing_codes(listing) -> dict:
    """{числовий код: value} параметрів лістингу, як їх надсилаємо в DigiKey."""
    try:
        attrs = {str(a["code"]): a.get("value") for a in listing.get_all_attributes_api()}
    except Exception:  # noqa: BLE001
        attrs = {str(k): v for k, v in (listing.dk_attributes or {}).items()}
    return {c: v for c, v in attrs.items() if c.isdigit() and v not in (None, "", "-")}


def category_codes(dk_category_id) -> list:
    """Числові коди параметрів, які мають наші лістинги в цій категорії DigiKey, — від найчастішого."""
    from bots.models import DigiKeyListing
    if not dk_category_id:
        return []
    counts = Counter()
    for listing in DigiKeyListing.objects.filter(dk_category_id=dk_category_id):
        counts.update(_listing_codes(listing))
    return [code for code, _n in counts.most_common()]


def category_examples(dk_category_id, code, limit=8) -> list:
    """Значення параметра в наших лістингах цієї категорії — підказка, коли DigiKey не дав списку."""
    from bots.models import DigiKeyListing
    counts = Counter()
    for listing in DigiKeyListing.objects.filter(dk_category_id=dk_category_id):
        value = _listing_codes(listing).get(code)
        if value:
            counts[str(value)] += 1
    return [v for v, _n in counts.most_common(limit)]


def template(dk_category_id, defs=None) -> list:
    """[{code, name, required, values, examples, type}] — обов'язкові поля DigiKey, пакування й життєвий цикл,
    параметри категорії (як у наших схожих лістингів)."""
    defs = field_defs() if defs is None else defs
    params = {p["code"]: p for p in category_params(dk_category_id)}       # каталог DigiKey
    rows, seen = [], set()
    required = [c for c, d in defs.items() if d["required"] and c not in COMMON]
    for code in list(COMMON) + required + category_codes(dk_category_id) + list(params):
        if code in seen:
            continue
        seen.add(code)
        d, p = defs.get(code, {}), params.get(code, {})
        name = (d.get("name") or p.get("name") or COMMON_NAMES.get(code) or FILTER_NAMES.get(code)
                or f"Параметр {code}")
        examples = [] if d.get("values") else list(dict.fromkeys(
            category_examples(dk_category_id, code) + (p.get("values") or [])))[:PARAM_VALUES]
        rows.append({"code": code, "name": name, "type": d.get("type") or "Text",
                     "required": bool(d.get("required")) or code in COMMON,
                     "values": d.get("values") or [], "examples": examples,
                     "from_catalog": bool(p) and code not in defs})
    return rows


# ── Значення зі складу ────────────────────────────────────────────────────────

def _stock_attrs(product) -> dict:
    from api.serializers import split_attributes
    return {str(k): v for k, v in split_attributes(product.tech_attributes or {})[0].items()
            if v not in (None, "", "-")}


def _pick(allowed, value):
    """Значення з допустимих (без регістру й розділювачів); немає списку — як є; не підходить — None."""
    if not allowed:
        return value
    want = _norm(value)
    for v in allowed:
        if _norm(v) == want:
            return v
    return None


def _from_stock(name, stock):
    key = _norm(name)
    candidates = [key] + [_norm(s) for s in SYNONYMS.get(key, [])]
    by_norm = {_norm(k): (k, v) for k, v in stock.items()}
    for c in candidates:                                    # точний збіг назви
        if c in by_norm:
            return by_norm[c]
    for c in candidates:                                    # «Frequency» ↔ «Frequency Range»
        for nk, (k, v) in by_norm.items():
            if c and len(c) >= 4 and len(nk) >= 4 and (c in nk or nk in c):
                return k, v
    return None, None


def _value_text(value) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value if str(v).strip())
    return str(value).strip()


def default_packaging() -> str:
    """Пакування, яке найчастіше стоїть у наших лістингах (інакше Bulk)."""
    from bots.models import DigiKeyListing
    common = Counter(DigiKeyListing.objects.exclude(dk_packaging="").values_list("dk_packaging", flat=True))
    return common.most_common(1)[0][0] if common else DEFAULT_PACKAGING


def autofill(product, rows, given=None, ai_sources=None) -> tuple:
    """(values {code: value}, sources {code: звідки}, missing [row + hint]) для шаблону rows.
    given — {code: value}, які людина вже дописала (мають пріоритет); ai_sources — {code: звідки} для тих, що
    вставив «🤖 AI: заповнити» (inventory/services/ai_fill.py) — показуються «🤖 AI · звідки»."""
    given = {k: _value_text(v) for k, v in (given or {}).items() if _value_text(v)}
    ai_sources = ai_sources or {}
    stock = _stock_attrs(product)
    values, sources, missing = {}, {}, []
    for row in rows:
        code, value, source = row["code"], None, ""
        if code in given:
            value, source = given[code], ("🤖 AI · " + ai_sources[code] if code in ai_sources else "вписано вручну")
        elif code == "productLifecycleStatus":
            value, source = LIFECYCLE.get(product.lifecycle_status or "active", "Active"), "життєвий цикл товару"
        elif code == "packaging":
            value, source = default_packaging(), "як у наших лістингів"
        else:
            key, raw = _from_stock(row["name"], stock)
            if key is not None:
                value, source = _value_text(raw), f"склад: {key}"
        if value is not None and value != "":
            picked = _pick(row["values"], value)
            if picked is None:
                source, value = f"{source} — «{value}» немає серед значень DigiKey", None
            else:
                value = picked
        if value is None or str(value).strip() == "":
            missing.append({**row, "hint": source})          # обов'язкові — спершу, решта — «як у схожих»
            continue
        values[code], sources[code] = str(value), source
    return values, sources, missing


# ── Картка ────────────────────────────────────────────────────────────────────

def _cut(text, limit) -> tuple:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text, False
    cut = text[:limit + 1].rsplit(" ", 1)[0].rstrip(",;:-— ")
    return (cut if len(cut) >= limit // 2 else text[:limit]).strip(), True


def _public(url) -> str:
    """Адреса, яку DigiKey зможе відкрити: /media/products/… — публічна (tabele/media.PUBLIC)."""
    if not url:
        return ""
    if url.startswith("http"):
        return url
    from dashboard.notifications import _abs_url
    url = _abs_url(url)
    return url if url.startswith("http") else ""


def known_categories() -> list:
    """[(dk_category_id, назва, лістингів)] категорій DigiKey, де вже є наші лістинги."""
    from django.db.models import Count

    from bots.models import DigiKeyListing
    rows = (DigiKeyListing.objects.exclude(dk_category_id="").values("dk_category_id", "dk_category_name")
            .annotate(n=Count("pk")).order_by("-n"))
    out, seen = [], set()
    for r in rows:
        if r["dk_category_id"] not in seen:
            seen.add(r["dk_category_id"])
            out.append((r["dk_category_id"], r["dk_category_name"] or r["dk_category_id"], r["n"]))
    return out


def guess_category(product) -> dict:
    """{dk_category_id, dk_category_name, category_type, reason} — як у наших лістингів схожих товарів."""
    from django.db.models import Count

    from bots.models import DigiKeyListing
    from labels_app.product_label import kind_of
    kind = kind_of(product.sku, product.category or "")
    text = " ".join([product.sku, product.name or "", product.name_export or "", product.category or ""]).lower()
    if kind not in ("filter", "antenna", "cable") and any(w in text for w in CONNECTOR_WORDS):
        kind = "connector"
    category_type = kind if kind in ("filter", "antenna", "cable", "connector") else "other"
    catalog = _catalog_by_id()
    by_name = {c["name"].lower(): c for c in catalog.values() if c["leaf"]}
    from_catalog = next((by_name[n.lower()] for n in KIND_CATEGORIES.get(category_type, []) if n.lower() in by_name),
                        None)
    listed = DigiKeyListing.objects.exclude(dk_category_id="")
    for label, qs in (("як у товарів тієї ж категорії складу", listed.filter(product__category=product.category)),
                      ("як у товарів того ж виду", listed.filter(category_type=category_type))):
        row = (qs.values("dk_category_id", "dk_category_name", "category_type").annotate(n=Count("pk"))
               .order_by("-n").first())
        known = catalog.get(row["dk_category_id"]) if row else None
        if row and from_catalog and known is not None and not known["leaf"]:
            break                                   # у лістингів лише розділ («RF and Wireless») — точніше з каталогу
        if row:
            return {"dk_category_id": row["dk_category_id"], "dk_category_name": row["dk_category_name"] or "",
                    "category_type": row["category_type"] or category_type, "reason": label}
    if from_catalog:
        return {"dk_category_id": from_catalog["id"], "dk_category_name": from_catalog["name"],
                "category_type": category_type, "reason": f"за видом товару — каталог DigiKey: {from_catalog['path']}"}
    return {"dk_category_id": "", "dk_category_name": "", "category_type": category_type,
            "reason": "схожих товарів на DigiKey ще немає — виберіть категорію з каталогу DigiKey"}


PRICE_STATES = ("ok", "manual", "no_prices", "no_rate", "bad")


def _prices(product, moq=1) -> tuple:
    """([{qty, price}], примітка, стан PRICE_STATES) за правилами «💲 Ціни DigiKey ← Minerva»;
    без цін — ([], причина, стан): немає цін на складі / немає курсу / правила не дали цін."""
    from bots.services import dk_prices
    rules = dk_prices.Rules.from_settings()
    home = dk_prices.home_currency()
    minerva, cur, _src = dk_prices.minerva_tiers(product, rules.source, home)
    if not minerva:
        return [], "на складі цін немає (ні базових, ні «Ціни продажу») — впишіть «кількість: ціна за 1 шт.; …»", "no_prices"
    dk_cur = dk_prices.currency()
    rate = dk_prices.fx(cur, dk_cur)
    if rate is None:
        return [], dk_prices.no_rate_text(cur, dk_cur), "no_rate"
    purchase_rate = dk_prices.fx(home, dk_cur) if product.purchase_price else None
    new, notes = dk_prices.compute(minerva, None, rules, rate, moq, product.purchase_price, purchase_rate)
    if not new:
        return [], "; ".join(notes), "bad"
    return dk_prices.tiers_json(new), "; ".join(notes), "ok"


def _money(value) -> str:
    """240 → «240,00», 100000 → «100 000,00» (сума за кількість)."""
    from decimal import Decimal
    whole, cents = f"{Decimal(value):.2f}".split(".")
    return f"{int(whole):,}".replace(",", "\u00a0") + "," + cents


def price_info(product, prices, reason, state) -> dict:
    """Що показати біля цін на сторінці: ціни складу, кожен тир із сумою за кількість, причина без цін,
    «схоже на суму за всю кількість» (dk_prices.price_suspects)."""
    from decimal import Decimal
    from bots.services import dk_prices
    s = dk_prices.settings()
    minerva, cur, src = dk_prices.minerva_tiers(product, s.source or "base_or_sale", dk_prices.home_currency())
    stock = " · ".join(f"{q} шт. — {dk_prices.fmt_price(p)}" for q, p in minerva)
    rows = [{"qty": t["qty"], "price": t["price"], "total": _money(Decimal(str(t["price"])) * t["qty"])} for t in prices]
    try:
        ref = dk_prices.product_ref(product, s)
    except Exception:  # noqa: BLE001 — лише довідка
        ref = []
    return {"state": state, "reason": reason, "rows": rows,
            "stock": f"{stock} {cur} ({src})" if stock else "",
            "suspects": dk_prices.price_suspects(prices, ref) if state == "manual" else []}


MEDIA_KIND_TEXT = {"image": "фото", "datasheet": "даташита"}


def media_info(product, kind, typed: str = "") -> dict:
    """Фото / даташит для DigiKey на сторінці «Зі складу»: {url, source, reason, suggest, suggest_source}.
    url — що піде в картку (вписане людиною або знайдене dk_media); без нього — reason і, якщо правило DigiKey
    для цього виду порожнє, підказка зі складу (suggest). Лише https://… до dk_media.URL_MAX символів."""
    from bots.services import dk_media
    found, how = dk_media.candidates(product, kind, product.sku)
    auto = dk_media.target(product, kind, product.sku)
    info = {"url": auto, "source": how if auto else "", "reason": "", "suggest": "", "suggest_source": ""}
    typed = (typed or "").strip()
    if typed and typed != auto:
        if not re.match(r"^https?://\S+$", typed):
            info["reason"] = "вписане не схоже на адресу https://… — лишаю знайдене"
        elif len(typed) > dk_media.URL_MAX:
            info["reason"] = f"вписана адреса довша за {dk_media.URL_MAX} символів — DigiKey не прийме"
        else:
            info.update(url=typed, source="вписано вручну")
    if info["url"]:
        return info
    typed_err = info["reason"]
    cfg = dk_media.config()
    if found:
        info["reason"] = f"посилання довше {dk_media.URL_MAX} символів — DigiKey не прийме"
    elif cfg.media_source == cfg.MEDIA_RULE:
        info["reason"] = (f"у «🖼 Фото й даташити для DigiKey» вибрано правило DigiKey, а для {MEDIA_KIND_TEXT[kind]} "
                          "його немає")
    else:
        info["reason"] = (f"у товару немає ні посилання, ні файлу {MEDIA_KIND_TEXT[kind]}"
                          + (" (.pdf)" if kind == "datasheet" else "") + " — картка товару або впишіть адресу тут")
    if typed_err:
        info["reason"] = f"{typed_err}; {info['reason']}"
    alt, alt_how = dk_media.candidates(product, kind, product.sku, stock_only=True)
    alt = [a for a in alt if len(a) <= dk_media.URL_MAX]
    if alt:
        info.update(suggest=alt[0], suggest_source=alt_how)
    return info


def stock_info(product, typed: str = "") -> dict:
    """Залишок складу й кількість для DigiKey: {on_hand, reserved, available, qty, override, reason}.
    Порожньо — DigiKey отримує вільний залишок («Доступно») і далі оновлюється сам; число — «Кількість для
    DigiKey» лістингу (dk_quantity_override)."""
    from inventory.models import Product
    from inventory.services.stock import annotate_stock
    row = annotate_stock(Product.objects.filter(pk=product.pk)).first()
    on_hand = int(row._on_hand) if row else 0
    reserved = int(row._reserved) if row else 0
    available = max(0, on_hand - reserved)
    info = {"on_hand": on_hand, "reserved": reserved, "available": available, "qty": available,
            "override": None, "reason": ""}
    typed = (typed or "").strip().replace(" ", "")
    if typed:
        if typed.isdigit():
            info.update(qty=int(typed), override=int(typed))
        else:
            info["reason"] = "кількість — ціле число від 0; лишаю вільний залишок складу"
    return info


def order_terms(product, overrides) -> dict:
    """MOQ і строк відвантаження для картки DigiKey: {moq, moq_source, lead, lead_source, errors}.
    MOQ — вписаний, інакше «Мін. замовлення в магазинах» товару (shop_min_qty), інакше 1;
    строк — вписаний, інакше «Строк поставки» товару, інакше як у лістингу за замовчуванням."""
    from bots.models import DigiKeyListing
    lot = getattr(product, "shop_min_qty", 1) or 1
    default_lead = DigiKeyListing._meta.get_field("dk_lead_time_days").default
    out = {"moq": lot, "moq_source": "мін. замовлення в магазинах" if lot > 1 else "за замовчуванням",
           "lead": product.lead_time_days or default_lead,
           "lead_source": "строк поставки товару" if product.lead_time_days else "за замовчуванням",
           "errors": []}
    moq = str(overrides.get("dk_min_order_qty") or "").strip().replace(" ", "")
    if moq:
        if moq.isdigit() and int(moq) >= 1:
            if int(moq) != out["moq"]:                 # поле передзаповнене — те саме число не «вручну»
                out.update(moq=int(moq), moq_source="вписано вручну")
        else:
            out["errors"].append("MOQ — ціле число від 1")
    lead = str(overrides.get("dk_lead_time_days") or "").strip().replace(" ", "")
    if lead:
        if lead.isdigit() and int(lead) <= 365:
            if int(lead) != out["lead"]:
                out.update(lead=int(lead), lead_source="вписано вручну")
        else:
            out["errors"].append("строк відвантаження — ціле число днів від 0 до 365")
    return out


def parse_prices(text) -> list:
    """«1: 12,50; 10: 11,20» (або рядками, «10 11.20») → [{qty, price}] за зростанням; помилка — ValueError."""
    from decimal import Decimal, InvalidOperation

    from bots.services.dk_prices import MAX_TIERS, tiers_json
    tiers = {}
    for part in re.split(r"[;\n]+", str(text or "")):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+)\s*(?:[:=x×]|шт\.?|pcs)?\s*([\d.,]+)$", part)
        if not m:
            raise ValueError(f"«{part}» — потрібно «кількість: ціна», напр. «10: 11,20»")
        try:
            price = Decimal(m.group(2).replace(",", "."))
        except InvalidOperation:
            raise ValueError(f"«{part}» — ціна не число") from None
        qty = int(m.group(1))
        if qty < 1 or price <= 0:
            raise ValueError(f"«{part}» — кількість ≥ 1 і ціна > 0")
        tiers[qty] = price
    if len(tiers) > MAX_TIERS:
        raise ValueError(f"DigiKey приймає до {MAX_TIERS} ступенів")
    return tiers_json(sorted(tiers.items()))


def build_draft(product, given=None, overrides=None, ai_sources=None) -> dict:
    """Чернетка картки DigiKey з товару складу: {fields, attrs, sources, missing, notes, category, rows}."""
    from bots.models import DigiKeyListing
    overrides = {k: v for k, v in (overrides or {}).items() if str(v or "").strip()}
    notes = []
    cat = guess_category(product)
    if overrides.get("dk_category_id"):
        cid = overrides["dk_category_id"].strip()
        from_catalog = _catalog_by_id().get(cid)
        name = dict((c, n) for c, n, _ in known_categories()).get(cid) or (from_catalog or {}).get("name", "")
        reason = "вказано вручну"
        if from_catalog and cid not in dict((c, n) for c, n, _ in known_categories()):
            reason += " — з каталогу DigiKey, ваших лістингів у ній ще немає"
        cat.update(dk_category_id=cid, dk_category_name=name, reason=reason)
    base = product.name_export or product.name or product.sku
    title, cut = _cut(overrides.get("dk_title") or base, TITLE_MAX)
    if cut:
        notes.append(f"назву скорочено до {TITLE_MAX} символів")
    stock = _stock_attrs(product)
    desc_parts = [base] + [f"{k}: {_value_text(v)}" for k, v in list(stock.items())[:DESC_ATTRS]]
    description, _ = _cut(overrides.get("dk_description") or ". ".join(desc_parts), DESC_MAX)
    manufacturer = (overrides.get("dk_manufacturer") or product.manufacturer or "").strip()
    if not manufacturer:
        common = Counter(DigiKeyListing.objects.exclude(dk_manufacturer="").values_list("dk_manufacturer", flat=True))
        if common:
            manufacturer = common.most_common(1)[0][0]
            notes.append(f"виробник — як у наших лістингів ({manufacturer})")
    terms = order_terms(product, overrides)
    prices, price_note, price_state = _prices(product, terms["moq"])
    if overrides.get("dk_prices"):
        try:
            prices, price_note, price_state = parse_prices(overrides["dk_prices"]), "ціни вписано вручну", "manual"
        except ValueError as exc:
            prices, price_note, price_state = [], f"ціни: {exc}", "bad"
    # примітки до цін (MOQ, округлення, причина «без цін») — під полем цін (price_info), не в загальних
    if price_state == "manual":
        notes.append(price_note)
    price = price_info(product, prices, price_note, price_state)
    rows = template(cat["dk_category_id"])
    values, sources, missing = autofill(product, rows, given, ai_sources)
    media = {k: media_info(product, k, overrides.get(f"dk_{k}_url" if k == "image" else "dk_datasheet_url"))
             for k in ("image", "datasheet")}           # «🖼 Фото й даташити для DigiKey» + вписане людиною
    image, datasheet = media["image"]["url"], media["datasheet"]["url"]
    stock = stock_info(product, overrides.get("dk_quantity_override"))
    fields = {
        "dk_supplier_sku": product.sku[:SKU_MAX], "dk_title": title, "dk_description": description,
        "dk_manufacturer": manufacturer[:MANUFACTURER_MAX], "dk_category_id": cat["dk_category_id"],
        "dk_category_name": cat["dk_category_name"], "category_type": cat["category_type"],
        "dk_image_url": image, "dk_datasheet_url": datasheet,
        "dk_prices": prices, "dk_min_order_qty": terms["moq"], "dk_quantity_override": stock["override"],
        "dk_lead_time_days": terms["lead"],
        "dk_packaging": values.get("packaging", DEFAULT_PACKAGING),
        "dk_lifecycle_status": values.get("productLifecycleStatus", "Active"),
    }
    if len(product.sku) > SKU_MAX:
        notes.append(f"артикул довший за {SKU_MAX} символів — Supplier SKU скорочено")
    if not image:
        notes.append("немає фото з публічною адресою — DigiKey покаже товар без фото")
    attrs = {c: v for c, v in values.items() if c not in COMMON}
    if cat["category_type"] == "filter":                     # фільтр — ще й у поля fa_* картки
        fields.update({FILTER_FIELDS[c]: v[:100] for c, v in attrs.items() if c in FILTER_FIELDS})
    return {"fields": fields, "attrs": attrs, "sources": sources, "missing": missing, "notes": notes,
            "category": cat, "rows": rows, "price": price, "media": media, "stock": stock, "terms": terms}


class AlreadyListed(Exception):
    def __init__(self, listing):
        self.listing = listing
        super().__init__(f"{listing.product.sku}: лістинг DigiKey уже є")


def create_listing(product, given=None, overrides=None):
    """(лістинг, чернетка) з товару складу (card_source = stock). Значення, які людина дописала, — і в товар."""
    from bots.models import DigiKeyListing
    from bots.services.dk_publish import mark, problems
    existing = DigiKeyListing.objects.filter(product=product).first()
    if existing:
        raise AlreadyListed(existing)
    draft = build_draft(product, given, overrides)
    listing = DigiKeyListing(product=product, card_source="stock", dk_attributes=draft["attrs"], **draft["fields"])
    listing.save()
    names = {r["code"]: r["name"] for r in draft["rows"]}
    learned = {names[c]: draft["attrs"][c] for c in (given or {}) if c in names and c in draft["attrs"]}
    if learned:                                              # наступного разу — зі складу, без питань
        attrs = dict(product.tech_attributes or {})
        attrs.update({k: v for k, v in learned.items() if not attrs.get(k)})
        product.tech_attributes = attrs
        product.save(update_fields=["tech_attributes"])
    mark(listing, "card")
    if not problems(listing) and not [m for m in draft["missing"] if m["required"]]:
        mark(listing, "checked")
    return listing, draft
