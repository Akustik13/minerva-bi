"""
Етикетка антени DYMO з шаблону — як для кабелів CA-* (shipping/services/dymo_label_service.py).

Шаблон shipping/templates_dymo/antenna_label_template.dymo (макет AN220213-01C), змінюється лише:
  {{PN}}   — артикул з рядка замовлення (саме той, що замовили): рядок P/N, штрихкод, QR;
  {{QTY}}  — кількість з рядка замовлення;
  {{LINE}} — рядки над Copyright (блок <!--LINE--> повторюється): Digi-Key P/N, опис, частота, роз'єм;
  {{YEAR}} — поточний рік у Copyright.

Опис, частота, роз'єм і Digi-Key P/N: підтверджене людиною для цього артикула (LabelText) → склад (Product) →
DigiKey (лістинг) → старий .dymo. Якщо товар знайдено лише за Supplier SKU DigiKey (у замовленні інший артикул,
ніж у картці товару), дані складу — іншого артикула, тому старий файл артикула йде першим.
Digi-Key P/N = «<Vendor ID>-<Supplier SKU>-ND»; Vendor ID — число (3228): marketplace_supplier_id лише якщо це
число (для Marketplace API там UUID), інакше — з Digi-Key P/N старих етикеток.
Частота з DigiKey — в одних одиницях: «600MHz ~ 7.2GHz» → «0.600...7.2 GHz».
Старі файли в LABELS_DIR не змінюються — лише читаються для порівняння й як запасне джерело.
"""
import html
import re
import textwrap
from collections import Counter
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

TEMPLATE_PATH = Path(settings.BASE_DIR) / "shipping" / "templates_dymo" / "antenna_label_template.dymo"

LINE_MAX = 46          # символів у рядку (Arial 8, блок 2,31″, FitMode None) — довше DYMO обріже
TEXT_LINES_MAX = 4     # рядків над Copyright: блок вміщує 6 разом із двома рядками Copyright
DESC_LINES_MAX = 2

FIELDS = ("description", "frequency", "connector")
EDIT_FIELDS = FIELDS + ("digikey_pn",)
FREQ_KEYS = ("Frequency Range", "Frequency (Center/Band)", "Frequency", "Frequency Band")
CONN_KEYS = ("Termination", "Connector Type", "Connector", "Connector Style")
SOURCES = {
    "edit": "✏️ щойно вписано",
    "saved": "✋ перевірено раніше",
    "stock": "📦 склад",
    "digikey": "🔷 DigiKey",
    "file": "📄 старий файл",
}

_FIELD_NAMES = {"description": "Опис", "frequency": "Частота", "connector": "Роз'єм", "digikey_pn": "Digi-Key P/N"}

ANTENNA_RE = re.compile(r"^AN\d{4}", re.I)
_CYR = re.compile(r"[\u0400-\u04FF]")
_DK_RE = re.compile(r"^(?:Digi[- ]?Key\s*)?P\s*[/I]\s*N\s*:\s*(\d{3,6}-\S+?-ND)\.?$", re.I)
_DK_PN_RE = re.compile(r"(?<![\w-])(\d{3,6})-([A-Za-z0-9][\w.\-]*?)-ND(?![\w-])", re.I)
_FREQ_RE = re.compile(r"^Freq(?:uency)?\.?(?:\s+range)?\s*:\s*(.+)$", re.I)
_CONN_RE = re.compile(r"^Connector\s+type\s*:\s*(.+)$", re.I)
_COPY_RE = re.compile(r"Copyright\b.*(\d{4})", re.I)
_PN_RE = re.compile(r"^P/N:\s*\S+\.?$", re.I)
_RANGE_RE = re.compile(r"\.\.\.|…|~|\d\s*-\s*\d")
_UNIT_RANGE_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(kHz|MHz|GHz)?\s*(?:\.\.\.|…|~|-)\s*(\d+(?:[.,]\d+)?)\s*(kHz|MHz|GHz)", re.I)
_UNITS = {"khz": ("kHz", 0.001), "mhz": ("MHz", 1.0), "ghz": ("GHz", 1000.0)}   # множник до MHz


def is_antenna(sku, product=None) -> bool:
    if product is not None and "antenn" in (product.category or "").lower():
        return True
    return bool(ANTENNA_RE.match((sku or "").strip()))


def _clean(value) -> str:
    """Один рядок без зайвих пробілів і без крапки в кінці (крапку ставить етикетка)."""
    s = " ".join(str(value or "").replace("\u00a0", " ").split())
    return s.rstrip(" .") if not s.endswith("...") else s


def _norm(value) -> str:
    return re.sub(r"\s+", "", _clean(value).lower().replace("…", "...").replace("~", "..."))


def _sentence(value) -> str:
    return value if value.endswith((".", "!", "?")) else value + "."


def _is_code(value) -> bool:
    """«3228-AN220214-01A-ND», «AN220213-01C» — номер, а не опис."""
    return " " not in value or bool(_DK_PN_RE.search(value))


def find_product(sku):
    """Товар за артикулом; інакше — за Supplier SKU лістингу DigiKey (рядки замовлень DigiKey)."""
    from inventory.models import Product
    sku = (sku or "").strip()
    product = Product.objects.filter(sku__iexact=sku).first()
    if product or not sku:
        return product
    try:
        from bots.models import DigiKeyListing
    except ImportError:
        return None
    listing = (DigiKeyListing.objects.filter(dk_supplier_sku__iexact=sku, product__isnull=False)
               .select_related("product").first())
    return listing.product if listing else None


def _listing(product):
    if product is None:
        return None
    try:
        return product.dk_listing
    except (ObjectDoesNotExist, AttributeError):
        return None


def _saved(pn):
    from labels_app.models import LabelText
    return LabelText.objects.filter(sku__iexact=pn).first() if pn else None


# ── Старий файл етикетки ──────────────────────────────────────────────────────

def _object_lines(block: str) -> list[str]:
    """Рядки текстового об'єкта DYMO: один LineTextSpan = один рядок (текст може бути в кількох TextSpan)."""
    lines = []
    for span in re.findall(r"<LineTextSpan>(.*?)</LineTextSpan>", block, re.S):
        lines.append(html.unescape("".join(re.findall(r"<Text>([^<]*)</Text>", span))))
    return lines


def read_file(sku):
    """Що написано в наявному .dymo для артикула (LABELS_DIR) — або None, якщо файлу немає."""
    from labels_app.views import get_label_path
    path = get_label_path(sku)
    if not path:
        return None
    out = {"name": path.name, "standard": False, "pn": "", "qty": None, "barcode": "", "qr": "",
           "digikey_pn": "", "description": "", "frequency": "", "connector": "", "year": None}
    try:
        xml = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return out
    body = []
    for block in re.findall(r"<TextObject>(.*?)</TextObject>", xml, re.S):
        lines = _object_lines(block)
        if any("Copyright" in line for line in lines):
            body = lines
            continue
        for line in lines:
            s = _clean(line)
            if s.upper().startswith("P/N:") and not out["pn"]:
                out["pn"] = s[4:].strip()
            m = re.match(r"QTY:\s*(\d+)", s, re.I)
            if m and out["qty"] is None:
                out["qty"] = int(m.group(1))
    m = re.search(r"<BarcodeObject>.*?</BarcodeObject>", xml, re.S)
    if m:
        out["barcode"] = next((html.unescape(d).strip() for d in re.findall(r"<DataString>([^<]*)</DataString>", m.group(0))
                               if d.strip()), "")
    m = re.search(r"<QRCodeObject>.*?<DataString>([^<]*)</DataString>", xml, re.S)
    if m:
        out["qr"] = html.unescape(m.group(1)).strip()

    desc, state = [], "desc"
    for line in body:
        s = " ".join(line.replace("\u00a0", " ").split())
        if not s:
            continue
        m = _COPY_RE.search(s)
        if m:
            out["year"], state = int(m.group(1)), "end"
            continue
        if state == "end":
            continue
        m = _DK_RE.match(s)
        if m:
            out["digikey_pn"] = m.group(1)
            continue
        m = _FREQ_RE.match(s)
        if m:
            out["frequency"], state = _clean(m.group(1)), "freq"
            continue
        m = _CONN_RE.match(s)
        if m:
            out["connector"], state = _clean(m.group(1)), "conn"
            continue
        if _PN_RE.match(s):
            continue
        if state == "freq" and out["frequency"].endswith(","):
            out["frequency"] = _clean(out["frequency"] + " " + s)
            continue
        desc.append(s)
    out["description"] = _clean(" ".join(desc))
    out["standard"] = bool(body)
    return out


# ── Джерела значень ───────────────────────────────────────────────────────────

def _number(value: float, unit: str) -> str:
    """0.65 GHz → «0.650» (як на наших етикетках), 7.2 → «7.2», 960 → «960»."""
    if unit == "GHz" and value < 1:
        return f"{value:.3f}"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _one_unit(m) -> str:
    a, unit_a, b, unit_b = m.groups()
    unit_a = (unit_a or unit_b).lower()
    unit_b = unit_b.lower()
    target, scale = max(_UNITS[unit_a], _UNITS[unit_b], key=lambda u: u[1])
    low = float(a.replace(",", ".")) * _UNITS[unit_a][1] / scale
    high = float(b.replace(",", ".")) * _UNITS[unit_b][1] / scale
    return f"{_number(low, target)}...{_number(high, target)} {target}"


def _fmt_frequency(value) -> str:
    """DigiKey «600MHz ~ 7.2GHz» → «0.600...7.2 GHz»: кожен діапазон — в одних одиницях."""
    return _clean(_UNIT_RANGE_RE.sub(_one_unit, str(value or "")))


def _mixed_units(value) -> bool:
    return any(m.group(2) and m.group(2).lower() != m.group(4).lower() for m in _UNIT_RANGE_RE.finditer(value or ""))


def _attr(attrs, keys) -> str:
    attrs = attrs if isinstance(attrs, dict) else {}
    for key in keys:
        value = attrs.get(key)
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value if v)
        if value and str(value).strip() and str(value).strip() != "-":
            return str(value)
    return ""


def _stock_value(field, product) -> str:
    if product is None:
        return ""
    if field == "description":
        for value in (product.name_export, product.name):
            value = _clean(value)
            if value and not _CYR.search(value) and not _is_code(value):
                return value
        return ""
    if field == "frequency":
        return _fmt_frequency(_attr(product.tech_attributes, FREQ_KEYS))
    if field == "connector":
        return _clean(_attr(product.tech_attributes, CONN_KEYS))
    return ""


def _digikey_value(field, listing) -> str:
    if listing is None:
        return ""
    if field == "description":
        title = _clean(listing.dk_title)
        if title and not _is_code(title):
            return title
        first = _clean(re.split(r"(?<=[.!?])\s", (listing.dk_description or "").strip(), maxsplit=1)[0])[:200]
        return "" if not first or _is_code(first) else first
    if field == "frequency":
        return _fmt_frequency(_attr(listing.dk_attributes, FREQ_KEYS))
    if field == "connector":
        return _clean(_attr(listing.dk_attributes, CONN_KEYS))
    return ""


def _candidates(values, edit=None) -> list[dict]:
    """values — [(джерело, значення)] у порядку переваги → [{source, label, value}];
    однакові значення з різних джерел — одним рядком."""
    raw = ([("edit", _clean(edit))] if edit is not None else []) + list(values)
    out, seen = [], {}
    for source, value in raw:
        if not value and source != "edit":
            continue
        key = _norm(value)
        if key in seen:
            seen[key]["sources"].append(source)
            seen[key]["label"] += " · " + SOURCES[source]
            continue
        item = {"source": source, "sources": [source], "label": SOURCES[source], "value": value}
        seen[key] = item
        out.append(item)
    return out


_VENDOR_CACHE = {}


def _vendor_from_files() -> str:
    """Найчастіший числовий префікс Digi-Key P/N у старих етикетках (кеш до зміни теки)."""
    from labels_app.views import LABELS_DIR
    try:
        stamp = (str(LABELS_DIR), LABELS_DIR.stat().st_mtime)
    except OSError:
        return ""
    if stamp not in _VENDOR_CACHE:
        counts = Counter()
        for path in LABELS_DIR.glob("*.dymo"):
            try:
                counts.update(m.group(1) for m in _DK_PN_RE.finditer(path.read_text(encoding="utf-8-sig", errors="ignore")))
            except OSError:
                continue
        _VENDOR_CACHE.clear()
        _VENDOR_CACHE[stamp] = counts.most_common(1)[0][0] if counts else ""
    return _VENDOR_CACHE[stamp]


def _vendor_id(old) -> str:
    """Числовий Vendor ID DigiKey (3228) — не UUID постачальника Marketplace."""
    try:
        from bots.models import DigiKeyConfig
        vendor = (DigiKeyConfig.get().marketplace_supplier_id or "").strip()
    except Exception:
        vendor = ""
    if vendor.isdigit():
        return vendor
    m = _DK_PN_RE.search((old or {}).get("digikey_pn") or "")
    return m.group(1) if m else _vendor_from_files()


def _digikey_pn(listing, old) -> tuple[str, str]:
    """(Digi-Key P/N з лістингу, "") — або ("", чому немає)."""
    if listing is None:
        return "", "товару немає на DigiKey"
    if not (listing.dk_product_id or listing.sync_status == "published"):
        return "", "лістинг DigiKey ще не опубліковано"
    vendor = _vendor_id(old)
    if not vendor:
        return "", "невідомий числовий Vendor ID DigiKey (як 3228) — впишіть Digi-Key P/N"
    return f"{vendor}-{listing.get_supplier_sku()}-ND", ""


# ── Дані етикетки ─────────────────────────────────────────────────────────────

def _issue(level, text):
    return {"level": level, "text": text}


def build(sku, qty=1, order=None, show_dk=None, edits=None) -> dict:
    """Усе для вікна перевірки й для файлу: значення, джерела, рядки, зауваження.

    edits — {поле: текст} з вікна (ще не збережено); show_dk — None = за джерелом замовлення."""
    sku = (sku or "").strip()
    product = find_product(sku)
    exact = product is not None and product.sku.upper() == sku.upper()
    pn = product.sku if exact else sku               # на етикетці — артикул із замовлення
    listing = _listing(product)
    saved = _saved(pn)
    old = read_file(pn)
    edits = {f: v for f, v in (edits or {}).items() if f in EDIT_FIELDS}
    year = timezone.localdate().year
    issues = []

    try:
        qty = int(float(qty))
    except (TypeError, ValueError):
        qty = 0
    if qty < 1:
        issues.append(_issue("error", "Кількість має бути не менше 1."))

    if product is None:
        issues.append(_issue("warn", f"Товару {sku} немає на складі — дані лише зі старого файлу."))
    elif not exact:
        issues.append(_issue("info", f"{pn} — Supplier SKU DigiKey товару {product.sku}: на етикетці {pn}, "
                                     f"спершу дані старої етикетки {pn}, потім DigiKey і склад."))

    dk_auto, dk_why = _digikey_pn(listing, old)
    old_dk = (old or {}).get("digikey_pn") or ""
    fields = {}
    for field in EDIT_FIELDS:
        if field == "digikey_pn":
            own = pn.upper() in old_dk.upper()       # у старому файлі може бути P/N іншого артикула
            autos_raw = [("digikey", dk_auto), ("file", old_dk if own else "")]
            chips_raw = autos_raw + ([] if own else [("file", old_dk)])
        else:
            stock = ("stock", _stock_value(field, product))
            digikey = ("digikey", _digikey_value(field, listing))
            file = ("file", (old or {}).get(field) or "")
            autos_raw = [stock, digikey, file] if exact else [file, digikey, stock]
            chips_raw = autos_raw
        saved_value = _clean(getattr(saved, field)) if saved is not None else ""
        cands = _candidates(([("saved", saved_value)] if saved_value else []) + chips_raw, edits.get(field))
        autos = _candidates(autos_raw)
        auto = autos[0] if autos else None
        if field in edits:
            chosen = {"source": "edit", "value": _clean(edits[field])}
        elif saved is not None and (saved_value or field != "digikey_pn"):
            chosen = {"source": "saved", "value": saved_value}
        else:
            chosen = auto or {"source": "", "value": ""}
        fields[field] = {"value": chosen["value"], "source": chosen["source"], "label": SOURCES.get(chosen["source"], ""),
                         "auto": auto["value"] if auto else "", "candidates": cands}
        name = _FIELD_NAMES[field]
        if chosen["source"] in ("stock", "digikey", "file"):
            for other in autos[1:]:
                issues.append(_issue("warn", f"{name}: {SOURCES[chosen['source']]} «{chosen['value']}», "
                                             f"а {other['label']} «{other['value']}» — перевірте."))
        seen = (saved.seen or {}).get(field) if saved is not None and isinstance(saved.seen, dict) else None
        if chosen["source"] == "saved" and seen is not None and _norm(seen) != _norm(fields[field]["auto"]):
            issues.append(_issue("warn", f"{name}: {auto['label'] if auto else 'дані'} тепер «{fields[field]['auto']}» "
                                         f"(під час перевірки було «{seen}») — перевірте."))

    deliberate = ("saved", "edit")
    if not fields["description"]["value"]:
        issues.append(_issue("error", "Немає опису: ні на складі, ні на DigiKey, ні в старому файлі — впишіть."))
    if not fields["frequency"]["value"]:
        issues.append(_issue("info" if fields["frequency"]["source"] in deliberate else "warn",
                             "Немає частоти — рядок «Frequency» не друкується."))
    elif _mixed_units(fields["frequency"]["value"]):
        issues.append(_issue("warn", f"Частота «{fields['frequency']['value']}»: у діапазоні різні одиниці — "
                                     "впишіть в одних (напр. 0.650...9 GHz)."))
    if not fields["connector"]["value"]:
        issues.append(_issue("info" if fields["connector"]["source"] in deliberate else "warn",
                             "Немає роз'єму — рядок «Connector type» не друкується."))

    dk_pn = fields["digikey_pn"]["value"]
    if show_dk is None:
        source = (getattr(order, "source", "") or "").lower()
        show_dk = bool(dk_pn) and (not order or source == "digikey")
        dk_reason = ("замовлення DigiKey" if source == "digikey" else
                     "замовлення не з DigiKey" if order else "є на DigiKey" if dk_pn else "")
    else:
        dk_reason = "вибрано вручну"
    if show_dk and not dk_pn:
        issues.append(_issue("warn", f"Digi-Key P/N не друкується: {dk_why or 'не вказано'}."))
        show_dk = False
    if dk_pn and not re.fullmatch(r"\d{3,6}-\S+-ND", dk_pn, re.I):
        issues.append(_issue("warn", f"Digi-Key P/N «{dk_pn}» не схожий на номер DigiKey (3228-…-ND)."))
    elif dk_pn and pn.upper() not in dk_pn.upper():
        issues.append(_issue("warn", f"Digi-Key P/N {dk_pn} — не артикула {pn}, перевірте."))

    if old:
        if old["standard"]:
            for what, value in (("P/N", old["pn"]), ("штрихкод", old["barcode"]), ("QR", old["qr"])):
                if value and value.upper() != pn.upper():
                    issues.append(_issue("info", f"У старому файлі {old['name']} {what}: {value} — на новій етикетці {pn}."))
            if old_dk and dk_pn and old_dk.upper() != dk_pn.upper():
                issues.append(_issue("info", f"У старому файлі Digi-Key P/N {old_dk} — на новій етикетці {dk_pn}."))
            if old["year"] and old["year"] != year:
                issues.append(_issue("info", f"У старому файлі рік {old['year']} — на новій етикетці {year}."))
        else:
            issues.append(_issue("info", f"Файл {old['name']} має інший макет — його можна надрукувати як є («📄 Старий файл»)."))

    lines = label_lines(dk_pn if show_dk else "", fields["description"]["value"],
                        fields["frequency"]["value"], fields["connector"]["value"])
    desc_lines = sum(1 for prefix, _v in lines if prefix == "")
    if desc_lines > DESC_LINES_MAX:
        issues.append(_issue("error", f"Опис займає {desc_lines} рядки — вміщується {DESC_LINES_MAX}. Скоротіть."))
    elif len(lines) > TEXT_LINES_MAX:
        issues.append(_issue("error", f"Над Copyright {len(lines)} рядків — вміщується {TEXT_LINES_MAX}. "
                                      "Скоротіть опис до одного рядка або приберіть Digi-Key P/N."))
    for prefix, value in lines:
        if len(prefix + value) > LINE_MAX:
            issues.append(_issue("warn", f"Рядок «{prefix}{value}» довший за {LINE_MAX} символів — DYMO обріже кінець."))

    return {
        "sku": sku, "pn": pn, "qty": qty, "year": year,
        "product": ({"pk": product.pk, "name": product.name, "sku": product.sku} if product is not None else None),
        "digikey_pn": dk_pn, "digikey_from": dk_why, "show_dk": bool(show_dk), "dk_reason": dk_reason,
        "fields": fields, "lines": lines, "issues": issues, "file": old,
        "can_print": not any(i["level"] == "error" for i in issues),
        # «тиснем і робить»: людина вже перевіряла, і з того часу немає зауважень — друк без вікна
        "ready": saved is not None and not edits and not any(i["level"] in ("error", "warn") for i in issues),
        "checked_at": saved.checked_at.isoformat() if saved is not None and saved.checked_at else None,
        "checked_by": saved.checked_by if saved is not None else "",
    }


def label_lines(digikey_pn, description, frequency, connector) -> list[tuple[str, str]]:
    """Рядки над Copyright як (незмінний початок, значення з даних)."""
    lines = []
    if digikey_pn:
        lines.append(("Digi-Key P/N: ", _sentence(digikey_pn)))
    if description:
        for part in textwrap.wrap(_sentence(description), LINE_MAX, break_long_words=False, break_on_hyphens=False):
            lines.append(("", part))
    if frequency:
        prefix = "Frequency range: " if _RANGE_RE.search(frequency) else "Frequency: "
        lines.append((prefix, _sentence(frequency)))
    if connector:
        lines.append(("Connector type: ", _sentence(connector)))
    return lines


def render(data) -> str:
    """XML етикетки (.dymo) з шаблону."""
    xml = TEMPLATE_PATH.read_text(encoding="utf-8-sig")
    block = re.search(r"[ \t]*<!--LINE-->\r?\n(.*?)[ \t]*<!--/LINE-->\r?\n", xml, re.S)
    spans = "".join(block.group(1).replace("{{LINE}}", escape(prefix + value)) for prefix, value in data["lines"])
    xml = xml[:block.start()] + spans + xml[block.end():]
    for marker, value in (("{{PN}}", data["pn"]), ("{{QTY}}", str(data["qty"])), ("{{YEAR}}", str(data["year"]))):
        xml = xml.replace(marker, escape(value))
    return xml


def save(sku, edits, user=None):
    """Людина перевірила етикетку артикула: запам'ятовує тексти й те, що тоді давали склад / DigiKey / файл."""
    from labels_app.models import LabelText
    data = build(sku, edits=edits)
    fields = data["fields"]
    text = LabelText.objects.filter(sku__iexact=data["pn"]).first() or LabelText(sku=data["pn"])
    text.product_id = data["product"]["pk"] if data["product"] else None
    for field in EDIT_FIELDS:
        setattr(text, field, fields[field]["value"][:LabelText._meta.get_field(field).max_length])
    text.seen = {field: fields[field]["auto"] for field in EDIT_FIELDS}
    text.checked_at = timezone.now()
    text.checked_by = getattr(user, "username", "") or ""
    text.save()
    return text
