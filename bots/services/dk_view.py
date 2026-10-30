"""
bots/services/dk_view.py — «🔍 Що бачить DigiKey»: що зараз справді лежить на DigiKey проти того, що Minerva
відправила б. Лише читання: GET /offers і GET /products (Marketplace API), на DigiKey нічого не змінюється.

Офер (Offers v1) — назва, артикул, ціни, залишок, MOQ, строк відвантаження, активний (ті самі поля, що
`dk_marketplace._offer_payload`); товар (Products v1) — категорія, опис, виробник, фото, параметри
(`get_all_attributes_api`). Рядок: same — збігається / diff — різниця / missing — на DigiKey немає.
Один лістинг — `live` (офер і товар точково); багато — `bulk` (лише офери, один список).
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


def _row(key, label, minerva, dk, same, show_m=None, show_d=None, group="offer"):
    state = "missing" if dk is None else ("same" if same else "diff")
    return {"key": key, "label": label, "group": group, "state": state,
            "minerva": show_m if show_m is not None else ("—" if minerva in (None, "") else minerva),
            "dk": "— немає на DigiKey —" if dk is None else (show_d if show_d is not None else (dk if dk != "" else "—"))}


def offer_rows(listing, offer) -> list:
    """Поля офера: що відправила б Minerva (як `_offer_payload`) і що на DigiKey."""
    o = offer or {}
    have = offer is not None
    m_tiers, d_tiers = _tiers(listing.dk_prices), _tiers(o.get("prices")) if have else None
    m_qty = listing.get_dk_quantity()
    rows = [
        _row("title", "Назва", _txt(listing.dk_title), _txt(o.get("title")) if have else None,
             _txt(listing.dk_title) == _txt(o.get("title"))),
        _row("supplierSku", "Артикул (Supplier SKU)", listing.get_supplier_sku(),
             _txt(o.get("supplierSku")) if have else None,
             _txt(listing.get_supplier_sku()).lower() == _txt(o.get("supplierSku")).lower()),
        _row("prices", "Ціни", m_tiers, d_tiers, have and m_tiers == d_tiers,
             show_m=_fmt_tiers(m_tiers), show_d=_fmt_tiers(d_tiers) if have else None),
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


def product_rows(listing, product) -> list:
    """Поля товару (категорія, опис, виробник, фото) і параметри additionalFields."""
    from bots.services.dk_marketplace import _build_attributes_dict
    p = product or {}
    have = product is not None
    rows = []
    for key, label, minerva_fn, dk_key in PRODUCT_ROWS:
        m, d = _txt(minerva_fn(listing)), _txt(p.get(dk_key)) if have else None
        rows.append(_row(key, label, m, d, have and m == d, group="product"))
    mine = {str(a["code"]): _txt(a.get("value")) for a in listing.get_all_attributes_api()}
    theirs = {str(k): _txt(v) for k, v in _build_attributes_dict(p.get("additionalFields") or []).items()} \
        if have else {}
    for code in sorted(set(mine) | (set(theirs) & (set(mine) | set(SERVICE_CODES))), key=lambda c: (not c.isdigit(), c)):
        m, d = mine.get(code, ""), theirs.get(code) if have else None
        if have and code not in theirs:
            d = ""
        rows.append(_row(f"attr:{code}", f"Параметр {code}", m, d, have and m.lower() == (d or "").lower(),
                         group="attrs"))
    return rows


def summary(rows) -> dict:
    return {"diff": sum(1 for r in rows if r["state"] == "diff"),
            "missing": sum(1 for r in rows if r["state"] == "missing"),
            "same": sum(1 for r in rows if r["state"] == "same")}


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
    o_rows, p_rows = offer_rows(listing, offer), product_rows(listing, product)
    return {"listing": listing, "offer": offer, "product": product, "errors": errors,
            "offer_rows": o_rows, "product_rows": [r for r in p_rows if r["group"] == "product"],
            "attr_rows": [r for r in p_rows if r["group"] == "attrs"],
            "offer_sum": summary(o_rows), "product_sum": summary(p_rows),
            "updated": (offer or {}).get("lastUpdateDateUtc") or "", "source": (offer or {}).get("lastUpdatedSource") or "",
            "status": (offer or {}).get("offerStatusReason") or "",
            "web": (product or {}).get("webDisplay"), "product_active": (product or {}).get("isActive")}


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
