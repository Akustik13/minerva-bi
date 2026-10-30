"""
bots/services/dk_from_stock.py — 📦 → 🔷 картка DigiKey Marketplace зі складу Minerva.

Що потрібно DigiKey для товару:
  • поля товару з Custom v1 (`GET /custom/fields?Owner=Product`: code, name, fieldType, required, допустимі значення;
    `field_defs`, кеш на добу в core.shared_cache, збій — останній збережений список);
  • параметри категорії — числові коди, які вже мають наші лістинги в тій самій категорії DigiKey
    (`category_codes`: dk_attributes + поля фільтра fa_*), — DigiKey не дає шаблону категорії через API,
    тож «як у схожих товарів».
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
    rows, seen = [], set()
    required = [c for c, d in defs.items() if d["required"] and c not in COMMON]
    for code in list(COMMON) + required + category_codes(dk_category_id):
        if code in seen:
            continue
        seen.add(code)
        d = defs.get(code, {})
        name = d.get("name") or COMMON_NAMES.get(code) or FILTER_NAMES.get(code) or f"Параметр {code}"
        rows.append({"code": code, "name": name, "type": d.get("type") or "Text",
                     "required": bool(d.get("required")) or code in COMMON,
                     "values": d.get("values") or [],
                     "examples": [] if d.get("values") else category_examples(dk_category_id, code)})
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


def autofill(product, rows, given=None) -> tuple:
    """(values {code: value}, sources {code: звідки}, missing [row + hint]) для шаблону rows.
    given — {code: value}, які людина вже дописала (мають пріоритет)."""
    given = {k: _value_text(v) for k, v in (given or {}).items() if _value_text(v)}
    stock = _stock_attrs(product)
    values, sources, missing = {}, {}, []
    for row in rows:
        code, value, source = row["code"], None, ""
        if code in given:
            value, source = given[code], "вписано вручну"
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
    category_type = kind if kind in ("filter", "antenna", "cable") else "other"
    listed = DigiKeyListing.objects.exclude(dk_category_id="")
    for label, qs in (("як у товарів тієї ж категорії складу", listed.filter(product__category=product.category)),
                      ("як у товарів того ж виду", listed.filter(category_type=category_type))):
        row = (qs.values("dk_category_id", "dk_category_name", "category_type").annotate(n=Count("pk"))
               .order_by("-n").first())
        if row:
            return {"dk_category_id": row["dk_category_id"], "dk_category_name": row["dk_category_name"] or "",
                    "category_type": row["category_type"] or category_type, "reason": label}
    return {"dk_category_id": "", "dk_category_name": "", "category_type": category_type,
            "reason": "схожих товарів на DigiKey ще немає — вкажіть категорію"}


def _prices(product, moq=1) -> tuple:
    """([{qty, price}], примітка) за правилами «💲 Ціни DigiKey ← Minerva»; без цін — ([], причина)."""
    from bots.services import dk_prices
    rules = dk_prices.Rules.from_settings()
    home = dk_prices.home_currency()
    minerva, cur, _src = dk_prices.minerva_tiers(product, rules.source, home)
    if not minerva:
        return [], "немає цін на складі (базових або ціни продажу) — вкажіть ціни в картці лістингу"
    dk_cur = dk_prices.currency()
    rate = dk_prices.fx(cur, dk_cur)
    if rate is None:
        return [], f"немає курсу {cur} → {dk_cur} («Налаштування цін магазину → Курси валют»)"
    purchase_rate = dk_prices.fx(home, dk_cur) if product.purchase_price else None
    new, notes = dk_prices.compute(minerva, None, rules, rate, moq, product.purchase_price, purchase_rate)
    if not new:
        return [], "; ".join(notes)
    return dk_prices.tiers_json(new), "; ".join(notes)


def build_draft(product, given=None, overrides=None) -> dict:
    """Чернетка картки DigiKey з товару складу: {fields, attrs, sources, missing, notes, category, rows}."""
    from bots.models import DigiKeyListing
    overrides = {k: v for k, v in (overrides or {}).items() if str(v or "").strip()}
    notes = []
    cat = guess_category(product)
    if overrides.get("dk_category_id"):
        name = dict((c, n) for c, n, _ in known_categories()).get(overrides["dk_category_id"], "")
        cat.update(dk_category_id=overrides["dk_category_id"].strip(), dk_category_name=name, reason="вказано вручну")
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
    prices, price_note = _prices(product)
    if price_note:
        notes.append(price_note)
    rows = template(cat["dk_category_id"])
    values, sources, missing = autofill(product, rows, given)
    image = _public(product.image_display_url)
    datasheet = _public(product.datasheet_display_url)
    fields = {
        "dk_supplier_sku": product.sku[:SKU_MAX], "dk_title": title, "dk_description": description,
        "dk_manufacturer": manufacturer[:MANUFACTURER_MAX], "dk_category_id": cat["dk_category_id"],
        "dk_category_name": cat["dk_category_name"], "category_type": cat["category_type"],
        "dk_image_url": image if len(image) <= 200 else "", "dk_datasheet_url": datasheet if len(datasheet) <= 200 else "",
        "dk_prices": prices, "dk_min_order_qty": 1,
        "dk_lead_time_days": product.lead_time_days or DigiKeyListing._meta.get_field("dk_lead_time_days").default,
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
            "category": cat, "rows": rows}


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
