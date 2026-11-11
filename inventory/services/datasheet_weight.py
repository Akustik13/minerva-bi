"""
inventory/services/datasheet_weight.py — вага нетто за 1 шт з даташита товару.

Без AI: текст PDF (той самий даташит, що читає агент — файл у картці → посилання товару → правило «Налаштувань
складу») → шаблони «Weight: 5.3 g», «Gewicht 12 g», «Mass (g) 3.2», «Вага 0,8 кг» → кандидати з цитатою.
Записує в товар лише людина (`save`); «у даташиті ваги немає» теж запам'ятовується (`Product.net_weight_note`,
`net_weight_checked_at`), щоб не перевіряти той самий даташит щоразу.
"""
import re
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from django.utils import timezone

KEYWORDS = r"(?:net\s*weight|nettogewicht|gewicht|weight|mass|masse|вага|масса|poids|peso)"
UNITS = {"mg": Decimal("0.001"), "g": Decimal(1), "gr": Decimal(1), "gram": Decimal(1), "grams": Decimal(1),
         "gramm": Decimal(1), "kg": Decimal(1000), "oz": Decimal("28.3495"), "lb": Decimal("453.592"),
         "lbs": Decimal("453.592"), "г": Decimal(1), "кг": Decimal(1000), "мг": Decimal("0.001")}
_UNIT = r"(mg|grams?|gramm|gr|g|kg|oz|lbs?|мг|кг|г)"
_NUM = r"(\d+(?:[.,]\d+)?)"
# «Weight: approx. 5.3 g» / «Weight 5,3g» — число й одиниця після слова
_AFTER = re.compile(KEYWORDS + r"[^\d\n]{0,30}?" + _NUM + r"\s*" + _UNIT + r"(?![a-zа-я/])", re.I)
# «Weight (g) 5.3» / «Mass [kg]: 0.01» — одиниця в дужках перед числом
_BRACKET = re.compile(KEYWORDS + r"\s*[\(\[]\s*" + _UNIT + r"\s*[\)\]][^\d\n]{0,20}?" + _NUM, re.I)
MAX_CANDIDATES = 5
QUOTE_CHARS = 120


class NoDatasheet(Exception):
    pass


def to_grams(value, unit) -> Decimal | None:
    try:
        v = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    k = UNITS.get((unit or "").lower())
    if k is None or v <= 0:
        return None
    g = (v * k).quantize(Decimal("0.0001"))
    return g if Decimal("0.0001") <= g <= Decimal(100000) else None


def _quote(text, start, end):
    a, b = max(0, start - 30), min(len(text), end + 30)
    return re.sub(r"\s+", " ", text[a:b]).strip()[:QUOTE_CHARS]


def find(pages) -> list:
    """[{grams, quote, page}] з тексту сторінок; та сама вага — один кандидат (найперша цитата, `hits`)."""
    found = OrderedDict()
    for n, text in enumerate(pages, 1):
        for rx, num_i, unit_i in ((_AFTER, 1, 2), (_BRACKET, 2, 1)):
            for m in rx.finditer(text or ""):
                g = to_grams(m.group(num_i), m.group(unit_i))
                if g is None:
                    continue
                key = g.normalize()
                if key in found:
                    found[key]["hits"] += 1
                    continue
                found[key] = {"grams": g, "quote": _quote(text, m.start(), m.end()), "page": n, "hits": 1}
    out = sorted(found.values(), key=lambda c: (-c["hits"], c["page"]))
    return out[:MAX_CANDIDATES]


def datasheet_pages(product) -> tuple:
    """(сторінки тексту, звідки даташит). NoDatasheet — даташита немає або PDF не читається."""
    from agent.tools import ToolError, _datasheet_bytes, _pdf_pages
    try:
        data, where = _datasheet_bytes(product)
        return _pdf_pages(data), where
    except ToolError as e:
        raise NoDatasheet(str(e)) from None


def check(product) -> dict:
    """Шукає вагу в даташиті: {status: found | none | no_datasheet, candidates, where, error}.
    none / no_datasheet — запам'ятовується в товарі (дата й примітка), вагу не змінює."""
    try:
        pages, where = datasheet_pages(product)
    except NoDatasheet as e:
        _remember(product, f"📄 Даташита немає або він не читається ({e})"[:300])
        return {"status": "no_datasheet", "candidates": [], "where": "", "error": str(e)}
    candidates = find(pages)
    if not candidates:
        text = "".join(pages).strip()
        why = "у даташиті ваги немає" if text else "даташит — скан без тексту, ваги не знайдено"
        _remember(product, f"📄 {why[0].upper()}{why[1:]}")
        return {"status": "none", "candidates": [], "where": where, "error": ""}
    return {"status": "found", "candidates": candidates, "where": where, "error": ""}


def _remember(product, note):
    from inventory.models import Product
    now = timezone.now()
    Product.objects.filter(pk=product.pk).update(
        net_weight_checked_at=now,
        net_weight_note=f"{note} · {timezone.localtime(now):%d.%m.%Y}"[:300])
    product.net_weight_checked_at = now


def save(product, grams, source, note="") -> Decimal:
    """Записати вагу нетто в товар (людина підтвердила). source — datasheet / manual."""
    from inventory.models import Product
    g = to_grams(grams, "g")
    if g is None:
        raise ValueError("Вага має бути додатним числом у грамах.")
    if source not in dict(Product.WEIGHT_SOURCES):
        source = "manual"
    if source == "datasheet":
        note = f"📄 {note}".strip() if note else "📄 З даташита"
    elif source == "manual" and not note and product.net_weight_note.startswith("📄"):
        note = product.net_weight_note             # «у даташиті немає» — лишається поясненням
    product.net_weight_g = g
    product.net_weight_source = source
    product.net_weight_note = (note or "")[:300]
    if source == "datasheet":
        product.net_weight_checked_at = timezone.now()
    product.save(update_fields=["net_weight_g", "net_weight_source", "net_weight_note", "net_weight_checked_at"])
    return g


def status(product) -> dict:
    """Стан ваги товару для сторінок: {grams, source, source_text, note, checked}."""
    return {
        "grams": product.net_weight_g,
        "source": product.net_weight_source,
        "source_text": dict(product.WEIGHT_SOURCES).get(product.net_weight_source, ""),
        "note": product.net_weight_note,
        "checked": product.net_weight_checked_at,
    }
