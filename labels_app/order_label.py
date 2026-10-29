"""
Етикетка замовлення DYMO: номер замовлення з кодом країни і всі артикули з кількістю.

Шаблон shipping/templates_dymo/order_label_template.dymo — макет антен без QR, логотипа, штрихкода й Copyright:
  {{ORDER}} {{COUNTRY}} — один рядок на всю ширину: «Order: 102497269   NZ» (країна жирним, `country`);
  {{LINE}} / {{FS}} — нумеровані позиції «1. AN220214-01A × 5» жирним моноширинним шрифтом (Courier New Bold) у стовпчиках; розмір
                      шрифту — найбільший, з яким усе вміщується (`layout`), інакше останній рядок «+N more».
Артикул — як у картці замовлення: з рядка замовлення, інакше товару; однакові артикули складаються.
"""
import math
import re
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings

TEMPLATE_PATH = Path(settings.BASE_DIR) / "shipping" / "templates_dymo" / "order_label_template.dymo"

TEXT_W, TEXT_H = 3.13, 1.04      # блок позицій, дюйми (як у шаблоні)
FONT_SIZES = (12, 11, 10, 9, 8, 7, 6)
CHAR_EM = 0.6                    # ширина символу Courier New
LINE_EM = 1.2                    # висота рядка з запасом
GAP = 3                          # пробіли між стовпчиками
NBSP = " "                  # DYMO не стискає нерозривні пробіли


def _qty(value) -> str:
    value = Decimal(value or 0)
    return str(value.quantize(Decimal(1))) if value == value.to_integral_value() else f"{value.normalize():f}"


def items(order) -> list[tuple[str, Decimal]]:
    """[(артикул, кількість)] у порядку рядків замовлення."""
    merged = {}
    for line in order.lines.select_related("product").order_by("pk"):
        sku = (line.sku_raw or "").strip() or (line.product.sku if line.product_id else "")
        if sku:
            merged[sku] = merged.get(sku, Decimal(0)) + Decimal(line.qty or 0)
    return list(merged.items())


def layout(rows) -> tuple[int, list[str]]:
    """(розмір шрифту, рядки етикетки) — найбільший шрифт і найменше стовпчиків, з якими все вміщується.
    Рядок позиції: «1. AN220214-01A × 5» (номер позиції, артикул, кількість — вирівняні)."""
    if not rows:
        return FONT_SIZES[0], []
    num_w = len(str(len(rows)))
    sku_w = max(len(sku) for sku, _q in rows)
    qty_w = max(len(_qty(q)) for _s, q in rows)
    cells = [f"{str(n).rjust(num_w)}. {sku.ljust(sku_w)} × {_qty(q).rjust(qty_w)}"
             for n, (sku, q) in enumerate(rows, 1)]
    cell_w = len(cells[0])
    for size in FONT_SIZES:
        per_line = int(TEXT_W / (CHAR_EM * size / 72))
        cols_max = max(1, (per_line + GAP) // (cell_w + GAP))
        lines_max = max(1, int(TEXT_H / (LINE_EM * size / 72)))
        if len(cells) <= cols_max * lines_max:
            break
    else:                                     # навіть найменшим шрифтом не вміщується
        keep = cols_max * lines_max - 1
        cells = cells[:keep] + [f"+{len(cells) - keep} more"]
    cols = min(cols_max, math.ceil(len(cells) / lines_max))
    depth = math.ceil(len(cells) / cols)       # заповнення стовпчиками: згори вниз, потім праворуч
    lines = []
    for r in range(depth):
        parts = [cells[c * depth + r] for c in range(cols) if c * depth + r < len(cells)]
        lines.append((" " * GAP).join(p.ljust(cell_w) for p in parts).rstrip())
    return size, lines


def country(order) -> str:
    """Код країни доставки (ISO 2): адреса замовлення → отримувач останнього відправлення → регіон доставки."""
    from config.country_utils import normalize_to_iso2
    shipment = order.shipments.exclude(recipient_country="").order_by("-pk").first()
    for code in (order.addr_country, shipment.recipient_country if shipment else "",
                 normalize_to_iso2(order.shipping_region or "")):
        code = (code or "").strip().upper()
        if re.fullmatch(r"[A-Z]{2}", code):
            return code
    return ""


def build(order) -> dict:
    rows = items(order)
    size, lines = layout(rows)
    return {
        "order": order.order_number,
        "country": country(order),
        "font_size": size,
        "lines": lines,
        "items": [(sku, _qty(q)) for sku, q in rows],
    }


def render(data) -> str:
    """XML етикетки (.dymo) з шаблону."""
    xml = TEMPLATE_PATH.read_text(encoding="utf-8-sig")
    block = re.search(r"[ \t]*<!--LINE-->\r?\n(.*?)[ \t]*<!--/LINE-->\r?\n", xml, re.S)
    span = block.group(1).replace("{{FS}}", str(data["font_size"]))
    spans = "".join(span.replace("{{LINE}}", escape(line.replace(" ", NBSP))) for line in data["lines"])
    xml = xml[:block.start()] + spans + xml[block.end():]
    country = NBSP * 3 + data["country"] if data["country"] else ""      # той самий рядок, що й номер
    for marker, value in (("{{ORDER}}", data["order"]), ("{{COUNTRY}}", country)):
        xml = xml.replace(marker, escape(value))
    return xml
