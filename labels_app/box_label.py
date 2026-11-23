"""
Наклейка коробки (пакувального матеріалу) DYMO — на полицю або стос коробок.

Шаблон shipping/templates_dymo/box_label_template.dymo (той самий формат, що етикетка замовлення):
  {{TITLE}} — назва коробки великими жирними літерами (AlwaysFit);
  {{CODE}}  — штрихкод Code128 з кодом коробки (`box_names.barcode_value`: код, без нього — PKG-<номер>), код під ним;
  {{SIZE}}  — «30×30×30 cm · ↧ 27/22/16 · 27 L»; {{INFO}} — клас, тара, макс. вага вмісту;
  {{LINE}} / {{FS}} — «Fits:» і що в неї кладемо — рекомендовані упаковки товарів (`ProductPackaging`: артикул ×
                      скільки вміщує), рядками до `LINE_CHARS` символів, до `MAX_LINES` рядків, решта — «+N more».
Без товарів — нотатки коробки (перший рядок). XML-коментарів DYMO не приймає — маркери блоку прибирає render.
"""
import re
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings

TEMPLATE_PATH = Path(settings.BASE_DIR) / "shipping" / "templates_dymo" / "box_label_template.dymo"
LINE_CHARS = 58          # Arial 8 pt на ширину 3,13″
MAX_LINES = 4
FONT_SIZE = 8


def _n(v) -> str:
    return f"{Decimal(v).normalize():f}"


def contents(material) -> list[tuple[str, int]]:
    """[(артикул, скільки вміщує)] — товари, для яких ця коробка — рекомендована упаковка (найбільша місткість,
    спершу «Рекомендована»), від найбільшої місткості."""
    from shipping.models import ProductPackaging
    best = {}
    for r in (ProductPackaging.objects.filter(packaging=material).select_related("product")
              .order_by("-is_default", "-qty_per_box", "pk")):
        sku = (r.product.sku or "").strip()
        if sku and r.qty_per_box > best.get(sku, (0, False))[0]:
            best[sku] = (r.qty_per_box, r.is_default)
    return sorted(((s, q) for s, (q, _d) in best.items()), key=lambda x: (-x[1], x[0]))


def wrap(items, prefix="Fits: ", width=LINE_CHARS, max_lines=MAX_LINES) -> list[str]:
    """Позиції «CA-SMA-1 × 10» через « · » рядками до width символів; не вмістилось — останній «+N more»."""
    parts = [f"{sku} × {qty}" for sku, qty in items]
    if not parts:
        return []

    def text(row, first):
        return (prefix if first else "") + " · ".join(row)

    rows = [[]]
    for p in parts:
        if rows[-1] and len(text(rows[-1] + [p], len(rows) == 1)) > width:
            rows.append([p])
        else:
            rows[-1].append(p)
    if len(rows) <= max_lines:
        return [text(r, i == 0) for i, r in enumerate(rows)]
    shown, hidden = rows[:max_lines], sum(len(r) for r in rows[max_lines:])
    last = shown[-1]
    while last and len(text(last, max_lines == 1) + f" · +{hidden + 1} more") > width:
        last.pop()
        hidden += 1
    lines = [text(r, i == 0) for i, r in enumerate(shown)]
    lines[-1] += (" · " if last else "") + f"+{hidden} more"
    return lines


def build(material) -> dict:
    from shipping.services import box_names
    m = material
    folds = m.heights[1:]
    size = f"{_n(m.length_cm)}×{_n(m.width_cm)}×{_n(m.height_cm)} cm"
    if folds:
        size += " · ↧ " + "/".join(_n(h) for h in folds)
    size += f" · {_n(box_names.litres(m))} L"
    info = [f"Class {box_names.class_of(m)}"]
    if m.tare_weight_kg:
        info.append(f"tare {_n(m.tare_weight_kg)} kg")
    if m.max_weight_kg:
        info.append(f"max {_n(m.max_weight_kg)} kg")
    items = contents(m)
    if items:
        lines = wrap(items)
    else:
        note = (m.notes or "").strip().splitlines()
        lines = [note[0][:LINE_CHARS * 2]] if note else []
    return {"title": m.name or m._dim_str(), "code": box_names.barcode_value(m), "size": size,
            "info": " · ".join(info), "lines": lines, "items": items, "font_size": FONT_SIZE}


def render(data) -> str:
    """XML наклейки (.dymo) з шаблону."""
    xml = TEMPLATE_PATH.read_text(encoding="utf-8-sig")
    block = re.search(r"[ \t]*<!--LINE-->\r?\n(.*?)[ \t]*<!--/LINE-->\r?\n", xml, re.S)
    span = block.group(1).replace("{{FS}}", str(data["font_size"]))
    spans = "".join(span.replace("{{LINE}}", escape(line)) for line in (data["lines"] or [" "]))
    xml = xml[:block.start()] + spans + xml[block.end():]
    for marker, key in (("{{TITLE}}", "title"), ("{{CODE}}", "code"), ("{{SIZE}}", "size"), ("{{INFO}}", "info")):
        xml = xml.replace(marker, escape(data[key]))
    return xml
