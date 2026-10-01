"""
shipping/services/packing_list_service.py — Packing List з власного Word-шаблону (docxtpl).

Змінні шаблону (див. PL_TEMPLATE_VARS):
  {{ pl_number }}, {{ pl_date }}, {{ ship_to.* }}, {{ parcels_count }}, {{ total_gross }},
  {{ signer_name }}, {{ signer_position }}
  {%p for parcel in parcels %} … {{ parcel.no }} {{ parcel.dims }} {{ parcel.gross }} … {%p endfor %}
  {%tr for it in parcel.lines %} {{ it.pos }} {{ it.description }} {{ it.part_no }}
       {{ it.qty }} {{ it.unit_net }} {{ it.total_net }} {%tr endfor %}
"""
import logging
import shutil
from datetime import date
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE = Path(settings.BASE_DIR) / "shipping" / "templates_docx" / "packing_list_template.docx"

PL_TEMPLATE_VARS = [
    ("{{ pl_number }}",          "Номер пакувального листа",               "17701"),
    ("{{ pl_date }}",            "Дата (YYYY-MM-DD)",                      "2026-10-01"),
    ("{{ ship_to.company }}",    "Компанія отримувача",                    "Voltaat Store for Trading"),
    ("{{ ship_to.street }}",     "Вулиця, будинок",                        "Zone 32, Street 958"),
    ("{{ ship_to.city_line }}",  "Індекс + місто (+ штат)",                "Doha"),
    ("{{ ship_to.country }}",    "Країна англійською",                     "Qatar"),
    ("{{ ship_to.contact }}",    "Контактна особа",                        "Yahya Alhomsi"),
    ("{{ ship_to.phone }}",      "Телефон",                                "+974 7733 4439"),
    ("{{ ship_to.email }}",      "Email",                                  "yalhomsi@voltaat.com"),
    ("{%p for parcel in parcels %}", "Початок блоку коробки (окремий абзац)", ""),
    ("{{ parcel.no }}",          "Номер коробки",                          "1"),
    ("{{ parcels_count }}",      "Всього коробок",                         "2"),
    ("{{ parcel.dims }}",        "Розміри коробки, см",                    "24 x 16 x 5"),
    ("{{ parcel.gross }}",       "Вага брутто коробки, кг",                "0,39"),
    ("{%tr for it in parcel.lines %}", "Рядок-маркер початку таблиці товарів", ""),
    ("{{ it.pos }}",             "Позиція (з 1 у кожній коробці)",         "1"),
    ("{{ it.description }}",     "Опис товару",                            "Antenna"),
    ("{{ it.part_no }}",         "P/N (артикул)",                          "AN240208-01A"),
    ("{{ it.qty }}",             "Кількість",                              "1"),
    ("{{ it.unit_net }}",        "Вага нетто 1 шт, кг",                    "0,100"),
    ("{{ it.total_net }}",       "Вага нетто рядка, кг",                   "0,100"),
    ("{%tr endfor %}",           "Рядок-маркер кінця таблиці",             ""),
    ("{%p endfor %}",            "Кінець блоку коробки (окремий абзац)",   ""),
    ("{{ total_gross }}",        "Загальна вага брутто, кг",               "1,19"),
    ("{{ total_net }}",          "Загальна вага нетто, кг",                "0,60"),
    ("{{ signer_name }}",        "Підписант",                              "Dr. Sergey Sevskiy"),
    ("{{ signer_position }}",    "Посада",                                 "CEO"),
]


# ── Шаблон ────────────────────────────────────────────────────────────────────

def _custom_template_path() -> Path:
    return Path(settings.MEDIA_ROOT) / "packing_list_templates" / "custom.docx"


def active_template_path() -> Path:
    custom = _custom_template_path()
    return custom if custom.exists() else DEFAULT_TEMPLATE


def has_custom_template() -> bool:
    return _custom_template_path().exists()


def save_custom_template(uploaded) -> None:
    """Перевіряє, що шаблон рендериться на тестових даних, і зберігає його."""
    from docxtpl import DocxTemplate
    data = uploaded.read()
    tpl = DocxTemplate(BytesIO(data))
    tpl.render(build_context(_sample_data()))  # кидає виняток, якщо синтаксис зламаний
    path = _custom_template_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def reset_custom_template() -> None:
    _custom_template_path().unlink(missing_ok=True)


def _sample_data() -> dict:
    return {
        "number": "00000", "pl_date": date.today(),
        "ship_to": {"company": "Sample Co", "street": "Street 1", "city_line": "City",
                    "country": "Country", "contact": "Name", "phone": "+00", "email": "a@b.c"},
        "parcels": [{"dims": "10 x 10 x 10", "gross_kg": 1,
                     "lines": [{"description": "Item", "part_no": "PN", "qty": 1, "unit_net_kg": 0.1}]}],
        "signer_name": "Name", "signer_position": "CEO",
    }


# ── Вага товарів ──────────────────────────────────────────────────────────────

def fill_missing_product_weights(pairs) -> list:
    """pairs: [(sku або product_id, кг/шт)]. Записує net_weight_g лише товарам без ваги
    (наявну вагу не перезаписує). Повертає [(sku, грами)] оновлених."""
    from decimal import Decimal, InvalidOperation
    from django.db.models import Q
    from inventory.models import Product

    updated = []
    for key, kg in pairs:
        try:
            grams = (Decimal(str(kg).replace(",", ".")) * 1000).quantize(Decimal("0.0001"))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if grams <= 0 or not key:
            continue
        lookup = Q(pk=key) if isinstance(key, int) else Q(sku=str(key).strip())
        p = Product.objects.filter(lookup).filter(Q(net_weight_g__isnull=True) | Q(net_weight_g=0)).first()
        if p:
            p.net_weight_g = grams
            p.save(update_fields=["net_weight_g"])
            updated.append((p.sku, grams))
    return updated


# ── Префіл із замовлення ──────────────────────────────────────────────────────

def is_placeholder_description(desc: str, sku: str) -> bool:
    d = (desc or "").strip().upper()
    return not d or d == (sku or "").strip().upper()[:35] or d == "GOODS"


def order_line_skus(order) -> list:
    return [(ln.product.sku if ln.product else ln.sku_raw) or ""
            for ln in order.lines.select_related("product").order_by("pk")]


def _customs_by_sku(shipment, order) -> dict:
    """SKU → {"description", "customs_number", "origin_country", "weight"} з митної декларації відправлення.
    weight — лише введена вручну (не «авто» брутто÷к-сть)."""
    items = ((shipment.customs_articles or {}).get("customs_line_items") or []) if shipment else []
    skus = order_line_skus(order)
    by_index = len(skus) == len(items)
    result = {}
    for i, it in enumerate(items):
        sku = it.get("sku") or (skus[i] if by_index else "")
        if not sku:
            continue
        entry = {
            "description":    (it.get("description") or "").strip(),
            "customs_number": (it.get("customs_number") or "").strip(),
            "origin_country": (it.get("origin_country") or "").strip(),
        }
        if it.get("weight") and not it.get("weight_auto"):
            try:
                entry["weight"] = float(it["weight"])
            except (TypeError, ValueError):
                pass
        result[sku] = entry
    return result


def _order_lines(order, shipment=None) -> list:
    from inventory.models import ProductCategory

    order_lines = list(order.lines.select_related("product").order_by("pk"))
    cat_slugs = {ln.product.category for ln in order_lines if ln.product and ln.product.category}
    cats = {c.slug: c for c in ProductCategory.objects.filter(slug__in=cat_slugs)} if cat_slugs else {}
    customs = _customs_by_sku(shipment, order)

    lines = []
    for ln in order_lines:
        p = ln.product
        cat = cats.get(p.category) if p and p.category else None
        sku = (p.sku if p else ln.sku_raw) or ""
        c = customs.get(sku, {})
        # Опис: з митної декларації відправлення, якщо там не заглушка (SKU / «Goods»);
        # інакше — пріоритет build_customs_articles: товар → категорія → назва → SKU
        c_desc = c.get("description", "")
        if is_placeholder_description(c_desc, sku):
            c_desc = ""
        desc = (c_desc or (p.name_export if p else "")
                or (cat.customs_description_de if cat else "") or (p.name if p else "") or sku)
        prod_kg = float((p.net_weight_g / 1000).normalize()) if p and p.net_weight_g else 0
        if prod_kg:
            unit_kg, src = prod_kg, "product"
        elif c.get("weight"):
            unit_kg, src = c["weight"], "customs"
        else:
            unit_kg, src = 0, ""
        lines.append({
            "description": desc,
            "part_no":     sku,
            "qty":         float(ln.qty or 0),
            "unit_net_kg": unit_kg,
            "product_id":  p.pk if p else None,
            "weight_src":  src,
        })
    return lines


def _dims(l, w, h) -> str:
    def n(v):
        f = float(v or 0)
        return str(int(f)) if f == int(f) else f"{f:g}".replace(".", ",")
    return f"{n(l)} x {n(w)} x {n(h)}" if (l or w or h) else ""


def _parcels_from_shipment(shipment, order_lines: list) -> list:
    pkgs = list(shipment.packages.all())
    if not pkgs:
        return [{
            "dims": _dims(shipment.length_cm, shipment.width_cm, shipment.height_cm),
            "gross_kg": float(shipment.weight_kg or 0),
            "lines": order_lines,
        }]
    parcels = []
    by_desc = {ln["description"].lower(): ln for ln in order_lines}
    for i, pkg in enumerate(pkgs):
        dist = (pkg.items_distribution or {}).get("items") or []
        if dist:
            lines = []
            for d in dist:
                idx = d.get("index")
                base = (order_lines[idx] if isinstance(idx, int) and idx < len(order_lines)
                        else by_desc.get((d.get("description") or "").lower(), {}))
                lines.append({
                    "description": base.get("description") or d.get("description", ""),
                    "part_no":     base.get("part_no", ""),
                    "qty":         float(d.get("quantity") or 0),
                    "unit_net_kg": base.get("unit_net_kg", 0),
                    "product_id":  base.get("product_id"),
                    "weight_src":  base.get("weight_src", ""),
                })
        else:
            lines = order_lines if len(pkgs) == 1 and (pkg.quantity or 1) == 1 else []
        for _ in range(max(1, pkg.quantity or 1)):
            parcels.append({
                "dims": _dims(pkg.length_cm, pkg.width_cm, pkg.height_cm),
                "gross_kg": float(pkg.weight_kg or 0),
                "lines": [dict(x) for x in lines],
            })
    return parcels


def order_packaging_rows(order) -> list:
    """«Фактична упаковка» замовлення для звірки з коробками форми.
    actual_weight_g — вага всього рядка (як у shipping/admin.py), ділиться на qty_boxes."""
    rows = []
    for op in order.packaging_used.select_related("packaging").order_by("pk"):
        pm  = op.packaging
        qty = max(1, op.qty_boxes or 1)
        rows.append({
            "label":    str(pm),
            "dims":     _dims(pm.length_cm, pm.width_cm, pm.height_cm),
            "qty":      qty,
            "gross_kg": round(op.actual_weight_g / qty / 1000, 3) if op.actual_weight_g else None,
            "auto":     "🤖" in (op.notes or ""),
        })
    return rows


def latest_shipment(order):
    return order.shipments.exclude(status="cancelled").order_by("-created_at").first()


def build_ship_to(order, shipment=None) -> dict:
    """Отримувач: з відправлення (якщо є), інакше з полів доставки замовлення."""
    from config.country_utils import country_name_en

    if shipment:
        s = shipment
        city_line = " ".join(x for x in (s.recipient_zip, s.recipient_city) if x)
        if s.recipient_state:
            city_line = f"{city_line}, {s.recipient_state}" if city_line else s.recipient_state
        return {
            "company": s.recipient_company or s.recipient_name, "contact": s.recipient_name,
            "street": s.recipient_street, "city_line": city_line,
            "country": country_name_en(s.recipient_country),
            "phone": s.recipient_phone, "email": s.recipient_email,
        }
    city_line = " ".join(x for x in (order.addr_zip, order.addr_city) if x)
    return {
        "company": order.ship_company or order.client or "",
        "contact": order.ship_name or order.contact_name or "",
        "street": order.addr_street or "", "city_line": city_line,
        "country": country_name_en(order.addr_country or order.shipping_region or ""),
        "phone": order.ship_phone or order.phone or "",
        "email": order.ship_email or order.email or "",
    }


def last_signer() -> tuple:
    """Підписант з останнього пакувального листа або інвойсу."""
    from shipping.models import CommercialInvoice, PackingList
    docs = [m.objects.exclude(signer_name="").order_by("-created_at").first()
            for m in (PackingList, CommercialInvoice)]
    docs = [d for d in docs if d]
    if not docs:
        return "", ""
    d = max(docs, key=lambda x: x.created_at)
    return d.signer_name, d.signer_position


def initial_data(order) -> dict:
    """Чернетка форми з даних замовлення та останнього відправлення."""
    shipment = latest_shipment(order)
    order_lines = _order_lines(order, shipment)
    if shipment:
        parcels = _parcels_from_shipment(shipment, order_lines)
    else:
        parcels = [{"dims": "", "gross_kg": 0, "lines": order_lines}]
    signer_name, signer_position = last_signer()
    return {
        "number":          order.order_number or "",
        "pl_date":         date.today(),
        "ship_to":         build_ship_to(order, shipment),
        "parcels":         parcels,
        "signer_name":     signer_name,
        "signer_position": signer_position,
        "shipment":        shipment,
    }


# ── Контекст шаблону ──────────────────────────────────────────────────────────

def _fmt(v, digits) -> str:
    return f"{float(v or 0):.{digits}f}".replace(".", ",")


def _dec(v):
    from decimal import Decimal, InvalidOperation
    try:
        return Decimal(str(v or 0).replace(",", "."))
    except InvalidOperation:
        return Decimal(0)


def _fmt_exact(v, min_digits) -> str:
    """Без округлення: усі значущі знаки (до 7), але не менше min_digits після коми."""
    from decimal import Decimal
    d = _dec(v).quantize(Decimal("0.0000001")).normalize()
    exp = -d.as_tuple().exponent if d.as_tuple().exponent < 0 else 0
    return f"{d:.{max(exp, min_digits)}f}".replace(".", ",")


def parcel_weight_errors(parcels) -> list:
    """Коробки, де нетто перевищує брутто: ['Parcel 1: нетто 0,25 > брутто 0,2']."""
    errors = []
    for i, p in enumerate(parcels or [], 1):
        gross = _dec(p.get("gross_kg"))
        net = sum((_dec(ln.get("qty")) * _dec(ln.get("unit_net_kg")) for ln in p.get("lines") or []), _dec(0))
        if gross > 0 and net > gross:
            errors.append(f"Parcel {i}: нетто {_fmt_exact(net, 3)} кг > брутто {_fmt_exact(gross, 2)} кг")
    return errors


def _fmt_qty(v) -> str:
    f = float(v or 0)
    return str(int(f)) if f == int(f) else _fmt(f, 2)


def build_context(data: dict) -> dict:
    parcels_raw = data.get("parcels") or []
    parcels, total_gross, total_net = [], _dec(0), _dec(0)
    for i, p in enumerate(parcels_raw, 1):
        lines = []
        for j, ln in enumerate(p.get("lines") or [], 1):
            qty, unit = _dec(ln.get("qty")), _dec(ln.get("unit_net_kg"))
            total_net += qty * unit
            lines.append({
                "pos": j, "description": ln.get("description", ""), "part_no": ln.get("part_no", ""),
                "qty": _fmt_qty(qty), "unit_net": _fmt_exact(unit, 3), "total_net": _fmt_exact(qty * unit, 3),
            })
        gross = _dec(p.get("gross_kg"))
        total_gross += gross
        parcels.append({"no": i, "dims": p.get("dims", ""), "gross": _fmt_exact(gross, 2), "lines": lines})

    pl_date = data.get("pl_date")
    return {
        "pl_number":       data.get("number", ""),
        "pl_date":         pl_date.isoformat() if hasattr(pl_date, "isoformat") else (pl_date or ""),
        "ship_to":         data.get("ship_to") or {},
        "parcels":         parcels,
        "parcels_count":   len(parcels),
        "total_gross":     _fmt_exact(total_gross, 2),
        "total_net":       _fmt_exact(total_net, 3),
        "signer_name":     data.get("signer_name", ""),
        "signer_position": data.get("signer_position", ""),
    }


# ── Генерація ─────────────────────────────────────────────────────────────────

def generate_files(pl) -> None:
    """Рендерить DOCX (+ PDF, якщо є LibreOffice), зберігає в PackingList і в документи замовлення."""
    from docxtpl import DocxTemplate
    from documents.service import _convert_to_pdf

    tpl = DocxTemplate(str(active_template_path()))
    tpl.render(build_context({
        "number": pl.number, "pl_date": pl.pl_date, "ship_to": pl.ship_to, "parcels": pl.parcels,
        "signer_name": pl.signer_name, "signer_position": pl.signer_position,
    }))
    buf = BytesIO()
    tpl.save(buf)

    base = f"Packing List_{pl.number}"
    for f in (pl.docx_file, pl.pdf_file):
        if f:
            f.delete(save=False)
    pl.docx_file.save(f"{base}.docx", ContentFile(buf.getvalue()), save=False)

    try:
        pdf_bytes = _convert_to_pdf(pl.docx_file.path)
    except Exception as e:
        logger.warning("Packing list PDF conversion failed: %s", e)
        pdf_bytes = None
    if pdf_bytes:
        pl.pdf_file.save(f"{base}.pdf", ContentFile(pdf_bytes), save=False)
    pl.save()

    order = pl.sales_order
    if order and order.order_number:
        dest = Path(settings.MEDIA_ROOT) / "orders" / (order.source or "manual") / order.order_number
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pl.docx_file.path, dest / f"{base}.docx")
        if pl.pdf_file:
            shutil.copyfile(pl.pdf_file.path, dest / f"{base}.pdf")
