"""
Етикетка замовлення DYMO: номер замовлення (текстом і штрихкодом) і всі артикули з кількістю.

Шаблон shipping/templates_dymo/order_label_template.dymo — той самий макет, що в антен, без QR і Copyright:
  {{ORDER}}   — номер замовлення: рядок «Order:» і штрихкод;
  {{SUMMARY}} — «4 Pos. · 15 PCS»;
  {{LINE}} / {{FS}} — рядки позицій моноширинним шрифтом (Courier New) у стовпчиках; розмір шрифту —
                      найбільший, з яким усе вміщується (`layout`), інакше останній рядок «+N more».
Артикул — як у картці замовлення: з рядка замовлення, інакше товару; однакові артикули складаються.
"""
import math
import re
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings

TEMPLATE_PATH = Path(settings.BASE_DIR) / "shipping" / "templates_dymo" / "order_label_template.dymo"

TEXT_W, TEXT_H = 3.13, 0.776     # блок позицій, дюйми (як у шаблоні)
FONT_SIZES = (10, 9, 8, 7, 6)
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
    """(розмір шрифту, рядки етикетки) — найбільший шрифт і найменше стовпчиків, з якими все вміщується."""
    if not rows:
        return FONT_SIZES[0], []
    sku_w = max(len(sku) for sku, _q in rows)
    qty_w = max(len(_qty(q)) for _s, q in rows)
    cells = [f"{sku.ljust(sku_w)} × {_qty(q).rjust(qty_w)}" for sku, q in rows]
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


def build(order) -> dict:
    rows = items(order)
    size, lines = layout(rows)
    total = sum((q for _s, q in rows), Decimal(0))
    return {
        "order": order.order_number,
        "summary": f"{len(rows)} Pos. · {_qty(total)} PCS",
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
    for marker, value in (("{{ORDER}}", data["order"]), ("{{SUMMARY}}", data["summary"])):
        xml = xml.replace(marker, escape(value))
    return xml
