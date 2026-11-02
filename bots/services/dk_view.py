"""
bots/services/dk_view.py — «🔍 Що бачить DigiKey»: що зараз справді лежить на DigiKey проти того, що Minerva
відправила б. Лише читання: GET /offers і GET /products (Marketplace API), на DigiKey нічого не змінюється.

Офер (Offers v1) — назва, артикул, ціни, залишок, MOQ, строк відвантаження, активний (ті самі поля, що
`dk_marketplace._offer_payload`); товар (Products v1) — категорія, опис, виробник, фото, параметри
(`get_all_attributes_api`). Рядок: same — збігається / diff — різниця / missing — на DigiKey немає /
unknown — DigiKey цього через API не віддає (числові параметри категорії: GET /products повертає лише службові
коди, а на сайті DigiKey вони є). Один лістинг — `live` (офер і товар точково); багато — `bulk` (лише офери).

Сторінка одного лістингу має два види (перемикач на сторінці): «🛒 Як бачить покупець» — `buyer`: картка товару,
як на digikey.com (фото, номери, опис, наявність, ціни за кількістю з сумою, даташит, параметри — з DigiKey, чого
API не віддає — з Minerva з позначкою), і «🔧 Усі поля» — порівняння поле за полем (ціни — `price_table` за
кожною кількістю) і `hidden` — службові поля, яких покупець не бачить (ID, стани, дати, налаштування Minerva).

Оновити лише вибране: `push_offer_fields` — свіжий офер з DigiKey (`dk_prices._fetch_one`) → PUT з усім, як на
DigiKey, і лише вибраними полями (`PUSHABLE`) з Minerva; `push_product` — товар цілком (`upsert_product`), DigiKey
перевіряє зміни окремо (Products → Pending, `dk_publish.note_update_sent`).
"""
import logging
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

PRODUCT_ROWS = [
    ("categoryId", "Категорія DigiKey", lambda l: l.dk_category_id, "categoryId"),
    ("description", "Опис", lambda l: l.dk_description, "description"),
    ("manufacturer", "Виробник", lambda l: l.dk_manufacturer, "manufacturer"),
    ("imageUrl", "Фото (адреса)", lambda l: l.dk_image_url, "imageUrl"),
]
SERVICE_CODES = ("packaging", "productLifecycleStatus")
SERVICE_NAMES = {"packaging": "Packaging", "productLifecycleStatus": "Product Status"}
CURRENCY_SIGNS = {"USD": "$", "EUR": "€", "GBP": "£"}
# поля офера, які можна оновити вибірково (артикул — ні: за ним DigiKey знаходить офер)
PUSHABLE = {"title": "назва", "prices": "ціни", "quantityAvailable": "залишок", "minOrderQuantity": "MOQ",
            "leadTimeToShip": "строк відвантаження", "isActive": "активний"}


def _txt(value) -> str:
    return " ".join(str(value if value is not None else "").split())


def _num(value):
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _tiers(rows):
    from bots.services.dk_prices import tiers_of
    return tiers_of(rows)


def _fmt_tiers(tiers) -> str:
    from bots.services.dk_prices import fmt_tiers
    return fmt_tiers(tiers) if tiers else "—"


def attr_names(listing) -> dict:
    """{код: назва} параметрів DigiKey без мережі: фільтри лістингу, службові, поля товару (кеш Custom v1),
    параметри категорії (кеш ProductSearch)."""
    from bots.services import dk_from_stock as fs
    names = {**fs.FILTER_NAMES, **SERVICE_NAMES}
    cache = fs._cache()
    for code, d in (cache.get(fs.FIELDS_CACHE_KEY) or {}).items():
        if isinstance(d, dict) and d.get("name"):
            names[str(code)] = str(d["name"])
    if listing.dk_category_id:
        for prm in cache.get(fs.PARAMS_KEY.format(listing.dk_category_id)) or []:
            if isinstance(prm, dict) and prm.get("code") and prm.get("name"):
                names[str(prm["code"])] = str(prm["name"])
    return names


def price_table(m_tiers, d_tiers) -> list:
    """Ціни поруч за кожною кількістю, що є хоч з одного боку: [{qty, minerva, dk, same}]."""
    from bots.services.dk_prices import fmt_price
    m, d = dict(m_tiers or []), dict(d_tiers or [])
    return [{"qty": q, "minerva": fmt_price(m[q]) if q in m else "—", "dk": fmt_price(d[q]) if q in d else "—",
             "same": q in m and q in d and m[q] == d[q]} for q in sorted(set(m) | set(d))]


def money(value, cur: str) -> str:
    """279 → «$279.00» (USD / EUR / GBP — знаком, інша валюта — кодом)."""
    text = f"{Decimal(str(value)):,.2f}"
    sign = CURRENCY_SIGNS.get(cur)
    return f"{sign}{text}" if sign else f"{text} {cur}"


def _http(url) -> str:
    url = _txt(url)
    return url if url.startswith(("https://", "http://")) else ""


def _row(key, label, minerva, dk, same, show_m=None, show_d=None, group="offer"):
    state = "missing" if dk is None else ("same" if same else "diff")
    return {"key": key, "label": label, "group": group, "state": state,
            "pushable": group == "offer" and key in PUSHABLE and state == "diff",
            "minerva": show_m if show_m is not None else ("—" if minerva in (None, "") else minerva),
            "dk": "— немає на DigiKey —" if dk is None else (show_d if show_d is not None else (dk if dk != "" else "—"))}


def offer_rows(listing, offer) -> list:
    """Поля офера: що відправила б Minerva (як `_offer_payload`) і що на DigiKey. Назва — без DigiKey Part
    Number («3228-…-ND» DigiKey присвоює сам, `dk_marketplace.offer_title`)."""
    from bots.services.dk_marketplace import offer_title
    o = offer or {}
    have = offer is not None
    m_tiers, d_tiers = _tiers(listing.dk_prices), _tiers(o.get("prices")) if have else None
    m_qty = listing.get_dk_quantity()
    rows = [
        _row("title", "Назва", offer_title(listing.dk_title), _txt(o.get("title")) if have else None,
             offer_title(listing.dk_title).lower() == offer_title(o.get("title")).lower()),
        _row("supplierSku", "Артикул (Supplier SKU)", listing.get_supplier_sku(),
             _txt(o.get("supplierSku")) if have else None,
             _txt(listing.get_supplier_sku()).lower() == _txt(o.get("supplierSku")).lower()),
        dict(_row("prices", "Ціни за 1 шт.", m_tiers, d_tiers, have and m_tiers == d_tiers,
                  show_m=_fmt_tiers(m_tiers), show_d=_fmt_tiers(d_tiers) if have else None),
             tiers=price_table(m_tiers, d_tiers if have else []), currency=_currency()),
        _row("quantityAvailable", "Залишок, шт", m_qty, _num(o.get("quantityAvailable")) if have else None,
             _num(o.get("quantityAvailable")) == m_qty),
        _row("minOrderQuantity", "MOQ", max(1, listing.dk_min_order_qty or 1),
             _num(o.get("minOrderQuantity")) if have else None,
             _num(o.get("minOrderQuantity")) == max(1, listing.dk_min_order_qty or 1)),
        _row("leadTimeToShip", "Строк відвантаження, дн", listing.dk_lead_time_days,
             _num(o.get("leadTimeToShip")) if have else None,
             _num(o.get("leadTimeToShip")) == listing.dk_lead_time_days),
        _row("isActive", "Активний", "так" if listing.dk_is_active else "ні",
             ("так" if o.get("isActive") else "ні") if have else None,
             bool(o.get("isActive")) == bool(listing.dk_is_active)),
    ]
    return rows


def _currency() -> str:
    from bots.services.dk_prices import currency
    return currency()


def product_rows(listing, product) -> list:
    """Поля товару (категорія, опис, виробник, фото — з мініатюрами) і параметри additionalFields з назвами."""
    from bots.services.dk_marketplace import _build_attributes_dict
    p = product or {}
    have = product is not None
    rows = []
    from bots.services.dk_media import dk_hosted
    for key, label, minerva_fn, dk_key in PRODUCT_ROWS:
        m, d = _txt(minerva_fn(listing)), _txt(p.get(dk_key)) if have else None
        if key == "imageUrl" and have and d and m != d and dk_hosted(d):
            # DigiKey показує свою копію фото — адреса інша завжди; фото йде лише з «🖼 Оновити фото на DigiKey»
            row = _row(key, label, m, d, True, group="product", show_d=f"{d} — копія DigiKey")
        else:
            row = _row(key, label, m, d, have and m == d, group="product")
        if key == "imageUrl":
            row.update(img_m=_http(m), img_d=_http(d) if have else "")
        rows.append(row)
    names = attr_names(listing)
    mine = {str(a["code"]): _txt(a.get("value")) for a in listing.get_all_attributes_api()}
    theirs = {str(k): _txt(v) for k, v in _build_attributes_dict(p.get("additionalFields") or []).items()} \
        if have else {}
    numeric_back = any(c.isdigit() for c in theirs)       # API віддає числові параметри? (зазвичай — ні)
    for code in sorted(set(mine) | (set(theirs) & (set(mine) | set(SERVICE_CODES))), key=lambda c: (not c.isdigit(), c)):
        m, d = mine.get(code, ""), theirs.get(code) if have else None
        if have and code.isdigit() and not numeric_back:
            row = _row(f"attr:{code}", _attr_label(code, names), m, "", True, group="attrs")
            row.update(state="unknown", dk="API не віддає — дивіться сторінку товару на digikey.com")
            rows.append(row)
            continue
        if have and code not in theirs:
            d = ""
        rows.append(_row(f"attr:{code}", _attr_label(code, names), m, d, have and m.lower() == (d or "").lower(),
                         group="attrs"))
    return rows


def _attr_label(code, names) -> str:
    name = names.get(code)
    return f"{name} ({code})" if name and name != code else f"Параметр {code}"


def buyer(listing, offer, product) -> dict:
    """Картка товару, як її бачить покупець на DigiKey: фото, номери, назва й опис, наявність, строк, MOQ, ціни
    за кількістю (ціна × кількість), даташит, параметри. Дані — з DigiKey; чого DigiKey не віддає через API
    (числові параметри) або товару там ще немає — з Minerva, з позначкою `from_minerva`."""
    from bots.services.dk_marketplace import _DK_PN_RE, _build_attributes_dict, offer_title
    from bots.services.dk_media import DATASHEET, _is_sheet, on_digikey
    from bots.services.dk_prices import fmt_price
    o, p = offer or {}, product or {}
    cur = _currency()
    tiers = _tiers(o.get("prices")) if offer else _tiers(listing.dk_prices)
    dk_pn = next((t for t in (_txt(p.get("title")), _txt(o.get("title")), _txt(listing.dk_title)) if _DK_PN_RE.match(t)), "")
    names = attr_names(listing)
    theirs = {str(k): _txt(v) for k, v in _build_attributes_dict(p.get("additionalFields") or []).items()}
    mine = {str(a["code"]): _txt(a.get("value")) for a in listing.get_all_attributes_api()}
    attrs = []
    if listing.dk_category_name:
        attrs.append({"name": "Category", "value": listing.dk_category_name, "from_minerva": False})
    manufacturer = _txt(p.get("manufacturer")) or _txt(listing.dk_manufacturer)
    if manufacturer:
        attrs.append({"name": "Mfr", "value": manufacturer, "from_minerva": not p.get("manufacturer")})
    rest = []
    for code in set(mine) | set(theirs):
        value = theirs.get(code) or mine.get(code)
        if not value or _is_sheet(code, names.get(code, "")):
            continue
        rest.append({"name": names.get(code) or code, "value": value, "from_minerva": not theirs.get(code),
                     "service": code in SERVICE_CODES})
    rest.sort(key=lambda a: (not a["service"], a["name"].lower()))
    image = _http(p.get("imageUrl")) or _http(listing.dk_image_on_dk) or _http(listing.dk_image_url)
    image_from = ("dk" if image == _http(p.get("imageUrl")) else "seen" if image == _http(listing.dk_image_on_dk)
                  else "card") if image else ""
    sheet = _http(on_digikey(product).get(DATASHEET) if product else "") or _http(listing.dk_datasheet_url)
    return {
        "live": bool(offer or product), "dk_pn": dk_pn,
        "url": f"https://www.digikey.com/en/products/detail/-/-/{dk_pn}" if dk_pn else "",
        "image": image, "image_from": image_from, "image_dk": image_from == "dk",
        "mpn": _txt(p.get("partNumber")) or listing.get_supplier_sku(),
        "manufacturer": manufacturer,
        "title": offer_title(o.get("title")) if offer else offer_title(listing.dk_title),
        "description": _txt(p.get("description")) or _txt(listing.dk_description),
        "description_dk": bool(p.get("description")),
        "datasheet": sheet,
        "stock": _num(o.get("quantityAvailable")) if offer else listing.get_dk_quantity(),
        "moq": (_num(o.get("minOrderQuantity")) if offer else listing.dk_min_order_qty) or 1,
        "lead": _num(o.get("leadTimeToShip")) if offer else listing.dk_lead_time_days,
        "active": bool(o.get("isActive")) if offer else listing.dk_is_active,
        "web": p.get("webDisplay") if product else None,
        "currency": cur,
        "prices": [{"qty": q, "unit": fmt_price(pr), "ext": money(q * pr, cur), "sign": CURRENCY_SIGNS.get(cur, "")}
                   for q, pr in tiers],
        "attrs": attrs + rest,
    }


def hidden(listing, offer, product) -> list:
    """Службові поля — покупець на DigiKey їх не бачить: [(назва, значення)]."""
    o, p = offer or {}, product or {}
    yes = lambda v: "так" if v else "ні"   # noqa: E731
    rows = [
        ("Offer ID (DigiKey)", _txt(o.get("id")) or listing.dk_offer_id or "—"),
        ("Product ID (DigiKey)", _txt(p.get("_id")) or listing.dk_product_id or "—"),
        ("Supplier SKU", listing.get_supplier_sku()),
        ("Category ID", listing.dk_category_id or "—"),
        ("Офер активний", yes(o.get("isActive")) if offer else "офера не прочитано"),
        ("Стан офера", _txt(o.get("offerStatusReason")) or "—"),
        ("Офер змінено", " ".join(filter(None, [_txt(o.get("lastUpdateDateUtc"))[:16].replace("T", " "),
                                                 f"({o['lastUpdatedSource']})" if o.get("lastUpdatedSource") else ""])) or "—"),
        ("Товар показується на сайті", yes(p.get("webDisplay")) if product else "товару не прочитано"),
        ("Товар активний", yes(p.get("isActive")) if product else "—"),
        ("Товар створено / затверджено", " / ".join(_txt(p.get(k))[:10] for k in ("createDateUtc", "lastUpdateDateUtc")
                                                    if p.get(k)) or "—"),
        ("Склад Minerva, шт", listing.get_stock_qty()),
        ("Залишок для DigiKey вручну", listing.dk_quantity_override if listing.dk_quantity_override is not None else "—"),
        ("Сповіщати, коли залишок ≤", listing.dk_qty_alert),
        ("Ціни — автооновлення з Minerva", yes(listing.price_auto)),
        ("Свої поправки ціни", " ".join(filter(None, [f"{listing.price_adjust_pct:+} %" if listing.price_adjust_pct is not None else "",
                                                       f"{listing.price_adjust_fixed:+}" if listing.price_adjust_fixed is not None else ""])) or "—"),
        ("Звідки картка", listing.get_card_source_display() or "—"),
        ("Статус у Minerva", listing.get_sync_status_display()),
        ("Остання синхронізація", listing.last_synced_at.strftime("%d.%m.%Y %H:%M") if listing.last_synced_at else "—"),
        ("Остання помилка", (listing.last_error or "—")[:300]),
    ]
    return [(label, value) for label, value in rows]


def summary(rows) -> dict:
    return {"diff": sum(1 for r in rows if r["state"] == "diff"),
            "missing": sum(1 for r in rows if r["state"] == "missing"),
            "same": sum(1 for r in rows if r["state"] == "same"),
            "unknown": sum(1 for r in rows if r["state"] == "unknown"),
            "pushable": sum(1 for r in rows if r.get("pushable"))}


def live(listing) -> dict:
    """Один лістинг: свіжий офер і товар з DigiKey + порівняння. Збій мережі — у errors, сторінка відкривається."""
    from bots.models import DigiKeyConfig
    from bots.services.dk_marketplace import fetch_product_by_sku
    from bots.services.dk_prices import _fetch_one
    errors, offer, product = [], None, None
    try:
        offer = _fetch_one(listing)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"офер: {exc}")
    try:
        product = fetch_product_by_sku(DigiKeyConfig.get(), listing.get_supplier_sku(),
                                       product_id=listing.dk_product_id or "")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"товар: {exc}")
    if product:
        from bots.services.dk_publish import note_product
        note_product(listing, product)                  # повторно надіслані зміни DigiKey уже прийняв?
    o_rows, p_rows = offer_rows(listing, offer), product_rows(listing, product)
    return {"listing": listing, "offer": offer, "product": product, "errors": errors,
            "buyer": buyer(listing, offer, product), "hidden": hidden(listing, offer, product),
            "offer_rows": o_rows, "product_rows": [r for r in p_rows if r["group"] == "product"],
            "attr_rows": [r for r in p_rows if r["group"] == "attrs"],
            "offer_sum": summary(o_rows), "product_sum": summary(p_rows),
            "updated": (offer or {}).get("lastUpdateDateUtc") or "", "source": (offer or {}).get("lastUpdatedSource") or "",
            "status": (offer or {}).get("offerStatusReason") or "",
            "web": (product or {}).get("webDisplay"), "product_active": (product or {}).get("isActive"),
            "product_created": (product or {}).get("createDateUtc") or "",
            "product_updated": (product or {}).get("lastUpdateDateUtc") or "", "pending": _pending(listing)}


def _pending(listing):
    from bots.services.dk_publish import pending_update
    return pending_update(listing.pub_events)


def push_offer_fields(listing, fields) -> list:
    """Оновити на DigiKey лише вибрані поля офера (решта — як на DigiKey зараз). Повертає оновлені поля."""
    from bots.services.dk_marketplace import DKMarketplaceError, _offer_payload
    from bots.services.dk_prices import _fetch_one, _put_offer, offer_payload, tiers_of
    fields = [f for f in fields if f in PUSHABLE]
    if not fields:
        raise DKMarketplaceError("Не вибрано, що оновлювати.")
    offer = _fetch_one(listing)
    if not offer or not offer.get("id"):
        raise DKMarketplaceError("Офер на DigiKey не прочитано — оновити вибіркою не можна.")
    body = offer_payload(offer, tiers_of(offer.get("prices")), listing)
    mine = _offer_payload(listing)
    for f in fields:
        body[f] = mine[f]
    _put_offer(offer["id"], body)
    if "prices" in fields:                                  # локальна копія цін DigiKey — як тепер на DigiKey
        listing.dk_prices = [{"qty": int(p["quantityBreak"]), "price": p["price"]} for p in mine["prices"]]
        listing.save(update_fields=["dk_prices"])
    return fields


def push_product(listing) -> str:
    """Надіслати товар (опис, категорію, виробника, фото, параметри) — DigiKey перевіряє зміни окремо."""
    from bots.models import DigiKeyConfig
    from bots.services.dk_marketplace import upsert_product
    from bots.services.dk_publish import mark, note_update_sent, was_published
    published = was_published(listing)
    staged = upsert_product(DigiKeyConfig.get(), listing)
    mark(listing, "sent", staged_id=staged)
    if published:
        note_update_sent(listing, staged)
    return staged


def bulk(listings) -> list:
    """Багато лістингів — лише офери (один GET /offers на всіх): [{listing, offer, rows, sum}]."""
    from bots.services.dk_prices import _match, _offers_for, offer_for
    listings = list(listings)
    maps = _match(_offers_for(listings))
    out = []
    for listing in listings:
        offer = offer_for(listing, maps)
        rows = offer_rows(listing, offer)
        out.append({"listing": listing, "offer": offer, "rows": rows, "sum": summary(rows),
                    "diff_keys": [r["label"] for r in rows if r["state"] == "diff"]})
    return out
