"""📌 Панель праворуч для списку товарів (core/side_panel.py): склад зараз, хто чекає товар, закупівлі, ціни,
магазини й DigiKey, продажі за 12 міс, параметри, BOM, фото й даташит, рухи складу, Angebote — і швидкі дії.

Числа — з тих самих сервісів, що картка й звіт по товару:
  • склад — inventory/services/product_report.stock_now (як «📊 Аналіз запасів»), по локаціях —
    stock.location_breakdown; «Доступно» в шапці й лічильнику — анотації queryset ProductAdmin (_available,
    _reserved, _in_cart_qty, _last_movement), без запитів;
  • хто чекає — stock.open_order_lines, «Очікується» — stock.po_open_qty (лише незакриті позиції закупівель);
  • продажі — product_report.kpis / sales_series, ступені цін Minerva / DigiKey — product_report.tiers_side_by_side.
Важке рахується раз на запит (side_panel.memo): лічильник, підсумок і розділ ділять одне обчислення.
Без мережі: DigiKey — лише збережене в Minerva (лістинг, дорожня карта публікації bots.services.dk_publish);
bots.admin_prices.product_panel (форма й скрипт) і dk_prices.row_for (може читати онлайн-курс) тут не викликаються.
Змінити з панелі можна лише «Точку дозамовлення» й «Термін поставки» — мінімальною формою (core/panel_edit.py),
тож базові ціни й решта картки не зачіпаються. «Історія» — LogEntry; повної історії Django в товару немає
(history_view показує рух складу), тому history_link=False.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.html import conditional_escape, format_html
from django.utils.safestring import mark_safe

from core import side_panel as sp
from core.side_panel import Action, Badge, Block, Edit, register

MODEL = "inventory.product"
ZERO = Decimal("0")

UNITS = {"piece": "шт.", "meter": "м", "kilogram": "кг", "liter": "л", "set": "компл."}
LINE_STATES = {"reserved": "🔒 резерв", "pending": "⏳ ще не списано", "deducted": "✓ списано"}
PO_STATES = {"draft": "у кошику", "ordered": "замовлено", "partial": "частково отримано"}
STOCK_TONES = {"critical": "err", "warning": "warn", "low": "warn", "ok": "ok", "no_sales": ""}
LIFECYCLE_TONES = {"nrnd": "warn", "discontinued": "err"}
TX_LABELS = {"Incoming": "➕ Прихід", "Outgoing": "➖ Витрата", "Adjustment": "± Коригування", "Reserved": "🔒 Резерв"}
DK_STATES = {"published": "виставлено", "staged": "очікує затвердження", "error": "помилка синхронізації",
             "draft": "чернетка"}
PARAMS_SHOWN = 10        # технічних параметрів одразу, решта — «ще N»
PROBLEMS_SHOWN = 3
SHIPMENTS_SHOWN = 3
MOVES_SHOWN = 8
OFFERS_SHOWN = 5
TOP_CUSTOMERS = 3
_ABBR = re.compile(r"\(([^()]{2,12})\)\s*$")         # «… (NRND)» → «NRND»


# ── дрібні помічники (усе екранується) ─────────────────────────────────────────

def _memo(p, key, fn):
    return sp.memo(p, f"inv:{key}", fn)


def _num(v) -> Decimal:
    if v is None or v == "" or isinstance(v, bool):
        return ZERO
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return ZERO


def _unit(p) -> str:
    return UNITS.get(getattr(p, "unit_type", ""), "шт.")


def _q(p, v) -> str:
    """Кількість з одиницею товару: «17 шт.», «2,5 м»."""
    return f"{sp.qty(_num(v))} {_unit(p)}"


def _ui(text):
    """Текст інтерфейсу (не дані запису) — екранований, перекладається разом з інтерфейсом."""
    return format_html("{}", text)


def _tone(text, tone):
    return format_html('<span class="mvp-{}">{}</span>', tone, text) if tone else conditional_escape(text)


def _dot(parts):
    """Частини через « · » (порожні пропускаються)."""
    items = [conditional_escape(x) for x in parts if x is not None and str(x).strip()]
    return mark_safe(" · ".join(str(i) for i in items))


def _join(items):
    return mark_safe("".join(str(conditional_escape(x)) for x in items if x))


def _line(head, right="", note="", tone=""):
    """Рядок списку: зліва головне, справа число, під ним примітка."""
    return format_html(
        '<div class="mvp-line" data-mvp-row><div class="mvp-row"><span>{}</span><span{}>{}</span></div>{}</div>',
        head, format_html(' class="mvp-{}"', tone) if tone else "", right,
        format_html('<div class="mvp-note">{}</div>', note) if note else "")


def _group(title, body):
    return format_html('<div class="mvp-fg"><h4 class="mvp-fg-h"><span>{}</span></h4>{}</div>', title, body)


def _pct(v) -> str:
    return "" if v is None else f"{float(v):+.1f} %".replace(".", ",")


def _url(name, *args, query="") -> str:
    try:
        url = reverse(name, args=list(args))
    except NoReverseMatch:
        return ""
    return f"{url}?{query}" if query else url


def _fieldset(part):
    """Номер групи полів картки товару за частиною назви (↗ «Відкрити в картці»); немає — None."""
    try:
        from inventory.admin import ProductAdmin
        for i, (name, _opts) in enumerate(ProductAdmin.fieldsets or ()):
            if name and part in str(name):
                return i
    except Exception:  # noqa: BLE001 — без якоря розділ однаково працює
        pass
    return None


# ── спільні обчислення (раз на запит) ──────────────────────────────────────────

def _home(p) -> str:
    def load():
        from inventory.services.base_prices import home_currency
        return home_currency()
    return _memo(p, "home", load)


def _rates(p):
    def load():
        from sales.revenue import Rates
        return Rates(_home(p))
    return _memo(p, "rates", load)


def _stock_now(p) -> dict:
    """product_report.stock_now — ті самі числа, що «📊 Аналіз запасів» картки; продажі вже пораховані — звідти."""
    kpis = (p.__dict__.get("_mvp_memo") or {}).get("inv:kpis")
    if kpis:
        return kpis["stock"]

    def load():
        from inventory.services import product_report
        return product_report.stock_now(p, timezone.localdate())
    return _memo(p, "stock", load)


def _annotated(p, attr, key):
    """Анотація списку товарів (0 запитів), без неї — з stock_now."""
    value = getattr(p, attr, None)
    return _num(value) if value is not None else _num(_stock_now(p)[key])


def _available(p) -> Decimal:
    return _annotated(p, "_available", "available")


def _reserved(p) -> Decimal:
    return _annotated(p, "_reserved", "reserved")


def _in_cart(p) -> Decimal:
    value = getattr(p, "_in_cart_qty", None)
    if value is not None:
        return _num(value)

    def load():
        from django.db.models import Sum
        from inventory.models import PurchaseOrderLine
        return (PurchaseOrderLine.objects.filter(product=p, purchase_order__status="draft")
                .aggregate(t=Sum("qty_ordered"))["t"]) or ZERO
    return _num(_memo(p, "cart", load))


def _last_movement(p):
    if hasattr(p, "_last_movement"):
        return p._last_movement

    def load():
        from django.db.models import Max
        from inventory.models import InventoryTransaction
        return InventoryTransaction.objects.filter(product=p).aggregate(t=Max("created_at"))["t"]
    return _memo(p, "last_move", load)


def _many_places(p) -> bool:
    """Рухи товару більш ніж на одній локації (1 запит; без цього таблиця «По локаціях» не потрібна)."""
    def load():
        from inventory.models import InventoryTransaction
        return len(InventoryTransaction.objects.filter(product=p).values_list("location_id", flat=True)
                   .distinct().order_by()[:2]) > 1
    return _memo(p, "many_places", load)


def _category_name(p) -> str:
    def load():
        from inventory.models import ProductCategory
        if not p.category:
            return ""
        return ProductCategory.objects.filter(slug=p.category).values_list("name", flat=True).first() or p.category
    return _memo(p, "category", load)


def _can_shops(request) -> bool:
    return sp.allowed("shop.view_shoplisting", request)


def _can_dk(request) -> bool:
    return sp.allowed("bots.view_digikeylisting", request)


def _dk_listing(p, request):
    """Лістинг DigiKey товару (select_related у списку — без запиту), лише якщо людина бачить лістинги DigiKey."""
    if not _can_dk(request):
        return None
    from shop.badges import dk_listing
    return dk_listing(p)


def _kpis(p, request) -> dict:
    def load():
        from inventory.services import product_report
        return product_report.kpis(p, timezone.localdate(), _rates(p), months=12, listing=_dk_listing(p, request))
    return _memo(p, "kpis", load)


def _dk_currency(p) -> str:
    """Валюта цін офера DigiKey (product_report.dk_currency — без мережі)."""
    def load():
        from inventory.services.product_report import dk_currency
        return dk_currency()
    return _memo(p, "dk_currency", load)


def _tiers(p, request) -> dict:
    kpis = (p.__dict__.get("_mvp_memo") or {}).get("inv:kpis")
    if kpis:
        return kpis["tiers"]

    def load():
        from inventory.services import product_report
        listing = _dk_listing(p, request)
        return product_report.tiers_side_by_side(p, _rates(p), listing=listing,
                                                 dk_cur=_dk_currency(p) if listing is not None else None)
    return _memo(p, "tiers", load)


def _open_lines(p) -> list:
    def load():
        from inventory.services import stock
        return stock.open_order_lines([p.pk])
    return _memo(p, "open_lines", load)


def _po_lines(p) -> list:
    """Незакриті позиції закупівель (draft / ordered / partial) з тим, що ще не отримано (po_open_qty > 0)."""
    def load():
        from django.db.models import F, Prefetch
        from inventory.models import IncomingShipment, PurchaseOrderLine
        from inventory.services import stock
        ships = IncomingShipment.objects.select_related("carrier").defer("tracking_events")   # без запиту на кожне
        return list(PurchaseOrderLine.objects
                    .filter(product=p, purchase_order__status__in=stock.OPEN_PO_STATUSES)
                    .annotate(open_qty=stock.po_open_qty()).filter(open_qty__gt=0)
                    .select_related("purchase_order__supplier")
                    .prefetch_related(Prefetch("purchase_order__incoming_shipments", queryset=ships))
                    .order_by(F("purchase_order__expected_date").asc(nulls_last=True), "purchase_order_id", "pk"))
    return _memo(p, "po_lines", load)


def _bom(p) -> dict:
    def load():
        from inventory.utils import get_bom_analysis
        return get_bom_analysis(p)
    return _memo(p, "bom", load)


def _roadmap(p, listing) -> dict:
    def load():
        from bots.services import dk_publish
        return dk_publish.roadmap(listing)
    return _memo(p, "roadmap", load)


def _shop_listings(p) -> list:
    """Позиції магазинів (prefetch shop_listings__shop у списку) — порядок як у бейджах."""
    return sorted(p.shop_listings.all(), key=lambda l: (not l.shop.is_active, not l.shop.is_default, l.shop.name))


def _margin(p):
    """(маржа %, tone) — як у позиціях Angebote: (продаж − закупка) / продаж; немає цін — None."""
    sale, purchase = p.sale_price, p.purchase_price
    if sale is None or purchase is None or not sale:
        return None
    pct = ((Decimal(sale) - Decimal(purchase)) / Decimal(sale) * 100).quantize(Decimal("0.1"))
    return pct, ("err" if pct < 10 else "warn" if pct < 25 else "ok")


def _abbr(label) -> str:
    m = _ABBR.search(str(label or ""))
    return m.group(1) if m else str(label or "")


def _label_kind(p) -> str:
    def load():
        from labels_app.product_label import kind_of
        return kind_of(p.sku, p.category)
    return _memo(p, "label_kind", load)


def _label_file(p) -> bool:
    """Чи є готовий файл етикетки в LABELS_DIR — те саме правило, що колонка «🏷️ Друк» (ProductAdmin.label_btn)."""
    def load():
        from pathlib import Path
        from django.conf import settings
        sku = (p.sku or "").strip().upper()
        if not sku:
            return False
        folder = Path(getattr(settings, "LABELS_DIR", Path(settings.BASE_DIR) / "labels"))
        try:
            stems = {f.stem.upper() for f in folder.glob("*.dymo")}
        except Exception:  # noqa: BLE001 — немає теки: етикетка з шаблону
            return False
        return any(s == sku or (s.startswith(sku) and len(s) > len(sku) and s[len(sku)] in (" ", "_")) for s in stems)
    return _memo(p, "label_file", load)


# ── шапка ──────────────────────────────────────────────────────────────────────

def title(p):
    """Мініатюра, артикул, позначки (життєвий цикл, неактивний) і назва."""
    from inventory import thumbs
    thumb = thumbs.thumb_url(p)
    img = format_html('<img class="mvp-thumb" src="{}" width="40" height="40" alt="">', thumb) if thumb else ""
    marks = []
    if p.lifecycle_status and p.lifecycle_status != "active":
        full = p.get_lifecycle_status_display()
        marks.append(format_html(' <span class="mvp-badge mvp-t-{}" title="{}">{}</span>',
                                 LIFECYCLE_TONES.get(p.lifecycle_status, "warn"), full, _abbr(full)))
    if not p.is_active:
        marks.append(format_html(" {}", sp.badge("неактивний", "off")))
    name = format_html(' <span class="mvp-note">{}</span>', p.name) if p.name else ""
    return format_html("{}<b>{}</b>{}{}", img, p.sku, _join(marks), name)


def label(p) -> str:
    return " · ".join(x for x in (p.sku, (p.name or "")[:60]) if x)


def subtitle(p, request) -> str:
    parts = [f"Доступно {_q(p, _available(p))}"]
    category = _category_name(p)
    if category:
        parts.append(category)
    if p.sale_price is not None:
        parts.append(sp.money(p.sale_price, _home(p)))
    return " · ".join(parts)


# ── 1. Склад зараз ─────────────────────────────────────────────────────────────

def stock_block(p, request):
    from inventory.services import stock
    s = _stock_now(p)
    available, reserved, incoming = _num(s["available"]), _num(s["reserved"]), _num(s["incoming"])
    point = p.reorder_point or 0
    tone = "err" if available <= 0 else ("warn" if available <= point else "ok")
    cart = _in_cart(p)
    reorder = _num(s["reorder_qty"])
    rows = [
        ("На складі", _q(p, s["on_hand"])),
        ("Резерв", _q(p, reserved), "warn" if reserved > 0 else ""),
        ("Доступно", _q(p, available), tone),
        ("Очікується", _q(p, incoming) if incoming else "—"),
        ("У кошику", _q(p, cart) if cart > 0 else None),
        ("Продажі на міс", f"{sp.qty(s['monthly'])} {_unit(p)}" if s["monthly"] else "—"),
        ("Вистачить на", f"{sp.qty(s['months_left'])} міс" if s["months_left"] is not None else "—"),
        ("Стан", _ui(s["status_text"]) if s["status_text"] else None, STOCK_TONES.get(s["status"], "")),
        ("Що замовити", _q(p, reorder) if reorder > 0 else _ui("не потрібно"), "info" if reorder > 0 else ""),
        ("Точка дозамовлення", sp.edit_html(request, p, "reorder_point")),
        ("Термін поставки, днів", sp.edit_html(request, p, "lead_time_days")),
        ("Останній рух", sp.dt(_last_movement(p)) or None),
    ]
    where = ""
    places = _memo(p, "locations", lambda: stock.location_breakdown(p)) if reserved > 0 or _many_places(p) else []
    if len(places) > 1 or (places and reserved > 0):
        body = []
        for r in places:
            name = r.get("location_name") or ""
            place = (format_html('{} <span class="mvp-note">{}</span>', sp.data(r["location"]), sp.data(name))
                     if name and name != r["location"] else (r["location"] or "—"))
            body.append((place, sp.qty(r["on_hand"]), sp.qty(r["reserved"]), sp.qty(r["available"])))
        where = _group("📍 По локаціях", sp.table(("Локація", "На складі", "Резерв", "Доступно"), body))
    return format_html("{}{}", sp.kv(rows), where)


def stock_badge(p, request):
    available, reserved = _available(p), _reserved(p)
    point = p.reorder_point or 0
    if available <= 0 and reserved > 0:
        return Badge(None, "err", f"вільного немає, резерв {_q(p, reserved)}")
    if available <= point:
        if point:
            return Badge(None, "warn", f"доступно {_q(p, available)} — не більше точки дозамовлення ({point})")
        return Badge(None, "warn", "вільного немає")
    return Badge(None, "ok", f"доступно {_q(p, available)}")


def stock_summary(p, request) -> str:
    reserved = _reserved(p)
    text = f"доступно {_q(p, _available(p))}"
    return f"{text} · резерв {_q(p, reserved)}" if reserved > 0 else text


# ── 2. Хто чекає товар ─────────────────────────────────────────────────────────

def demand_block(p, request):
    lines = _open_lines(p)
    if not lines:
        return ""
    today = timezone.localdate()
    out, total = [], ZERO
    for line, state in lines:
        o = line.order
        total += _num(line.qty)
        who = o.client or o.contact_name or o.email or ""
        head = format_html("{}{}", sp.admin_link(request, o, f"№ {o.order_number}"),
                           format_html(" · {}", sp.data(who)) if who else "")
        deadline = o.shipping_deadline
        if deadline and deadline < today:
            when = format_html('<span class="mvp-err">{}</span>', f"⚠ строк відправки минув {sp.date(deadline)}")
        elif deadline:
            when = f"відправити до {sp.date(deadline)}"
        else:
            when = ""
        out.append(_line(head, sp.data(_q(p, line.qty)), _dot([LINE_STATES.get(state, state),
                                                              o.get_status_display(), when])))
    foot = format_html('<div class="mvp-note">{}</div>', f"Разом чекають: {_q(p, total)}")
    return format_html("{}{}", _join(out), foot)


def demand_badge(p, request):
    n = len(_open_lines(p))
    return Badge(n, "info", f"позицій замовлень: {n}") if n else None


def demand_summary(p, request) -> str:
    lines = _open_lines(p)
    if not lines:
        return ""
    orders = len({line.order_id for line, _state in lines})
    total = sum((_num(line.qty) for line, _state in lines), ZERO)
    return f"замовлень: {orders} · {_q(p, total)}"


# ── 3. Закупівлі ───────────────────────────────────────────────────────────────

def _shipment_note(sh):
    carrier = sh.carrier_name or (sh.carrier.name if getattr(sh, "carrier_id", None) and sh.carrier else "")
    tracking = ""
    if sh.tracking_number:
        number = sp.data(sh.tracking_number)
        tracking = format_html("{}{}", sp.link(sh.tracking_url, number, new_tab=True) if sh.tracking_url else number,
                               sp.copy(sh.tracking_number, "Трек-номер"))
    return _dot([format_html("🚚 {}", sp.data(carrier)) if carrier else "🚚", sh.get_status_display(), tracking,
                 f"очікується {sp.date(sh.expected_date)}" if sh.expected_date else ""])


def incoming_block(p, request):
    lines = _po_lines(p)
    if not lines:
        return ""
    out = []
    for line in lines:
        po = line.purchase_order
        supplier = po.supplier.name if po.supplier_id and po.supplier else ""
        head = format_html("{}{}", sp.admin_link(request, po, po.code or str(po)),
                           format_html(" · {}", sp.data(supplier)) if supplier else "")
        right = _q(p, line.open_qty)
        if _num(line.qty_received) > 0:
            right = f"{right} з {sp.qty(line.qty_ordered)}"
        note = _dot([PO_STATES.get(po.status, po.get_status_display()),
                     f"очікується {sp.date(po.expected_date)}" if po.expected_date else ""])
        ships = [sh for sh in po.incoming_shipments.all() if sh.status != "delivered"][:SHIPMENTS_SHOWN]
        if ships:
            note = format_html("{}{}", note, _join(format_html("<br>{}", _shipment_note(sh)) for sh in ships))
        out.append(_line(head, sp.data(right), note, "muted" if po.status == "draft" else ""))
    return _join(out)


def incoming_badge(p, request):
    n = len(_po_lines(p))
    return Badge(n, "info", f"позицій закупівель: {n}") if n else None


def incoming_summary(p, request) -> str:
    lines = _po_lines(p)
    if not lines:
        return ""
    total = sum((_num(line.open_qty) for line in lines), ZERO)
    n = len({line.purchase_order_id for line in lines})
    return f"закупівель: {n} · {_q(p, total)}"


# ── 4. Ціни ────────────────────────────────────────────────────────────────────

def _price_cell(text, is_break):
    if not text or text == "—":
        return "—"
    if is_break:
        return text
    return format_html('<span class="mvp-muted" title="{}">{}</span>', "ціна меншого ступеня", sp.data(text))


def prices_block(p, request):
    from inventory.services.base_prices import tiers as base_tiers
    home = _home(p)
    rows = []
    if p.sale_price is not None:
        rows.append(("Ціна продажу", sp.money(p.sale_price, home)))
    if p.purchase_price is not None:
        margin = _margin(p)
        extra = format_html(' <span class="mvp-{}">{}</span>', margin[1],
                            f"маржа {str(margin[0]).replace('.', ',')} %") if margin else ""
        rows.append(("Ціна закупки", format_html("{}{}", sp.data(sp.money(p.purchase_price, home)), extra)))
    history = _memo(p, "price_history", lambda: p.price_history.first())
    if history is not None:
        rows.append(("Зміна базових цін", format_html(
            "{} · {} · {}", sp.data(sp.dt(history.created_at)), sp.data(history.user_label or "—"),
            history.get_source_display())))
    out = [sp.kv(rows)]
    currency = (p.base_price_currency or home).upper()
    if base_tiers(p) and currency != home.upper():
        ma = sp.admin_for(request, p)
        fix = ""
        if sp.can_change(request, ma, p):
            from inventory.admin_currency import page_url
            fix = format_html(" {}", sp.link(page_url([p.pk]), f"💱 Перевести в {home}"))
        out.append(format_html('<div class="mvp-warn">{}{}</div>',
                               f"⚠ Базові ціни в {currency}, а валюта Minerva — {home}.", fix))
    t = _tiers(p, request)
    if t["rows"]:
        head = ["Від, шт.", f"Minerva, {t['minerva_currency']}"]
        if t["has_digikey"]:
            head += [f"DigiKey, {t['digikey_currency']}", "Різниця"]
        body = []
        for r in t["rows"]:
            row = [str(r["qty"]), _price_cell(r["minerva_text"], r["minerva_break"])]
            if t["has_digikey"]:
                row += [_price_cell(r["digikey_text"], r["digikey_break"]), _pct(r["diff_pct"]) or "—"]
            body.append(row)
        notes = [f"Minerva — {t['minerva_label']}"]
        if t["minerva_rate_missing"]:
            notes.append(f"немає курсу {t['minerva_currency']} → {home}")
        if t["digikey_rate_missing"]:
            notes.append(f"немає курсу {t['digikey_currency']} → {home} — різницю не пораховано")
        out.append(_group("📐 Ступені цін", format_html("{}{}", sp.table(head, body), format_html(
            '<div class="mvp-note">{}</div>', _dot(notes)))))
    return _join(out)


def prices_badge(p, request):
    from inventory.services.base_prices import tiers as base_tiers
    currency = (p.base_price_currency or "").upper()
    if base_tiers(p) and currency and currency != _home(p).upper():
        return Badge(None, "warn", f"базові ціни в {currency}")
    return None


def prices_summary(p, request) -> str:
    from inventory.services.base_prices import tiers as base_tiers
    parts = [sp.money(p.sale_price, _home(p))] if p.sale_price is not None else []
    n = len(base_tiers(p))
    if n:
        parts.append(f"ступенів базових цін: {n}")
    return " · ".join(parts)


# ── 5. Магазини й DigiKey ──────────────────────────────────────────────────────

def _dk_state(listing) -> str:
    if listing.sync_status == "published" and not listing.dk_is_active:
        return "вимкнено на DigiKey"
    return DK_STATES.get(listing.sync_status, listing.get_sync_status_display())


def _channels_perm(request, obj=None) -> bool:
    return _can_shops(request) or _can_dk(request)


def _shops_part(p, request):
    from shop.badges import badges
    listings = _shop_listings(p)
    if not listings:
        return _group("🏪 Магазини", sp.empty("У магазинах товару немає."))
    today = timezone.localdate()
    rows = []
    for l in listings:
        price = l.effective_price
        right = sp.data(sp.money(price, l.shop.currency)) if price is not None else _ui("ціна за запитом")
        discount = _num(l.discount_percent)
        on_sale = discount > 0 and (l.discount_until is None or l.discount_until >= today)
        note = _dot([_tone("показується", "ok") if l.is_visible else _tone("сховано", "muted"),
                     "базові ціни" if l.price_source == "base" else "",
                     f"акція −{sp.qty(discount)} %" if on_sale else "",
                     "магазин вимкнено" if not l.shop.is_active else ""])
        rows.append(_line(sp.admin_link(request, l, l.shop.name), right, note))
    return _group("🏪 Магазини", format_html('<div class="mvp-chips">{}</div>{}', badges(listings, dk=None),
                                            _join(rows)))


def _dk_part(p, request):
    from shop.badges import dk_badge
    listing = _dk_listing(p, request)
    if listing is None:
        publish = ""
        if sp.allowed("bots.add_digikeylisting", request):
            try:
                from bots.admin_from_stock import product_link
                url = product_link(p)
            except Exception:  # noqa: BLE001
                url = _url("admin:bots_digikeylisting_from_stock", query=f"ids={p.pk}")
            publish = sp.more(url, "🔷 Опублікувати на DigiKey")
        return _group("🔷 DigiKey", format_html("{}{}", sp.empty("Товару немає на DigiKey."), publish))
    cur = _dk_currency(p)
    rm = _roadmap(p, listing)
    step = rm.get("current_step")
    if step is None:
        stage = _ui("🟢 на DigiKey — усі кроки пройдено")
    else:                               # що не заповнено — окремим рядком нижче, тут лише відмова / причина
        hint = "" if step.get("key") == "checked" else (step.get("hint") or "")
        stage = _dot([f"{step['icon']} {step['label']}", hint, f"{rm['done']} з {rm['count']} кроків"])
    problems = list(rm.get("problems") or [])
    more = f" … ще {len(problems) - PROBLEMS_SHOWN}" if len(problems) > PROBLEMS_SHOWN else ""
    rows = [
        ("На DigiKey", _ui("активне") if listing.dk_is_active else _ui("вимкнено"),
         "ok" if listing.dk_is_active else "off"),
        ("Залишок на DigiKey", f"{listing.dk_quantity_available} шт."
         if listing.dk_quantity_available is not None else None),
        ("Ціна за 1 шт.", sp.money(listing.dk_price_min, cur) if listing.dk_price_min is not None else None),
        ("MOQ", f"{listing.dk_min_order_qty} шт."),
        ("Відвантаження", f"{listing.dk_lead_time_days} дн."),
        ("Ціни прочитано", sp.dt(listing.price_checked_at) or None),
        ("Публікація", stage, "warn" if step and step.get("state") in ("error", "warn") else ""),
        ("Далі", _ui(rm["next"]) if rm.get("next") else None),
        ("Стан офера", _ui(rm["offer_status"]) if rm.get("offer_status") else None, "warn"),
        ("Не дасть опублікувати", _ui("; ".join(problems[:PROBLEMS_SHOWN]) + more) if problems else None, "warn"),
    ]
    return _group("🔷 DigiKey", format_html('<div class="mvp-chips">{}</div>{}', dk_badge(listing), sp.kv(rows)))


def channels_block(p, request):
    parts = []
    if _can_shops(request):
        parts.append(_shops_part(p, request))
    if _can_dk(request):
        parts.append(_dk_part(p, request))
    return _join(parts)


def channels_badge(p, request):
    n = len(_shop_listings(p)) if _can_shops(request) else 0
    if _dk_listing(p, request) is not None:
        n += 1
    return Badge(n, "", f"каналів продажу: {n}") if n else None


def channels_summary(p, request) -> str:
    parts = []
    if _can_shops(request):
        visible = sum(1 for l in _shop_listings(p) if l.is_visible and l.shop.is_active)
        parts.append(f"у магазинах показується: {visible}")
    listing = _dk_listing(p, request)
    if _can_dk(request):
        parts.append(f"DigiKey: {_dk_state(listing)}" if listing is not None else "DigiKey: немає")
    return " · ".join(parts)


# ── 6. Продажі за 12 міс ───────────────────────────────────────────────────────

def _spark(p, series):
    """Стовпчики продажів по місяцях (inline SVG, без скриптів): висота — штуки, підказка — місяць і число."""
    values = [float(_num(v)) for v in series.get("units") or []]
    labels = list(series.get("labels") or [])
    if not values or not any(values):
        return ""
    top = max(values)
    width, height, gap = 10, 36, 3
    bars = []
    for i, value in enumerate(values):
        h = max(2.0, round(value / top * height, 1)) if value > 0 else 1.0
        month = labels[i] if i < len(labels) else ""
        bars.append(format_html('<rect x="{}" y="{}" width="{}" height="{}" rx="1"{}><title>{}</title></rect>',
                                i * (width + gap), round(height - h, 1), width, h,
                                mark_safe(' opacity="0.3"') if value <= 0 else "",
                                f"{month}: {sp.qty(value)} {_unit(p)}"))
    aria = "Продано по місяцях: " + "; ".join(
        f"{labels[i] if i < len(labels) else i + 1} — {sp.qty(v)}" for i, v in enumerate(values))
    svg = format_html('<svg class="mvp-spark" viewBox="0 0 {} {}" preserveAspectRatio="none" role="img" '
                      'aria-label="{}" fill="currentColor">{}</svg>',
                      len(values) * (width + gap) - gap, height, aria, _join(bars))
    ends = format_html('<div class="mvp-row mvp-note"><span>{}</span><span>{}</span></div>',
                       labels[0] if labels else "", labels[-1] if labels else "")
    return format_html("{}{}", svg, ends)


def _delta(pct):
    if pct is None:
        return ""
    return format_html(' <span class="mvp-{}" title="{}">{}</span>', "ok" if pct >= 0 else "warn",
                       "проти попередніх 12 місяців", _pct(pct))


def sales_block(p, request):
    from inventory.services import product_report
    k = _kpis(p, request)
    if not (_num(k["units_12"]) or _num(k["units_prev"]) or k["orders_12"]):
        return ""
    home = _home(p)
    series = _memo(p, "series", lambda: product_report.sales_series(p, months=12, rates=_rates(p)))
    avg = sp.money(k["avg_price"], home) if k["avg_price"] is not None else "—"
    if k["avg_price_prev"] is not None:
        avg = f"{avg} (рік тому {sp.money(k['avg_price_prev'], home)})"
    rows = [
        ("Продано", format_html("{}{}", sp.data(_q(p, k["units_12"])), _delta(k["units_pct"]))),
        ("Виручка", format_html("{}{}", sp.data(k["revenue_12"].full_text), _delta(k["revenue_pct"]))),
        ("Замовлень", str(k["orders_12"])),
        ("Середня ціна", avg),
        ("Клієнтів", str(k["customers"])),
    ]
    top = []
    can_crm = sp.allowed("crm.view_customer", request)
    for c in (k.get("top_customers") or [])[:TOP_CUSTOMERS]:
        name = (sp.admin_link(request, c["customer"], c["name"]) if c.get("customer") is not None and can_crm
                else sp.data(c["name"]))
        top.append(_line(name, sp.data(_q(p, c["units"])), sp.data(c["revenue"].full_text)))
    parts = [_spark(p, series), sp.kv(rows)]
    if top:
        parts.append(_group("👥 Найбільші клієнти", _join(top)))
    parts.append(sp.more(_url("admin:inventory_product_report", p.pk), "📄 Звіт по товару"))
    return _join(parts)


# ── 7. Параметри ───────────────────────────────────────────────────────────────

def _attr_text(value) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in value.items())
    return str(value)


def _tech_params(p) -> list:
    """Технічні параметри (людські назви) без службових кодів DigiKey — як API (api.serializers.split_attributes)."""
    try:
        from api.serializers import split_attributes
    except ImportError:            # немає REST API — те саме правило тут
        attrs = p.tech_attributes if isinstance(p.tech_attributes, dict) else {}
        return [(str(k), v) for k, v in attrs.items()
                if v not in (None, "", [], {}) and not (str(k)[:1].islower() and " " not in str(k))]
    return list(split_attributes(p.tech_attributes)[0].items())


def params_block(p, request):
    from inventory.models import ProductAlias, ProductComponent
    from shop.countries import name as country_name
    aliases = list(ProductAlias.objects.filter(product=p).order_by("alias").values_list("alias", flat=True))
    older = [sp.admin_link(request, x, x.sku) for x in p.predecessors.order_by("sku")[:5]]
    parents = [format_html("{} × {}", sp.admin_link(request, c.parent, c.parent.sku), sp.qty(c.qty_per))
               for c in ProductComponent.objects.filter(component=p).select_related("parent").order_by("parent__sku")]
    rows = [
        ("Категорія", _category_name(p)),
        ("Виробник", p.manufacturer),
        ("Тип товару", _ui(p.get_kind_display())),
        ("BOM", _ui(p.get_bom_type_display())),
        ("Одиниця", _ui(p.get_unit_type_display())),
        ("Життєвий цикл", _ui(p.get_lifecycle_status_display()), LIFECYCLE_TONES.get(p.lifecycle_status, "")),
        ("Заміна", sp.admin_link(request, p.successor, p.successor.sku) if p.successor_id and p.successor else None),
        ("Попередні версії", _dot(older) if older else None),
        ("Короткий артикул", p.sku_short),
        ("Аліаси", ", ".join(aliases)),
        ("Назва для документів", p.name_export),
        ("HS-код", p.hs_code),
        ("Країна виробника", country_name(p.country_of_origin) if p.country_of_origin else None),
        ("Вага нетто", f"{sp.qty(p.net_weight_g)} г" if p.net_weight_g is not None else None),
        ("Входить у вироби", _dot(parents) if parents else None),
    ]
    out = [sp.kv(rows)]
    attrs = _tech_params(p)
    if attrs:
        shown = sp.kv([(sp.data(k), _attr_text(v)) for k, v in attrs[:PARAMS_SHOWN]])
        rest = attrs[PARAMS_SHOWN:]
        tail = format_html('<details class="mvp-det"><summary>{}</summary>{}</details>', f"ще {len(rest)}",
                           sp.kv([(sp.data(k), _attr_text(v)) for k, v in rest])) if rest else ""
        out.append(_group("📡 Технічні параметри", format_html("{}{}", shown, tail)))
    if p.notes:
        out.append(_group("📝 Нотатки", format_html('<div class="mvp-clamp" data-mvp-clamp translate="no">{}</div>',
                                                   p.notes)))
    return _join(out)


def params_summary(p, request) -> str:
    n = len(_tech_params(p))
    parts = [x for x in (_category_name(p), p.manufacturer) if x]
    if n:
        parts.append(f"параметрів: {n}")
    return " · ".join(parts)


# ── 8. Комплектація (BOM) ──────────────────────────────────────────────────────

def _is_bom(p) -> bool:
    return p.bom_type == "key"


def bom_block(p, request):
    a = _bom(p)
    comps = a.get("components") or []
    if not comps:
        return ""
    body = []
    for c in comps:
        part = c["product"]
        head = format_html("{}{}", sp.admin_link(request, part, part.sku),
                           format_html(' <span class="mvp-note">{}</span>', "необов’язковий") if c["optional"] else "")
        body.append((head, sp.qty(c["qty_per"]), _q(part, c["stock"]), _q(p, c["buildable"])))
    buildable = a.get("buildable_qty") or 0
    neck = a.get("bottleneck")
    rows = [("Можна зібрати", _q(p, buildable), "ok" if buildable > 0 else "err"),
            ("Вузьке місце", sp.admin_link(request, neck, neck.sku) if neck is not None else None)]
    return format_html('{}{}<div class="mvp-note">{}</div>', sp.kv(rows),
                       sp.table(("Компонент", "На 1 шт.", "На складі", "Вистачить на"), body),
                       "За фізичним залишком компонентів (без резервів).")


def bom_badge(p, request):
    a = _bom(p)
    if not a.get("components"):
        return None
    n = a.get("buildable_qty") or 0
    return Badge(None, "ok" if n > 0 else "err", f"можна зібрати {_q(p, n)}")


# ── 9. Фото й даташит ──────────────────────────────────────────────────────────

def media_block(p, request):
    from inventory import thumbs
    thumb = thumbs.thumb_url(p)
    if thumb:
        img = format_html('<img class="mvp-thumb" src="{}" width="96" height="96" alt="{}" loading="lazy">',
                          thumb, f"Фото {p.sku}")
        href = thumbs.image_href(p)
        photo = format_html("{}{}", sp.link(href, img, new_tab=True) if href else img, format_html(
            '<div class="mvp-note">{}</div>', "файл у картці" if p.image and p.image.name else "за посиланням"))
    else:
        photo = format_html('<div class="mvp-muted">{}</div>', "🖼 Фото немає")
    sheet = thumbs.datasheet_href(p)
    if sheet:
        sheet_html = format_html('<div>{} <span class="mvp-note">{}</span></div>',
                                 sp.link(sheet, "📄 Даташит", new_tab=True),
                                 "файл у картці" if p.datasheet_file and p.datasheet_file.name else "за посиланням")
    else:
        sheet_html = format_html('<div class="mvp-muted">{}</div>', "📄 Даташита немає")
    shared = sp.kv([("Фото: спільний файл", p.image_key), ("Даташит: спільний файл", p.datasheet_key)])
    return format_html("{}{}{}", photo, sheet_html, shared)


def media_badge(p, request):
    from inventory import thumbs
    missing = [text for ok, text in ((thumbs.has_image(p), "немає фото"), (thumbs.has_datasheet(p), "немає даташита"))
               if not ok]
    return Badge(None, "warn", "; ".join(missing)) if missing else Badge(None, "ok", "фото й даташит є")


# ── 10. Останні рухи складу ────────────────────────────────────────────────────

def _move_source(tx) -> str:
    """Звідки рух: службові ключі складу (inventory/services/stock.py) — людською мовою, інакше «Документ»."""
    key = tx.external_key or ""
    parts = key.split(":")
    if key.startswith("so:") and len(parts) > 2:
        return f"замовлення № {parts[2]}"
    if key.startswith("so-cancel:"):
        return "повернення при скасуванні замовлення"
    if key.startswith("so-bom:"):
        return "компоненти BOM замовлення"
    if key.startswith("so-writeoff:"):
        return "залишено списаним при скасуванні"
    if key.startswith("po:") and len(parts) > 1:
        return f"закупівля {parts[1]}"
    return tx.ref_doc or ""


def moves_block(p, request):
    from inventory.models import InventoryTransaction
    txs = list(InventoryTransaction.objects.filter(product=p).select_related("location", "performed_by")
               .order_by("-tx_date", "-pk")[:MOVES_SHOWN])
    if not txs:
        return ""
    out = []
    for tx in txs:
        qty = _num(tx.qty)
        amount = f"{'+' if qty > 0 else ''}{_q(p, qty)}"
        user = tx.performed_by
        who = (user.get_full_name() or user.get_username()) if user is not None else ""
        head = _dot([TX_LABELS.get(tx.tx_type, tx.get_tx_type_display()),
                     sp.data(tx.location.code) if tx.location_id else ""])
        tone = "ok" if tx.tx_type == "Incoming" else ("warn" if tx.tx_type == "Reserved" else "")
        out.append(_line(head, sp.data(amount), _dot([sp.dt(tx.tx_date), sp.data(_move_source(tx)), sp.data(who)]),
                         tone))
    out.append(sp.more(_url("admin:inventory_product_history", p.pk), "Увесь рух →"))
    return _join(out)


# ── 11. В Angebote ─────────────────────────────────────────────────────────────

def offers_block(p, request):
    from offers.models import OfferLine
    lines = list(OfferLine.objects.filter(product=p).select_related("offer__customer")
                 .order_by("-offer__created_at", "-pk")[:OFFERS_SHOWN])
    if not lines:
        return ""
    today = timezone.localdate()
    out = []
    for line in lines:
        o = line.offer
        head = sp.link(_url("offers:quick_edit", o.pk), sp.data(o.display_number))
        right = f"{sp.qty(line.qty)} {line.unit or ''}".strip()
        if line.unit_price is not None:
            right = f"{right} × {sp.money(line.unit_price, o.currency)}"
        if o.customer_id and o.customer is not None:
            who = sp.admin_link(request, o.customer, o.customer.company or o.customer.name or str(o.customer))
        else:
            who = sp.data(o.contact_name)
        valid = ""
        if o.valid_until:
            late = o.valid_until < today and o.status not in ("accepted", "rejected", "cancelled")
            valid = (format_html('<span class="mvp-warn">{}</span>', f"⚠ діяла до {sp.date(o.valid_until)}")
                     if late else f"дійсна до {sp.date(o.valid_until)}")
        out.append(_line(head, sp.data(right), _dot([o.get_status_display(), who, sp.date(o.created_at), valid])))
    return _join(out)


def offers_badge(p, request):
    from offers.models import OfferLine
    n = OfferLine.objects.filter(product=p).values("offer_id").distinct().count()
    return Badge(n, "", f"Angebote: {n}") if n else None


# ── дії ────────────────────────────────────────────────────────────────────────

def _label_attrs(p, request) -> dict:
    sku = (p.sku or "").strip()
    kind = _label_kind(p) if sku else ""
    if not kind:
        return {}
    return {"svl": kind, "sku": sku, "qty": 1, "file": "1" if _label_file(p) else "", "svl-act": "print"}


def _label_href(p, request) -> str:
    sku = (p.sku or "").strip()
    if not sku:
        return ""
    return _url("labels:cable" if _label_kind(p) == "cable" else "labels:product", sku)


def _datasheet_href(p, request) -> str:
    from inventory import thumbs
    return thumbs.datasheet_href(p)


def _dk_href(p, request) -> str:
    from shop.badges import dk_listing
    listing = dk_listing(p)
    if listing is not None:
        return _url("admin:bots_digikeylisting_change", listing.pk)
    try:
        from bots.admin_from_stock import product_link
        return product_link(p)
    except Exception:  # noqa: BLE001
        return _url("admin:bots_digikeylisting_from_stock", query=f"ids={p.pk}")


def _dk_prices_href(p, request) -> str:
    from shop.badges import dk_listing
    if dk_listing(p) is None:           # без лістингу звіряти нічого
        return ""
    return _url("admin:bots_digikeypricerun_compare", query=f"products={p.pk}")


def _change_and(perm):
    """Дія списку: право змінювати товари й ще одне право (дія веде в інший розділ)."""
    def check(request, obj=None):
        return sp.allowed("inventory.change_product", request) and sp.allowed(perm, request)
    return check


# ── реєстрація ─────────────────────────────────────────────────────────────────

PANEL = register(
    MODEL, title=title, label=label, subtitle=subtitle,
    blocks=[
        Block("stock", "Склад зараз", stock_block, icon="🗃", short="Склад", badge=stock_badge,
              summary=stock_summary, affects=("reorder_point", "lead_time_days"), fieldset=_fieldset("Availability")),
        Block("demand", "Хто чекає товар", demand_block, icon="🧾", short="Чекають", perm="sales.view_salesorder",
              badge=demand_badge, summary=demand_summary, empty="Відкритих замовлень з цим товаром немає."),
        Block("incoming", "Закупівлі", incoming_block, icon="🚢", perm="inventory.view_purchaseorder",
              badge=incoming_badge, summary=incoming_summary, empty="Незакритих закупівель цього товару немає."),
        Block("prices", "Ціни", prices_block, icon="💲", badge=prices_badge, summary=prices_summary,
              empty="Цін ще немає.", fieldset=_fieldset("Ціни та закупівля")),
        Block("channels", "Магазини й DigiKey", channels_block, icon="🏪", short="Канали", perm=_channels_perm,
              badge=channels_badge, summary=channels_summary, fieldset=_fieldset("Інтернет-магазини")),
        Block("sales", "Продажі за 12 міс", sales_block, icon="📈", short="Продажі", lazy=True,
              perm="sales.view_salesorder", empty="Продажів за два роки не було."),
        Block("params", "Параметри", params_block, icon="🔧", collapsed=True, summary=params_summary,
              fieldset=_fieldset("Технічні атрибути")),
        Block("bom", "Комплектація (BOM)", bom_block, icon="🧩", short="BOM", applies=_is_bom, badge=bom_badge,
              empty="Компонентів у виробу ще немає."),
        Block("media", "Фото й даташит", media_block, icon="🖼", short="Фото", badge=media_badge,
              fieldset=_fieldset("Медіа")),
        Block("moves", "Останні рухи складу", moves_block, icon="🔁", short="Рухи", default=False, lazy=True,
              perm="inventory.view_inventorytransaction", empty="Рухів складу ще немає.",
              fieldset=_fieldset("Рух товару")),
        Block("offers", "В Angebote", offers_block, icon="📑", short="Angebote", default=False, lazy=True,
              perm="offers.view_offer", badge=offers_badge, empty="Цього товару ще немає в Angebote."),
    ],
    actions=[
        Action("report", "📄 Звіт", lambda p, r: _url("admin:inventory_product_report", p.pk)),
        Action("label", "🖨 Етикетка", _label_href, kind="svl", attrs=_label_attrs, perm="module:labels_app",
               hotkey="KeyL"),
        Action("datasheet", "📄 Даташит", _datasheet_href, new_tab=True),
        Action("moves_page", "📜 Рух товару", lambda p, r: _url("admin:inventory_product_history", p.pk),
               perm="inventory.view_inventorytransaction"),
        Action("move", "➕ Рух складу",
               lambda p, r: _url("admin:inventory_inventorytransaction_add", query=f"product={p.pk}"),
               default=False, perm="inventory.add_inventorytransaction"),
        Action("dk", "🔷 DigiKey", _dk_href, default=False, perm="bots.view_digikeylisting"),
        Action("dk_prices", "💲 Звірити з DigiKey", _dk_prices_href, default=False, perm="bots.view_digikeylisting"),
        Action("base_prices", "💰 Базові ціни",
               lambda p, r: _url("admin:inventory_product_base_prices", query=f"ids={p.pk}"),
               default=False, perm="inventory.change_product"),
        Action("shop", "🏪 Асортимент",
               lambda p, r: _url("admin:shop_shoplisting_changelist", query=f"product__id__exact={p.pk}"),
               default=False, perm="shop.view_shoplisting"),
        Action("po_lines", "🚢 Позиції закупівель",
               lambda p, r: _url("admin:inventory_purchaseorderline_changelist", query=f"product__id__exact={p.pk}"),
               default=False, perm="inventory.view_purchaseorderline"),
    ],
    edits=[
        Edit("reorder_point", "reorder_point", "Точка дозамовлення", "number", place="block",
             cells=("reorder_badge", "stock_badge"), undo=True),
        Edit("lead_time_days", "lead_time_days", "Термін поставки, днів", "number", place="block", undo=True),
    ],
    hide_cols=("media_col", "shops_col", "kind", "bom_type", "reserved_qty", "incoming_qty", "buildable_qty",
               "set_stock_link"),
    admin_perms={
        "action_dk_prices": _change_and("bots.view_digikeylisting"),
        "action_dk_from_stock": _change_and("bots.add_digikeylisting"),
        "action_base_from_dk": _change_and("bots.view_digikeylisting"),
        "bulk_sync_digikey_attrs": _change_and("bots.change_digikeylisting"),
    },
    extra_fields=("updated_at", "base_price_currency", "base_prices_source", "base_prices_updated_at",
                  "base_prices_updated_by"),
    history_link=False,
)
