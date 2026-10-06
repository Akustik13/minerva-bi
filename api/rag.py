"""Документи товарів для RAG / пошуку: текст (markdown) для ембедингів + метадані для фільтрації.

GET /api/v1/rag/products/ — див. RagProductViewSet у views.py, опис в api/README.md (розділ «RAG»).
"""
from __future__ import annotations

import re

from .serializers import split_attributes

# Значення DigiKey з кількома варіантами пишуться через кому: «5G, Bluetooth, ISM»
_SPLIT = re.compile(r",\s+")

LABELS = {
    "en": {"category": "Category", "manufacturer": "Manufacturer", "status": "Lifecycle", "successor": "Recommended successor",
           "params": "Technical parameters", "codes": "Compliance / codes", "stock": "Availability",
           "in_stock": "In stock: {n} pcs", "out": "Not in stock", "lead": "lead time {n} days",
           "prices": "Base prices ({cur}, per piece)", "from": "from {n} pcs", "datasheet": "Datasheet",
           "export_name": "Export name",
           "life": {"active": "active", "nrnd": "not recommended for new designs (NRND)",
                    "discontinued": "discontinued (EOL)"}},
    "uk": {"category": "Категорія", "manufacturer": "Виробник", "status": "Життєвий цикл", "successor": "Рекомендована заміна",
           "params": "Технічні параметри", "codes": "Коди / відповідність", "stock": "Наявність",
           "in_stock": "На складі: {n} шт.", "out": "Немає на складі", "lead": "термін поставки {n} дн.",
           "prices": "Базові ціни ({cur}, за шт.)", "from": "від {n} шт.", "datasheet": "Datasheet",
           "export_name": "Назва для документів",
           "life": {"active": "активний", "nrnd": "не рекомендовано для нових розробок (NRND)",
                    "discontinued": "знято з виробництва (EOL)"}},
}


def split_value(value) -> list[str]:
    """«5G, Bluetooth, ISM» → ['5G', 'Bluetooth', 'ISM']; списки — як є."""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in _SPLIT.split(str(value)) if v.strip()]


def attribute_lists(attrs) -> dict[str, list[str]]:
    """Технічні параметри у вигляді списків значень (для метаданих і фільтрів)."""
    params, _ = split_attributes(attrs)
    return {k: split_value(v) for k, v in params.items()}


def _num(v) -> str:
    return f"{v:g}" if isinstance(v, float) else format(v.normalize(), "f") if hasattr(v, "normalize") else str(v)


def build_document(p, *, lang: str = "en", with_prices: bool = False, categories: dict | None = None,
                   datasheet_url: str | None = None, image_url: str | None = None) -> dict:
    """Один документ на товар: {id, sku, title, text, metadata, updated_at}."""
    L = LABELS.get(lang, LABELS["en"])
    params, codes = split_attributes(p.tech_attributes)
    cat_name = (categories or {}).get(p.category, p.category)
    available = getattr(p, "_available", None)
    title = p.name or p.name_export or p.sku

    lines = [f"# {p.sku} — {title}", ""]
    facts = [f"{L['category']}: {cat_name}"]
    if p.manufacturer:
        facts.append(f"{L['manufacturer']}: {p.manufacturer}")
    facts.append(f"{L['status']}: {L['life'].get(p.lifecycle_status, p.lifecycle_status)}")
    lines.append(" · ".join(facts))
    if p.name_export and p.name_export != title:
        lines.append(f"{L['export_name']}: {p.name_export}")
    if p.successor_id and p.successor:
        lines.append(f"{L['successor']}: {p.successor.sku} — {p.successor.name or p.successor.sku}")
    if params:
        lines += ["", f"## {L['params']}"] + [f"- {k}: {v}" for k, v in sorted(params.items())]
    if available is not None or p.lead_time_days:
        stock = L["in_stock"].format(n=_num(available)) if available and available > 0 else L["out"]
        if p.lead_time_days:
            stock += "; " + L["lead"].format(n=p.lead_time_days)
        lines += ["", f"## {L['stock']}", stock]
    prices = []
    if with_prices:
        from inventory.services.base_prices import normalize
        prices = normalize(p.base_prices)
        if prices:
            lines += ["", f"## {L['prices'].format(cur=p.base_price_currency)}"]
            lines += [f"- {L['from'].format(n=r['min_qty'])}: {r['unit_price']}" for r in prices]
    if codes:
        lines += ["", f"## {L['codes']}"] + [f"- {k}: {v}" for k, v in sorted(codes.items()) if k != "datasheetUrl"]
    ds = datasheet_url or codes.get("datasheetUrl")
    if ds:
        lines += ["", f"{L['datasheet']}: {ds}"]

    meta = {
        "sku": p.sku,
        "category": p.category,
        "category_name": cat_name,
        "manufacturer": p.manufacturer or None,
        "lifecycle_status": p.lifecycle_status,
        "successor_sku": p.successor.sku if p.successor_id and p.successor else None,
        "is_active": p.is_active,
        "in_stock": bool(available and available > 0),
        "available": float(available) if available is not None else None,
        "lead_time_days": p.lead_time_days,
        "attributes": attribute_lists(p.tech_attributes),
        "datasheet_url": ds or None,
        "image_url": image_url or None,
    }
    if with_prices:
        meta["base_price_currency"] = p.base_price_currency if prices else None
        meta["base_price_from"] = float(prices[-1]["unit_price"]) if prices else None   # найнижча (від найбільшої к-сті)
        meta["base_price_1pc"] = float(prices[0]["unit_price"]) if prices else None
    return {
        "id": f"product:{p.sku}",
        "sku": p.sku,
        "title": title,
        "text": "\n".join(lines).strip() + "\n",
        "metadata": meta,
        "updated_at": p.updated_at,
    }


def attribute_facets(products) -> list[dict]:
    """[{name, products, values: [{value, count}]}] — які параметри і значення є серед товарів."""
    facets: dict[str, dict] = {}
    for p in products:
        for k, values in attribute_lists(p.tech_attributes).items():
            f = facets.setdefault(k, {"name": k, "products": 0, "values": {}})
            f["products"] += 1
            for v in set(values):
                f["values"][v] = f["values"].get(v, 0) + 1
    out = []
    for f in sorted(facets.values(), key=lambda x: (-x["products"], x["name"])):
        vals = sorted(f["values"].items(), key=lambda kv: (-kv[1], kv[0]))
        out.append({"name": f["name"], "products": f["products"],
                    "values": [{"value": v, "count": c} for v, c in vals]})
    return out
