"""🤖 AI-заповнення картки DigiKey Marketplace (ціль `digikey` для inventory/services/ai_fill.py).

Claude отримує даташит / фото / текст / посилання (і дані складу, якщо товар є) і заповнює картку DigiKey:
назву ≤ 50, опис ≤ 2048, виробника ≤ 50, категорію-листок каталогу DigiKey, пакування й життєвий цикл, посилання,
параметри категорії числовими кодами. Інструменти — лише читання (без мережі, з кешу каталогу):
  • `dk_find_category` — пошук у каталозі DigiKey (`dk_from_stock.dk_categories`) + наші категорії лістингів;
  • `dk_category_fields` — шаблон категорії (`dk_from_stock.template`: обов'язкові поля DigiKey, параметри
    категорії з найчастішими значеннями, як у наших лістингів);
  • `similar_listings` — наші лістинги з такими словами (стиль назви, опису, параметрів).
`clean` перевіряє все ще раз: категорія — з каталогу чи наших лістингів, коди — лише з шаблону категорії,
значення зі списку DigiKey — лише зі списку (`dk_from_stock._pick`), довжини — як у специфікації DigiKey.
"""
import re

from inventory.services.ai_fill import FillError, Target, _conf, _dedupe, _items, _src, product_summary, submit_tool

FIELDS = {
    "dk_title":            ("Назва (DK)", "title"),
    "dk_description":      ("Опис товару (DK)", "desc"),
    "dk_manufacturer":     ("Виробник (DK)", "mfr"),
    "dk_category_id":      ("Категорія DigiKey", "category"),
    "category_type":       ("Вид (фільтр / антена / кабель…)", "kind"),
    "dk_packaging":        ("Packaging", "packaging"),
    "dk_lifecycle_status": ("Product Life Cycle Status", "lifecycle"),
    "dk_datasheet_url":    ("Datasheet (URL)", "url"),
    "dk_image_url":        ("Фото (URL)", "url"),
    "dk_supplier_sku":     ("Supplier SKU (DK)", "sku"),
}
LIFECYCLE_VALUES = ["Active", "Not For New Designs", "Last Time Buy", "Obsolete"]
PACKAGING_VALUES = ["Bulk", "Tape & Reel (TR)", "Cut Tape (CT)", "Digi-Reel®", "Tray", "Tube", "Box", "Bag"]
URL_MAX, VALUE_MAX = 200, 255
FIND_MAX, ROWS_MAX, VALUES_MAX = 25, 60, 30


def _svc():
    from bots.services import dk_from_stock
    return dk_from_stock


def _known() -> dict:
    """{id: (назва, лістингів)} — наші категорії лістингів."""
    return {cid: (name, n) for cid, name, n in _svc().known_categories()}


def _catalog() -> dict:
    try:
        cats = _svc().dk_categories()
    except Exception:  # noqa: BLE001 — немає ProductSearch / мережі — лише наші категорії
        cats = []
    return {c["id"]: c for c in cats}


def _values(code) -> list:
    """Допустимі значення поля DigiKey (packaging / productLifecycleStatus) з отриманого списку полів."""
    d = (_svc().field_defs() or {}).get(code) or {}
    return list(d.get("values") or [])


def find_category(query) -> str:
    words = [w for w in re.split(r"[\s/,]+", str(query or "").lower()) if len(w) >= 2][:6]
    catalog, known = _catalog(), _known()
    if not catalog:
        if not known:
            return "Каталог DigiKey недоступний, наших лістингів з категорією ще немає."
        return ("Каталог DigiKey недоступний (ProductSearch). Наші категорії лістингів:\n"
                + "\n".join(f"ID {cid} | {name} | наших лістингів {n}" for cid, (name, n) in known.items()))
    leaves = [c for c in catalog.values() if c["leaf"]]
    scored = []
    for c in leaves:
        hay = c["path"].lower()
        hits = sum(1 for w in words if w in hay)
        if hits:
            scored.append((-hits, -(known.get(c["id"], ("", 0))[1]), -c["count"], c))
    scored.sort(key=lambda t: t[:3])
    lines = [f"ID {c['id']} | {c['path']} | товарів на DigiKey {c['count']}"
             + (f" | наших лістингів {known[c['id']][1]}" if c["id"] in known else "")
             for *_s, c in scored[:FIND_MAX]]
    ours = [f"ID {cid} | {catalog[cid]['path'] if cid in catalog else name} | наших лістингів {n}"
            for cid, (name, n) in known.items()]
    return ("Категорії-листки каталогу DigiKey:\n" + ("\n".join(lines) or "нічого — спробуй інші слова (англійською)")
            + ("\n\nНаші категорії лістингів:\n" + "\n".join(ours[:15]) if ours else ""))


def category_fields(category_id) -> str:
    cid = str(category_id or "").strip()
    if not cid:
        raise FillError("потрібен category_id")
    if cid not in _catalog() and cid not in _known():
        raise FillError(f"категорії {cid} немає в каталозі DigiKey — знайди через dk_find_category")
    rows = [r for r in _svc().template(cid) if r["code"] not in _svc().COMMON][:ROWS_MAX]
    if not rows:
        return "Параметрів категорії немає (DigiKey не дав, наших лістингів у ній ще немає)."
    out = []
    for r in rows:
        line = f"{r['code']} | {r['name']}" + (" | ОБОВ'ЯЗКОВЕ" if r["required"] else "")
        if r["values"]:
            line += " | лише одне з: " + "; ".join(r["values"][:VALUES_MAX])
        elif r["examples"]:
            line += " | приклади: " + "; ".join(r["examples"][:8])
        out.append(line)
    return "code | назва | … (значення — точно як у списку)\n" + "\n".join(out)


def similar_listings(query, limit=4) -> str:
    from django.db.models import Q

    from bots.models import DigiKeyListing
    words = [w for w in re.split(r"\s+", str(query or "").strip()) if len(w) >= 2][:6]
    if not words:
        return "Порожній запит."
    cond = Q()
    for w in words:
        cond |= (Q(dk_title__icontains=w) | Q(dk_supplier_sku__icontains=w) | Q(dk_category_name__icontains=w)
                 | Q(product__sku__icontains=w) | Q(dk_description__icontains=w))
    found = list(DigiKeyListing.objects.filter(cond).exclude(dk_title="")[:200])
    if not found:
        return "Схожих лістингів немає."
    found.sort(key=lambda lst: -sum(1 for w in words if w.lower() in
                                  f"{lst.dk_title} {lst.dk_supplier_sku} {lst.dk_category_name}".lower()))
    names = {c: d.get("name") or c for c, d in (_svc().field_defs() or {}).items()}
    names.update(_svc().FILTER_NAMES)
    out = []
    for lst in found[:limit]:
        codes = _svc()._listing_codes(lst)
        out.append(f"• {lst.dk_supplier_sku or (lst.product.sku if lst.product_id else '')} | «{lst.dk_title}» | "
                   f"категорія {lst.dk_category_id} {lst.dk_category_name} | виробник {lst.dk_manufacturer or '—'} | "
                   f"пакування {lst.dk_packaging or '—'}\n  опис: {' '.join((lst.dk_description or '').split())[:300]}"
                   "\n  параметри: " + ("; ".join(f"{c} {names.get(c, '')}: {v}" for c, v in list(codes.items())[:20])
                                        or "—"))
    return "\n".join(out)


TOOLS = [
    {"name": "dk_find_category", "description": (
        "Пошук категорії-листка в каталозі DigiKey за словами англійською («RF Filters», «antenna», «coaxial "
        "cable assembly»). Повертає ID, шлях, скільки товарів, чи є наші лістинги в ній."),
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "dk_category_fields", "description": (
        "Параметри категорії DigiKey: код, назва, обов'язкове чи ні, допустимі значення (тоді — точно одне з них) "
        "або приклади з наших лістингів. Коди й значення для attributes бери лише звідси."),
     "input_schema": {"type": "object", "properties": {"category_id": {"type": "string"}},
                      "required": ["category_id"]}},
    {"name": "similar_listings", "description": (
        "Наші лістинги DigiKey з такими словами: назва, опис, категорія, параметри — щоб нова картка була в тому "
        "самому стилі."),
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]


class DigiKeyTarget(Target):
    name = "digikey"
    title = "картка DigiKey Marketplace"

    def __init__(self, product=None, listing=None):
        super().__init__(product)
        self.listing = listing

    def rules(self) -> str:
        svc = _svc()
        packaging = _values("packaging") or PACKAGING_VALUES
        lifecycle = _values("productLifecycleStatus") or LIFECYCLE_VALUES
        return (
            "ЦІЛЬ: картка товару для DigiKey Marketplace (ми — постачальник, продаємо на DigiKey). Поля (key):\n"
            f"- dk_title — назва англійською ≤ {svc.TITLE_MAX} символів: вид, головні параметри, роз'єм — як у "
            "наших лістингів (similar_listings); без назви фірми;\n"
            f"- dk_description — опис англійською ≤ {svc.DESC_MAX} символів: що це, ключові параметри, "
            "застосування; без цін, контактів і посилань;\n"
            f"- dk_manufacturer — виробник ≤ {svc.MANUFACTURER_MAX} символів (наш товар — як у наших лістингів);\n"
            "- dk_category_id — числовий ID категорії-ЛИСТКА каталогу DigiKey: спершу dk_find_category, потім "
            "dk_category_fields для вибраної;\n"
            "- category_type — filter | antenna | cable | connector | other;\n"
            f"- dk_packaging — одне з: {'; '.join(packaging)};\n"
            f"- dk_lifecycle_status — одне з: {'; '.join(lifecycle)};\n"
            f"- dk_datasheet_url / dk_image_url — пряме публічне посилання ≤ {URL_MAX} символів на PDF / фото саме "
            "цього товару (лише знайдене, не вигадуй);\n"
            "- dk_supplier_sku — лише якщо людина дала свій артикул продавця.\n"
            "attributes — параметри категорії: code — лише коди з dk_category_fields; value — точно одне з "
            "допустимих значень, якщо список є, інакше у стилі прикладів («2.4GHz», «50 Ohms», «SMA Male»); "
            "обов'язкові заповни обов'язково, якщо дані є.")

    def context(self) -> str:
        lines = []
        if self.product is not None:
            lines.append("ТОВАР СКЛАДУ:\n" + product_summary(self.product))
        lst = self.listing
        if lst is not None:
            lines.append(f"КАРТКА DIGIKEY ЗАРАЗ: назва «{lst.dk_title}», категорія {lst.dk_category_id} "
                         f"{lst.dk_category_name}, виробник {lst.dk_manufacturer or '—'}, параметри: "
                         + ("; ".join(f"{c}: {v}" for c, v in list((lst.dk_attributes or {}).items())[:40]) or "—"))
        return "\n\n".join(lines)

    def tools(self) -> list:
        return list(TOOLS)

    def handle(self, name, args):
        if name == "dk_find_category":
            return find_category(args.get("query"))
        if name == "dk_category_fields":
            return category_fields(args.get("category_id"))
        if name == "similar_listings":
            return similar_listings(args.get("query"))
        return super().handle(name, args)

    def submit_tool(self) -> dict:
        return submit_tool(list(FIELDS), {"code": {"type": "string"}, "value": {"type": "string"}}, ["code", "value"])

    def clean(self, data) -> dict:
        svc = _svc()
        fields, dropped = [], []
        catalog, known = _catalog(), _known()
        category = None
        for item in _items(data.get("fields")):
            key, value = str(item.get("key") or ""), " ".join(str(item.get("value") or "").split())
            if key not in FIELDS or not value:
                continue
            label, kind = FIELDS[key]
            display, extra = value, None
            if kind == "title":
                value, cut = svc._cut(value, svc.TITLE_MAX)
                display = value + (" (скорочено)" if cut else "")
            elif kind == "desc":
                value, _ = svc._cut(str(item.get("value") or "").strip(), svc.DESC_MAX)
                display = value
            elif kind == "mfr":
                value = display = value[:svc.MANUFACTURER_MAX]
            elif kind == "sku":
                value = display = value[:svc.SKU_MAX]
            elif kind == "category":
                value = value.removeprefix("ID").strip()
                if value not in catalog and value not in known:
                    dropped.append(f"{label}: «{value}» немає в каталозі DigiKey")
                    continue
                name = catalog[value]["name"] if value in catalog else known[value][0]
                path = catalog[value]["path"] if value in catalog else name
                category, display = value, f"{path} · ID {value}"
                extra = {"key": "dk_category_name", "label": "Назва категорії DK", "value": name, "display": name,
                         "source": _src(item), "confidence": _conf(item), "follow": "dk_category_id"}
            elif kind == "kind":
                choices = dict(_cat_choices())
                if value not in choices:
                    dropped.append(f"{label}: «{value}» — немає такого виду")
                    continue
                display = choices[value]
            elif kind in ("packaging", "lifecycle"):
                allowed = _values("packaging" if kind == "packaging" else "productLifecycleStatus") or (
                    PACKAGING_VALUES if kind == "packaging" else LIFECYCLE_VALUES)
                picked = svc._pick(allowed, value)
                if picked is None:
                    dropped.append(f"{label}: «{value}» немає серед значень DigiKey")
                    continue
                value = display = picked
            elif kind == "url":
                if not re.match(r"^https?://\S+$", value) or len(value) > URL_MAX:
                    dropped.append(f"{label}: потрібне посилання http(s)://… до {URL_MAX} символів")
                    continue
            fields.append({"key": key, "label": label, "value": value, "display": display,
                           "source": _src(item), "confidence": _conf(item)})
            if extra:
                fields.append(extra)
        attrs = []
        if category is None and self.listing is not None and self.listing.dk_category_id:
            category = self.listing.dk_category_id
        rows = {r["code"]: r for r in svc.template(category) if r["code"] not in svc.COMMON} if category else {}
        items = _items(data.get("attributes"))
        if items and not rows:
            dropped.append("параметри — без категорії DigiKey їх не перевірити")
        seen = set()
        for item in items:
            code, value = str(item.get("code") or "").strip(), " ".join(str(item.get("value") or "").split())
            if not rows or not value or code in seen:
                continue
            row = rows.get(code)
            if row is None:
                dropped.append(f"параметр {code}: немає в категорії {category}")
                continue
            picked = svc._pick(row["values"], value)
            if picked is None:
                dropped.append(f"{row['name']}: «{value}» немає серед значень DigiKey")
                continue
            seen.add(code)
            attrs.append({"key": code, "label": f"{row['name']} ({code})", "value": picked[:VALUE_MAX],
                          "display": picked[:VALUE_MAX], "source": _src(item), "required": row["required"]})
        missing = [f"{r['name']} ({c})" for c, r in rows.items() if r["required"] and c not in seen]
        if missing:
            dropped.append("обов'язкові параметри без значення: " + ", ".join(missing[:10]))
        return {"fields": _dedupe(fields), "attributes": attrs, "dropped": dropped, "category": category or ""}


def _cat_choices():
    from bots.models import DigiKeyListing
    return DigiKeyListing.CAT_CHOICES
