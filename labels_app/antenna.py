"""
Етикетка антени DYMO з шаблону — як для кабелів CA-* (shipping/services/dymo_label_service.py).

Шаблон shipping/templates_dymo/antenna_label_template.dymo (макет AN220213-01C), змінюється лише:
  {{PN}}   — артикул товару: рядок P/N, штрихкод, QR;
  {{QTY}}  — кількість з рядка замовлення;
  {{LINE}} — рядки над Copyright (блок <!--LINE--> повторюється): Digi-Key P/N, опис, частота, роз'єм;
  {{YEAR}} — поточний рік у Copyright.

Опис, частота й роз'єм: виправлене людиною (LabelText) → склад (Product) → DigiKey (лістинг) → старий .dymo.
Digi-Key P/N = «<Vendor ID>-<Supplier SKU>-ND» (DigiKeyConfig.marketplace_supplier_id + лістинг).
Старі файли в LABELS_DIR не змінюються — лише читаються для порівняння й як запасне джерело.
"""
import html
import re
import textwrap
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
FREQ_KEYS = ("Frequency Range", "Frequency (Center/Band)", "Frequency", "Frequency Band")
CONN_KEYS = ("Termination", "Connector Type", "Connector", "Connector Style")
SOURCES = {
    "edit": "✏️ щойно вписано",
    "saved": "✋ перевірено раніше",
    "stock": "📦 склад",
    "digikey": "🔷 DigiKey",
    "file": "📄 старий файл",
}

_FIELD_NAMES = {"description": "Опис", "frequency": "Частота", "connector": "Роз'єм"}

ANTENNA_RE = re.compile(r"^AN\d{4}", re.I)
_CYR = re.compile(r"[\u0400-\u04FF]")
_DK_RE = re.compile(r"^(?:Digi[- ]?Key\s*)?P\s*[/I]\s*N\s*:\s*(\d{3,6}-\S+?-ND)\.?$", re.I)
_FREQ_RE = re.compile(r"^Freq(?:uency)?\.?(?:\s+range)?\s*:\s*(.+)$", re.I)
_CONN_RE = re.compile(r"^Connector\s+type\s*:\s*(.+)$", re.I)
_COPY_RE = re.compile(r"Copyright\b.*(\d{4})", re.I)
_PN_RE = re.compile(r"^P/N:\s*\S+\.?$", re.I)
_RANGE_RE = re.compile(r"\.\.\.|…|~|\d\s*-\s*\d")


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


def _saved(product):
    if product is None or product.pk is None:
        return None
    try:
        return product.label_text
    except ObjectDoesNotExist:
        return None


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

def _fmt_frequency(value) -> str:
    """DigiKey «370MHz ~ 6GHz» → «370MHz...6GHz» (як на наших етикетках)."""
    return _clean(re.sub(r"\s*~\s*", "...", str(value or "")))


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
            if value and not _CYR.search(value) and value.upper() != (product.sku or "").upper():
                return value
        return ""
    if field == "frequency":
        return _fmt_frequency(_attr(product.tech_attributes, FREQ_KEYS))
    return _clean(_attr(product.tech_attributes, CONN_KEYS))


def _digikey_value(field, listing) -> str:
    if listing is None:
        return ""
    if field == "description":
        title = _clean(listing.dk_title)
        if title:
            return title
        first = re.split(r"(?<=[.!?])\s", (listing.dk_description or "").strip(), maxsplit=1)[0]
        return _clean(first)[:200]
    if field == "frequency":
        return _fmt_frequency(_attr(listing.dk_attributes, FREQ_KEYS))
    return _clean(_attr(listing.dk_attributes, CONN_KEYS))


def _candidates(field, saved, product, listing, old, edit=None) -> list[dict]:
    """[{source, label, value}] у порядку переваги; однакові значення з різних джерел — одним рядком."""
    raw = []
    if edit is not None:
        raw.append(("edit", _clean(edit)))
    if saved is not None:
        raw.append(("saved", _clean(getattr(saved, field))))
    raw.append(("stock", _stock_value(field, product)))
    raw.append(("digikey", _digikey_value(field, listing)))
    raw.append(("file", (old or {}).get(field) or ""))
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


def _vendor_id(old) -> str:
    try:
        from bots.models import DigiKeyConfig
        vendor = (DigiKeyConfig.get().marketplace_supplier_id or "").strip()
    except Exception:
        vendor = ""
    if not vendor and old and old.get("digikey_pn"):
        m = re.match(r"(\d+)-", old["digikey_pn"])
        vendor = m.group(1) if m else ""
    return vendor


def _digikey_pn(sku, listing, old) -> tuple[str, str]:
    """(Digi-Key P/N, звідки) — або ("", чому немає)."""
    if listing is not None:
        vendor = _vendor_id(old)
        on_digikey = bool(listing.dk_product_id) or listing.sync_status == "published"
        if not on_digikey:
            return "", "лістинг DigiKey ще не опубліковано"
        if not vendor:
            return "", "не вказано Vendor ID у налаштуваннях DigiKey"
        return f"{vendor}-{listing.get_supplier_sku()}-ND", "лістинг DigiKey"
    old_pn = (old or {}).get("digikey_pn") or ""
    if old_pn and sku and sku.upper() in old_pn.upper():
        return old_pn, "старий файл"
    return "", "товару немає на DigiKey"


# ── Дані етикетки ─────────────────────────────────────────────────────────────

def _issue(level, text):
    return {"level": level, "text": text}


def build(sku, qty=1, order=None, show_dk=None, edits=None) -> dict:
    """Усе для вікна перевірки й для файлу: значення, джерела, рядки, зауваження.

    edits — {поле: текст} з вікна (ще не збережено); show_dk — None = за джерелом замовлення."""
    sku = (sku or "").strip()
    product = find_product(sku)
    listing = _listing(product)
    saved = _saved(product)
    old = read_file(product.sku if product is not None else sku) or (read_file(sku) if product is not None else None)
    edits = edits or {}
    year = timezone.localdate().year
    pn = product.sku if product is not None else sku
    issues = []

    try:
        qty = int(float(qty))
    except (TypeError, ValueError):
        qty = 0
    if qty < 1:
        issues.append(_issue("error", "Кількість має бути не менше 1."))

    if product is None:
        issues.append(_issue("warn", f"Товару {sku} немає на складі — дані лише зі старого файлу."))
    elif sku and sku.upper() != pn.upper():
        issues.append(_issue("info", f"У замовленні артикул {sku} — на етикетці артикул товару {pn}."))

    fields = {}
    for field in FIELDS:
        cands = _candidates(field, saved, product, listing, old, edits.get(field))
        autos = _candidates(field, None, product, listing, old)
        auto = autos[0] if autos else None
        if field in edits:
            chosen = {"source": "edit", "value": _clean(edits[field])}
        elif saved is not None:
            chosen = {"source": "saved", "value": _clean(getattr(saved, field))}
        else:
            chosen = auto or {"source": "", "value": ""}
        fields[field] = {"value": chosen["value"], "source": chosen["source"], "label": SOURCES.get(chosen["source"], ""),
                         "auto": auto["value"] if auto else "", "candidates": cands}
        name = _FIELD_NAMES[field]
        if chosen["source"] in ("stock", "digikey"):
            for other in autos[1:]:
                if other["source"] in ("stock", "digikey"):
                    issues.append(_issue("warn", f"{name}: {SOURCES[chosen['source']]} «{chosen['value']}», "
                                                 f"а {SOURCES[other['source']]} «{other['value']}» — перевірте."))
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
    if not fields["connector"]["value"]:
        issues.append(_issue("info" if fields["connector"]["source"] in deliberate else "warn",
                             "Немає роз'єму — рядок «Connector type» не друкується."))

    dk_pn, dk_from = _digikey_pn(pn, listing, old)
    if show_dk is None:
        source = (getattr(order, "source", "") or "").lower()
        show_dk = bool(dk_pn) and (not order or source == "digikey")
        dk_reason = ("замовлення DigiKey" if source == "digikey" else
                     "замовлення не з DigiKey" if order else "є на DigiKey" if dk_pn else "")
    else:
        dk_reason = "вибрано вручну"
    if show_dk and not dk_pn:
        issues.append(_issue("warn", f"Digi-Key P/N не друкується: {dk_from}."))
        show_dk = False

    if old:
        if old["standard"]:
            for what, value in (("P/N", old["pn"]), ("штрихкод", old["barcode"]), ("QR", old["qr"])):
                if value and value.upper() != pn.upper():
                    issues.append(_issue("info", f"У старому файлі {old['name']} {what}: {value} — на новій етикетці {pn}."))
            if old["digikey_pn"] and dk_pn and old["digikey_pn"].upper() != dk_pn.upper():
                issues.append(_issue("info", f"У старому файлі Digi-Key P/N {old['digikey_pn']} — правильний {dk_pn}."))
            elif old["digikey_pn"] and not dk_pn and pn.upper() not in old["digikey_pn"].upper():
                issues.append(_issue("info", f"У старому файлі Digi-Key P/N {old['digikey_pn']} іншого артикула — не друкується."))
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
        "digikey_pn": dk_pn, "digikey_from": dk_from, "show_dk": bool(show_dk), "dk_reason": dk_reason,
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
    """Людина перевірила етикетку: запам'ятовує тексти й те, що тоді було на складі / DigiKey."""
    from labels_app.models import LabelText
    product = find_product(sku)
    if product is None:
        return None
    fields = build(sku, edits={f: v for f, v in (edits or {}).items() if f in FIELDS})["fields"]
    text, _ = LabelText.objects.get_or_create(product=product)
    for field in FIELDS:
        setattr(text, field, fields[field]["value"][:LabelText._meta.get_field(field).max_length])
    text.seen = {field: fields[field]["auto"] for field in FIELDS}
    text.checked_at = timezone.now()
    text.checked_by = getattr(user, "username", "") or ""
    text.save()
    return text
