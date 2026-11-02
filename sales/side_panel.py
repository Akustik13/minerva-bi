"""📌 Панель праворуч для списку замовлень (core/side_panel.py, каркас v2).

Поруч зі списком: 👤 клієнт, 📦 позиції й склад, 🚚 доставка, 💶 гроші й оплата, 📄 документи, ✉️ листування,
📝 нотатки й події, 📑 Angebote й проєкти, 👥 клієнт CRM, 🔁 рухи складу (+ загальні «Історія», «Усі поля»,
«Пов’язане» — core/panel_generic.py). Швидкі зміни — статус у шапці, ⭐, нотатка, трек-номер, кур’єр, строк
відправки — ідуть тим самим шляхом, що «Зберегти» в картці (core/panel_edit.py): склад, CRM, журнал — як із картки.

Правила цього постачальника:
  • склад лише читається (inventory/services/stock.py: order_lines_cover, po_open_qty, order_stock_issues);
    розбіжності виправляє «🔄 Синхронізувати склад» у картці;
  • пошта — лише з власних скриньок людини (crm.views._own_messages), посилання на лист — лише в основній
    скриньці (email_assistant.views._get_account), інакше «🔍 у пошті»; показ тексту тут лист не позначає прочитаним;
  • нотатки й події — без подій-листів (email_in / email_out — теми з чужих скриньок) і лише з правом бачити
    клієнтів CRM;
  • посилання на інші записи — side_panel.admin_link або своя перевірка права (/labels/, /packing-lists/,
    /commercial-invoices/, /offers/ — поза /admin/); Kundennummer лише читається (shop.models);
  • важке — через side_panel.memo (розділ, лічильник і підсумок ділять роботу); теку етикеток — раз на запит.
"""
from __future__ import annotations

import logging
import re
from datetime import date as _date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, urlencode

from django.conf import settings
from django.template.defaultfilters import linebreaksbr
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from core import side_panel as sp
from core.side_panel import Action, Badge, Block, Edit, register

logger = logging.getLogger(__name__)

STATUS_ICONS = {"received": "📥", "processing": "⚙️", "shipped": "🚚", "delivered": "✅", "cancelled": "🚫"}
STATUS_TONES = {"received": "new", "processing": "work", "shipped": "ship", "delivered": "done", "cancelled": "off"}
OPEN_STATUSES = ("received", "processing")                 # ще не відправлено — строк відправки діє
CLOSED_STATUSES = ("shipped", "delivered", "cancelled")    # склад уже не чекає — без «вистачає / бракує»
SHIPMENT_TONES = {"draft": "new", "submitted": "work", "label_ready": "work", "in_transit": "ship",
                  "delivered": "done", "error": "err", "cancelled": "off"}
PAYMENT_TONES = {"failed": "err", "unpaid": "warn", "pending": "warn", "paid": "ok"}
OPEN_OFFER_STATUSES = ("draft", "review", "approved", "sent")

HIDE_COLS = ("note_col", "source_badge", "country_display", "shipped_badge", "items_count", "items_summary",
             "bom_status_summary", "stock_warning", "label_buttons_list")
EXTRA_FIELDS = ("total_price", "currency", "order_confirm_sent_at", "ship_notify_sent_at", "eu_invoice_number",
                "eu_invoice_date", "buyer_vat_id", "ship_vat_id")
SEEN_V1 = {"blocks": ("summary", "lines", "shipping", "documents", "mail", "notes"),
           "actions": ("open", "label", "mail", "shipments", "crm")}

SUBJECT_MIN = 5            # номер замовлення в темі листа шукаємо від 5 символів
TRACKING_MIN = 8           # трек-номер — від 8
MAIL_DAYS_BEFORE = 30      # такі листи — не раніше ніж за 30 днів до дати замовлення
MAIL_MAX, MAIL_TEXT_MAX = 5, 600
SHIPMENTS_MAX, NOTES_MAX, CALENDAR_MAX = 5, 6, 3
OFFERS_MAX, PROJECTS_MAX, MOVES_MAX, KNOWLEDGE_MAX, DOCS_LISTS_MAX = 10, 10, 10, 5, 5
NOTE_BODY_MAX = 1000
CENT = Decimal("0.01")
_DASHES = str.maketrans({chr(c): "-" for c in (0x2013, 0x2014, 0x2010, 0x2011)})   # тире й дефіси Unicode → «-»


# ── дрібниці ───────────────────────────────────────────────────────────────────

def _ui(text):
    """Текст інтерфейсу (перекладається), екранований — без translate="no", на відміну від даних запису."""
    return format_html("{}", text)


def _rev(name, *args):
    try:
        return reverse(name, args=list(args))
    except NoReverseMatch:
        return ""


def _plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _dec(v):
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _grouped(d: Decimal, places: int) -> str:
    q = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    whole, _, frac = f"{abs(q):.{places}f}".partition(".")
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    return ("-" if q < 0 else "") + " ".join(groups) + (f",{frac}" if frac else "")


def _price(v, currency=""):
    """Ціна за одиницю: 2 знаки, а якщо їх більше — до 4 («12,50», «0,0435»)."""
    d = _dec(v)
    if d is None:
        return ""
    d = d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    places = 2 if d == d.quantize(CENT) else max(2, len(format(d.normalize(), "f").partition(".")[2]))
    text = _grouped(d, min(places, 4))
    return f"{text} {currency}" if currency else text


def _size(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{round(n / 1024)} КБ"
    return f"{n / 1024 / 1024:.1f} МБ".replace(".", ",")


def _short_date(d):
    return d.strftime("%d.%m") if d else ""


def _first_lower(text):
    text = str(text or "")
    return text[:1].lower() + text[1:]


def _line_row(left, right="", below=()):
    """Рядок розділу: ліворуч / праворуч + підрядки (усе — готовий HTML)."""
    return format_html('<div class="mvp-line" data-mvp-row><div class="mvp-row"><span>{}</span>{}</div>{}</div>',
                       left, format_html("<span>{}</span>", right) if right else "",
                       format_html_join("", '<div class="mvp-note">{}</div>', ((b,) for b in below if b)))


def _join(items):
    return mark_safe("".join(str(x) for x in items if x))


# ── дані замовлення (memo: розділ, лічильник і підсумок ділять роботу) ─────────

def _lines(o):
    """Позиції — з prefetch queryset адмінки (lines__product), у порядку додавання."""
    return sp.memo(o, "sales:lines", lambda: sorted(o.lines.all(), key=lambda line: line.pk))


def _lines_sum(o) -> Decimal:
    return sum(((_dec(line.line_total()) or Decimal("0")) for line in _lines(o)), Decimal("0"))


def _sum_differs(o, lines_sum) -> bool:
    return bool(lines_sum) and o.total_price is not None and abs(lines_sum - o.total_price) >= CENT


def _source_name(o):
    def load():
        from sales.models import SalesSource
        return SalesSource.objects.filter(slug=o.source).values_list("name", flat=True).first() or o.source or ""
    return sp.memo(o, "sales:source", load)


def _customer(o):
    return sp.memo(o, "sales:customer", lambda: o.crm_customer)


def _can_crm(request) -> bool:
    return sp.allowed("crm.view_customer", request)


def _shop_order(o):
    """Замовлення з сайту (shop.ShopOrder) — один запит разом з акаунтом і Kundennummer; лише читання."""
    def load():
        from shop.models import ShopOrder
        return (ShopOrder.objects.filter(order_id=o.pk)
                .select_related("shop", "customer__customer_number").first())
    return sp.memo(o, "sales:shop", load)


def _kundennummer(info) -> str:
    """Kundennummer покупця з сайту — лише прочитати (shop.numbering.for_crm / display_number видають новий)."""
    acc = getattr(info, "customer", None) if info is not None else None
    if acc is None:
        return ""
    if getattr(info.shop, "is_test", False):
        return f"TEST-{acc.test_number}" if acc.test_number else ""
    number = acc.customer_number
    return str(number.number) if number is not None else ""


def _cover(o):
    def load():
        from inventory.services import stock
        return stock.order_lines_cover(o)
    return sp.memo(o, "sales:cover", load)


def _line_state(o, line):
    """Склад позиції відкритого замовлення: {short, available, held}; відправлене / доставлене / скасоване —
    None. available — доступно на складі, held — що вже тримає це замовлення (своє — не нестача)."""
    if o.status in CLOSED_STATUSES:
        return None
    c = _cover(o).get(line.pk)
    if c is None:
        return None
    short = (line.qty or Decimal("0")) - c["free"]
    return {"short": short if short > 0 else Decimal("0"), "available": c["available"], "held": c["held"]}


def _short_lines(o) -> list:
    out = []
    for line in _lines(o):
        st = _line_state(o, line)
        if st is not None and st["short"] > 0:
            out.append(line)
    return out


def _incoming(o):
    """{товар: {qty, date, code}} — ще не отримано за підтвердженими закупівлями (як «Очікується» складу)."""
    def load():
        from inventory.models import PurchaseOrderLine
        from inventory.services import stock
        out = {}
        pids = {line.product_id for line in _lines(o) if line.product_id}
        if not pids:
            return out
        rows = (PurchaseOrderLine.objects
                .filter(product_id__in=pids, purchase_order__status__in=stock.CONFIRMED_PO_STATUSES)
                .annotate(_po_open=stock.po_open_qty()).filter(_po_open__gt=0)
                .values("product_id", "_po_open", "purchase_order_id", "purchase_order__code",
                        "purchase_order__expected_date"))
        for r in rows:
            cur = out.setdefault(r["product_id"], {"qty": Decimal("0"), "date": None, "code": ""})
            cur["qty"] += r["_po_open"] or Decimal("0")
            when = r["purchase_order__expected_date"]
            if not cur["code"] or (when is not None and (cur["date"] is None or when < cur["date"])):
                cur["date"], cur["code"] = when, r["purchase_order__code"] or f"PO-{r['purchase_order_id']}"
        return out
    return sp.memo(o, "sales:incoming", load)


def _issues(o, request) -> list:
    """Розбіжності складу (що виправить «🔄 Синхронізувати склад» у картці) — лише читання, з правом бачити рухи."""
    if not (o.affects_stock and sp.allowed("inventory.view_inventorytransaction", request)):
        return []

    def load():
        from inventory.services import stock
        return [str(i["text"]) for i in stock.order_stock_issues(o) if i.get("text")]
    return sp.memo(o, "sales:issues", load)


def _shipments(o, request) -> list:
    """Останні відправлення — лише з правом бачити відправлення."""
    if not sp.allowed("shipping.view_shipment", request):
        return []
    return sp.memo(o, "sales:shipments",
                   lambda: list(o.shipments.select_related("carrier").order_by("-pk")[:SHIPMENTS_MAX]))


def _ship_count(o, request):
    """Скільки відправлень у замовлення: зі списку останніх, якщо їх менше за ліміт, інакше — запит."""
    def load():
        if sp.allowed("shipping.view_shipment", request):
            ships = _shipments(o, request)
            if len(ships) < SHIPMENTS_MAX:
                return len(ships)
        return o.shipments.count()
    return sp.memo(o, "sales:ship_count", load)


def _deadline_tone(o) -> str:
    if o.status not in OPEN_STATUSES or not o.shipping_deadline:
        return ""
    days = (o.shipping_deadline - _date.today()).days
    if days < 0:
        return "err"
    return "warn" if days <= 3 else "ok"


# ── етикетки DYMO (labels/_label_actions.html, підключено на сторінці списку) ─

def _label_stems(request):
    """Назви файлів етикеток у LABELS_DIR — один раз на запит (як ProductAdmin.label_btn)."""
    stems = getattr(request, "_sales_label_stems", None)
    if stems is None:
        labels_dir = Path(getattr(settings, "LABELS_DIR", Path(settings.BASE_DIR) / "labels"))
        try:
            stems = frozenset(f.stem.upper() for f in labels_dir.glob("*.dymo"))
        except OSError:
            stems = frozenset()
        try:
            request._sales_label_stems = stems
        except Exception:  # noqa: BLE001
            pass
    return stems


def _label_sku(line) -> str:
    """Артикул етикетки — той, що замовили (DigiKey: Supplier SKU може відрізнятись від товару), як у списку."""
    sku = (line.sku_raw or "").strip() or (line.product.sku if line.product_id else "")
    return (sku or "").translate(_DASHES).strip()


def _has_label_file(stems, sku) -> bool:
    up = sku.upper()
    return any(s == up or (s.startswith(up) and len(s) > len(up) and s[len(up)] in (" ", "_")) for s in stems)


def _label_button(o, line, request):
    if not sp.allowed("module:labels_app", request):
        return ""
    from labels_app.product_label import kind_of
    sku = _label_sku(line)
    kind = kind_of(sku, line.product.category if line.product_id else "") if sku else ""
    if not kind:
        return ""
    found = _has_label_file(_label_stems(request), sku)
    title = ("📄 файл з бази" if found else "🧩 з шаблону — файлу в базі немає") + " · правий клік — меню"
    return format_html(
        '<button type="button" class="mvp-chip" data-svl="{}" data-sku="{}" data-qty="{}" data-order="{}" '
        'data-file="{}" data-svl-act="print" title="{}">🖨 Етикетка</button>',
        kind, sku, max(1, int(line.qty or 1)), o.pk, "1" if found else "", title)


# ── назва, підзаголовок ────────────────────────────────────────────────────────

def title(o):
    parts = [format_html("<b>№ {}</b>", o.order_number)]
    if o.document_type and o.document_type != "SALE":
        parts.append(sp.badge(o.get_document_type_display(), "info"))
    if not o.affects_stock:
        parts.append(sp.badge("не впливає на склад", "off"))
    return format_html_join(" ", "{}", ((p,) for p in parts))


def label(o):
    return f"№ {o.order_number}"


def subtitle(o, request):
    total = sp.money(o.total_price, o.currency) if o.total_price is not None else ""
    return " · ".join(x for x in (o.client or o.contact_name, _source_name(o), total) if x)


# ── 👤 Клієнт ──────────────────────────────────────────────────────────────────

def summary(o, request):
    from shop.countries import name as country_name
    rows = []
    if o.contact_name and o.contact_name != o.client:
        rows.append(("Контакт", o.contact_name))
    if o.email:
        rows.append(("Email", format_html("{} {}", sp.data(o.email), sp.copy(o.email, "Email"))))
    if o.phone:
        tel = re.sub(r"[^\d+]", "", o.phone)
        rows.append(("Телефон", sp.link(f"tel:{tel}", sp.data(o.phone)) if tel else o.phone))
    rows.append(("Країна, місто", ", ".join(x for x in (country_name(o.addr_country) if o.addr_country else "",
                                                       o.addr_city) if x)))
    rows.append(("Дата замовлення", sp.date(o.order_date)))
    rows.append(("Джерело", _source_name(o)))
    if o.document_type and o.document_type != "SALE":
        rows.append(("Тип документа", _ui(o.get_document_type_display())))
    if not o.affects_stock:
        rows.append(("Склад", _ui("Не впливає на склад і статистику"), "warn"))
    rows.append(("VAT ID покупця", o.buyer_vat_id))
    info = _shop_order(o)
    if info is not None:
        rows.append(("Номер на сайті", info.site_ref))
        rows.append(("Kundennummer", _kundennummer(info)))
    crm_line = ""
    if _can_crm(request):
        c = _customer(o)
        if c is not None:
            text = " · ".join(x for x in (c.company or c.name or c.email, c.get_segment_display()) if x)
            crm_line = format_html('<div class="mvp-note">{}</div>', sp.admin_link(request, c, f"👤 {text}"))
    return format_html("{}{}", sp.kv(rows), crm_line)


def _summary_brief(o, request):
    from shop.countries import name as country_name
    return " · ".join(x for x in (o.client or o.contact_name,
                                   country_name(o.addr_country) if o.addr_country else "") if x)


# ── 📦 Позиції й склад ─────────────────────────────────────────────────────────

def _bom_html(product, qty):
    from inventory.utils import get_assembly_status
    try:
        st = get_assembly_status(product, qty)
    except Exception as exc:  # noqa: BLE001 — BOM не прочитався: позиція без нього
        logger.warning("order panel: BOM of product %s failed: %s", product.pk, exc)
        return ""
    if st.get("can_assemble"):
        return format_html('<span class="mvp-ok">🔧 BOM: можна зібрати {}</span>', sp.data(sp.qty(qty)))
    return format_html('<span class="mvp-warn">🔧 {}</span>', sp.data(st.get("reason") or ""))


def _line_html(o, line, request):
    product = line.product if line.product_id else None
    sku = line.sku_raw or (product.sku if product is not None else "") or "?"
    cur = line.currency or o.currency or ""
    head = sp.admin_link(request, product, sku) if product is not None else sp.data(sku)
    st = _line_state(o, line)
    if st is None:
        state = ""
    elif st["short"] > 0:
        state = format_html('<span class="mvp-err">⚠ бракує {}</span>', sp.data(sp.qty(st["short"])))
    else:
        state = format_html('<span class="mvp-ok">{}</span>', "✓ вистачає")
    below = []
    if product is not None and product.name:
        below.append(sp.data(product.name))
    total = _dec(line.line_total()) or Decimal("0")
    if line.unit_price is not None:
        below.append(format_html("{} × {} = {}", sp.data(_price(line.unit_price)), sp.data(sp.qty(line.qty)),
                                 sp.data(sp.money(total, cur))))
    elif total:
        below.append(sp.data(sp.money(total, cur)))
    if st is not None:
        held = format_html(" · це замовлення тримає {}", sp.data(sp.qty(st["held"]))) if st["held"] else ""
        below.append(format_html("вільно {}{}", sp.data(sp.qty(st["available"])), held))
        # закупівлі (код, кількість, строк) — лише з правом бачити закупівлі, як розділ «🚢 Закупівлі» товару
        inc = _incoming(o).get(line.product_id) if sp.allowed("inventory.view_purchaseorder", request) else None
        if inc and inc["qty"] > 0:
            until = format_html(" до {}", sp.data(_short_date(inc["date"]))) if inc["date"] else ""
            below.append(format_html("очікується {} ({}{})", sp.data(sp.qty(inc["qty"])), sp.data(inc["code"]),
                                     until))
        if product is not None and product.bom_type == "key":
            below.append(_bom_html(product, line.qty))
    return format_html(
        '<div class="mvp-line" data-mvp-row><div class="mvp-row"><span>{} <span class="mvp-note">× {}</span></span>'
        '{}</div>{}{}</div>',
        head, sp.data(sp.qty(line.qty)), state,
        format_html_join("", '<div class="mvp-note">{}</div>', ((b,) for b in below if b)),
        _label_button(o, line, request))


def lines(o, request):
    items = _lines(o)
    if not items:
        return ""
    total_qty = sum((line.qty or Decimal("0") for line in items), Decimal("0"))
    lines_sum = _lines_sum(o)
    foot = [format_html("Разом {} шт.", sp.data(sp.qty(total_qty)))]
    if lines_sum:
        foot.append(format_html("сума позицій {}", sp.data(sp.money(lines_sum, o.currency))))
        if _sum_differs(o, lines_sum):
            foot.append(format_html('<span class="mvp-warn">⚠ не збігається з сумою замовлення {}</span>',
                                    sp.data(sp.money(o.total_price, o.currency))))
    issues = _issues(o, request)
    fix = ""
    if issues:
        fix = format_html('{}<div class="mvp-note">виправити — {}</div>',
                          format_html_join("", '<div class="mvp-warn">⚠ {}</div>', ((sp.data(t),) for t in issues)),
                          sp.link(_rev("admin:sales_salesorder_change", o.pk), "🔄 Синхронізувати склад у картці"))
    return format_html('{}<div class="mvp-note">{}</div>{}', _join(_line_html(o, line, request) for line in items),
                       format_html_join(" · ", "{}", ((f,) for f in foot)), fix)


def _lines_badge(o, request):
    n = len(_lines(o))
    if not n:
        return None
    short = len(_short_lines(o))
    if short:
        return Badge(n, "err", f"бракує {short}")
    issues = _issues(o, request)
    if issues:
        return Badge(n, "warn", f"розбіжностей складу: {len(issues)}")
    if o.status in CLOSED_STATUSES:
        return Badge(n)
    return Badge(n, "ok", "усього вистачає")


def _lines_summary(o, request):
    n = len(_lines(o))
    if not n:
        return ""
    text = f"{n} {_plural(n, 'позиція', 'позиції', 'позицій')}"
    short = len(_short_lines(o))
    return f"{text} · бракує {short}" if short else text


# ── 🚚 Доставка ────────────────────────────────────────────────────────────────

def _eta(s) -> str:
    if s.eta_from and s.eta_to and s.eta_from != s.eta_to:
        return f"{_short_date(s.eta_from)}–{_short_date(s.eta_to)}"
    one = s.eta_to or s.eta_from or s.carrier_eta
    return _short_date(one)


def _shipment_html(s, request):
    from api.serializers import carrier_tracking_url
    carrier = s.carrier if s.carrier_id else None
    name = (carrier.name if carrier is not None else "") or "Відправлення"
    head = format_html("{}{}", sp.admin_link(request, s, name),
                       format_html(' <span class="mvp-note">{}</span>', sp.data(s.carrier_service))
                       if s.carrier_service else "")
    below = []
    if s.carrier_status_label:
        below.append(sp.data(s.carrier_status_label))
    if s.tracking_number:
        url = carrier_tracking_url(carrier.carrier_type if carrier is not None else "", s.tracking_number)
        tn = sp.data(s.tracking_number)
        below.append(format_html("трек {} {}", sp.link(url, tn, new_tab=True) if url else tn,
                                 sp.copy(s.tracking_number, "Трек-номер")))
    eta = _eta(s)
    late = mark_safe(' <span class="mvp-warn">⚠ затримка</span>') if s.carrier_delayed else ""
    if eta or late:
        below.append(format_html("{}{}", format_html("очікується {}", sp.data(eta)) if eta else "", late))
    if s.delivered_at:
        below.append(format_html("доставлено {}", sp.data(sp.dt(s.delivered_at))))
    if s.label_url:
        below.append(sp.link(s.label_url, "🏷 PDF", new_tab=True))
    return _line_row(head, sp.badge(s.get_status_display(), SHIPMENT_TONES.get(s.status, "")), below)


def _recipient(o) -> list:
    """Отримувач, як на етикетці: компанія, ім'я, вулиця, індекс і місто, штат, країна, телефон, email, VAT ID."""
    from shop.countries import name as country_name
    out = []
    for x in (o.ship_company or o.client, o.ship_name or o.contact_name, o.addr_street,
              " ".join(x for x in (o.addr_zip, o.addr_city) if x), o.addr_state,
              country_name(o.addr_country) if o.addr_country else "", o.ship_phone or o.phone,
              o.ship_email or o.email, o.ship_vat_id):
        x = (x or "").strip()
        if x and x not in out:
            out.append(x)
    return out


def shipping(o, request):
    rows = []
    deadline = sp.edit_html(request, o, "deadline")
    status = o.deadline_status if o.status in OPEN_STATUSES else None
    tone = _deadline_tone(o)
    if str(deadline).strip() or status:
        rows.append(("Строк відправки", format_html("{} {}", deadline, sp.badge(status["label"], tone)
                                                    if status else ""), tone))
    ships = _shipments(o, request)
    if not ships:
        courier = sp.edit_html(request, o, "courier")
        tracking = sp.edit_html(request, o, "tracking")
        rows.append(("Кур’єр", courier))
        rows.append(("Трек-номер", format_html("{} {}", tracking, sp.copy(o.tracking_number, "Трек-номер"))
                     if str(tracking).strip() else ""))
    top = sp.kv(rows)
    middle = _join(_shipment_html(s, request) for s in ships)
    dates = sp.kv([
        ("Відправлено", sp.date(o.shipped_at)),
        ("Доставлено", sp.dt(o.delivered_at)),
        ("📥 Підтвердження надіслано", sp.dt(o.order_confirm_sent_at)),
        ("📧 Повідомлення про відправку", sp.dt(o.ship_notify_sent_at)),
        ("Lieferschein", o.lieferschein_nr),
        ("Вартість доставки", sp.money(o.shipping_cost, o.shipping_currency) if o.shipping_cost else ""),
    ])
    rec = _recipient(o)
    recipient = ""
    if rec:
        recipient = format_html(
            '<div class="mvp-note">{}</div><div class="mvp-addr" translate="no">{}</div>'
            '<button type="button" class="mvp-copy" data-copy="{}" aria-label="Копіювати адресу" '
            'title="Копіювати адресу">📋 Копіювати адресу</button>',
            "Отримувач (як на етикетці)", format_html_join(mark_safe("<br>"), "{}", ((x,) for x in rec)),
            "\n".join(rec))
    legacy = ""
    raw = "\n".join(x for x in (o.shipping_region, o.shipping_address) if x)
    if raw.strip():
        legacy = format_html('<details class="mvp-det"><summary>{}</summary><div class="mvp-quote" translate="no">'
                             '{}</div></details>', "Сирий текст з імпорту", linebreaksbr(raw))
    return _join((top, middle, dates, recipient, legacy))


def _shipping_badge(o, request):
    n = _ship_count(o, request) if sp.allowed("shipping.view_shipment", request) else 0
    tone, text = "", ""
    if o.status == "delivered":
        tone, text = "ok", "доставлено"
    else:
        t = _deadline_tone(o)
        if t == "err":
            tone, text = "err", "строк відправки минув"
        elif t == "warn":
            tone, text = "warn", "строк відправки скоро"
    if not (n or tone):
        return None
    return Badge(n or None, tone, text)


def _shipping_summary(o, request):
    ships = _shipments(o, request)
    if ships:
        s = ships[0]
        eta = s.eta_to or s.carrier_eta or s.eta_from
        parts = [s.carrier.name if s.carrier_id else "", _first_lower(s.get_status_display()),
                 f"до {_short_date(eta)}" if eta else ""]
    else:
        parts = [o.shipping_courier, o.tracking_number]
        if o.status in OPEN_STATUSES and o.shipping_deadline:
            parts.append(f"відправити до {_short_date(o.shipping_deadline)}")
    return " · ".join(p for p in parts if p)


# ── 💶 Гроші й оплата ─────────────────────────────────────────────────────────

def _approx(o):
    """« ≈ €1,234» — сума у валюті Minerva (sales.revenue), якщо замовлення в іншій валюті; курсу немає —
    так і сказати («+ $100 без курсу» з нулем попереду нічого не пояснює)."""
    from sales.models import SalesOrder
    from sales.revenue import home_currency, revenue
    cur = (o.currency or "").strip().upper()
    home = (home_currency() or "").upper()
    if not cur or cur == home:
        return ""
    m = revenue(SalesOrder.objects.filter(pk=o.pk))
    if not m.total and m.missing:
        return format_html(' <span class="mvp-note">немає курсу {} → {}</span>', sp.data(cur), sp.data(home))
    if not m:
        return ""
    return format_html(' <span class="mvp-note">≈ {}</span>', sp.data(m.full_text))


def money(o, request):
    rows = []
    lines_sum = _lines_sum(o)
    approx = _approx(o)
    if o.total_price is not None:
        rows.append(("Сума", format_html("{}{}", sp.data(sp.money(o.total_price, o.currency)), approx)))
    if lines_sum:
        rows.append(("Сума позицій", format_html("{}{}", sp.data(sp.money(lines_sum, o.currency)),
                                                 approx if o.total_price is None else ""),
                     "warn" if _sum_differs(o, lines_sum) else ""))
    if o.shipping_cost:
        rows.append(("Доставка", sp.money(o.shipping_cost, o.shipping_currency)))
    if o.payment_method:
        rows.append(("Спосіб оплати", _ui(o.get_payment_method_display())))
    if o.payment_status:
        rows.append(("Статус оплати", _ui(o.get_payment_status_display()), PAYMENT_TONES.get(o.payment_status, "")))
    rows.append(("Референс оплати", o.payment_reference))
    if o.eu_invoice_number:
        rows.append(("EU Invoice №", format_html("{}{}", sp.data(o.eu_invoice_number),
                                                format_html(" від {}", sp.data(sp.date(o.eu_invoice_date)))
                                                if o.eu_invoice_date else "")))
    info = _shop_order(o)
    if info is not None:
        if info.vat_rate is not None:
            rows.append(("ПДВ замовлення з сайту", f"{sp.qty(info.vat_rate)} %"))
        nums = info.doc_numbers if isinstance(info.doc_numbers, dict) else {}
        rows.append(("Номери документів", " · ".join(f"{k}: {v}" for k, v in sorted(nums.items()) if v)))
    out = [sp.kv(rows)]
    if sp.allowed("shop.view_shoppayment", request):
        pays = list(o.shop_payments.order_by("-pk")[:3])        # без адрес оплати й ідентифікаторів Stripe
        if pays:
            out.append(format_html('<div class="mvp-note">{}</div>{}', "💳 Онлайн-оплати", sp.table(
                ["Статус", "Сума", "Оплачено"],
                [[_ui(p.get_status_display()), sp.money(p.amount, p.currency), sp.dt(p.paid_at)] for p in pays])))
    if sp.allowed("accounting.view_invoice", request):
        invoices = list(o.invoice_set.prefetch_related("lines").order_by("-pk")[:3])
        if invoices:
            out.append(format_html('<div class="mvp-note">{}</div>{}', "🧾 Рахунки", _join(
                _line_row(sp.admin_link(request, inv, inv.number or f"#{inv.pk}"), _ui(inv.get_status_display()),
                          [format_html("{}{}", sp.data(sp.money(inv.total, inv.currency)),
                                       format_html(" · оплатити до {}", sp.data(sp.date(inv.due_date)))
                                       if inv.due_date else "")])
                for inv in invoices)))
    return _join(out)


def _money_badge(o, request):
    tone = PAYMENT_TONES.get(o.payment_status, "")
    return Badge(None, tone, o.get_payment_status_display()) if tone else None


def _money_summary(o, request):
    total = sp.money(o.total_price, o.currency) if o.total_price is not None else ""
    return " · ".join(x for x in (total, o.get_payment_status_display() if o.payment_status else "") if x)


# ── 📄 Документи ───────────────────────────────────────────────────────────────

def _documents(o) -> list:
    def load():
        from email_assistant.attachments import order_documents
        return order_documents(o)
    return sp.memo(o, "sales:documents", load)


def _doc_lists(o, request):
    """Packing Lists і Commercial Invoices замовлення — сторінки поза /admin/, тож право перевіряємо самі."""
    out = []
    kinds = (("shipping.view_packinglist", "packing_lists", "packing_list_edit", "packing_list_file", "📦 Packing List"),
             ("shipping.view_commercialinvoice", "commercial_invoices", "commercial_invoice_edit",
              "commercial_invoice_file", "🛃 Commercial Invoice"))
    for perm, rel, edit_name, file_name, text in kinds:
        if not sp.allowed(perm, request):
            continue
        for doc in getattr(o, rel).order_by("-pk")[:DOCS_LISTS_MAX]:
            kind = "pdf" if doc.pdf_file else ("docx" if doc.docx_file else "")
            files = sp.link(_rev(file_name, doc.pk, kind), kind.upper(), new_tab=True) if kind else ""
            out.append(_line_row(sp.link(_rev(edit_name, doc.pk), format_html("{} {}", text, sp.data(doc.number))),
                                 files))
    return out


def _knowledge(o, request):
    try:
        from rag_assistant import access
        if not access.has(request.user, "documents"):
            return []
        from rag_assistant.knowledge_items import items_for
        items = list(items_for(request.user, "order", o))[:KNOWLEDGE_MAX]
    except Exception as exc:  # noqa: BLE001 — база знань не ламає розділ
        logger.warning("order panel: knowledge items of %s failed: %s", o.pk, exc)
        return []
    return [_line_row(sp.link(_rev("rag_assistant:knowledge_item", it.pk), sp.data(it.title or f"#{it.pk}"),
                              new_tab=True)) for it in items]


def documents(o, request):
    out = []
    files = _documents(o)
    if files:
        base = f"{settings.MEDIA_URL}orders/{quote(o.source or 'manual')}/{quote(o.order_number)}/"
        out.append(_join(_line_row(sp.link(base + quote(f["name"], safe=""), format_html("📄 {}", sp.data(f["name"])),
                                           new_tab=True), format_html('<span class="mvp-note">{}</span>',
                                                                      _size(f.get("size"))))
                         for f in files))
    gens = []
    for text, name in (("📦 Packing List", "admin:sales_salesorder_doc_packing_list"),
                       ("🧾 Proforma", "admin:sales_salesorder_doc_proforma"),
                       ("✅ Auftragsbestätigung", "admin:sales_salesorder_doc_order_confirmation"),
                       ("🛃 Customs", "admin:sales_salesorder_doc_customs")):
        url = _rev(name, o.pk)                                  # лише GET: PDF на льоту, без ?action=save
        if url:
            gens.append(format_html('<a class="mvp-chip" href="{}" target="_blank" rel="noopener">{}</a>', url, text))
    if gens:
        out.append(format_html('<div class="mvp-note">{}</div><div class="mvp-chips">{}</div>',
                               "Згенерувати PDF", _join(gens)))
    lists = _doc_lists(o, request)
    if lists:
        out.append(_join(lists))
    kb = _knowledge(o, request)
    if kb:
        out.append(format_html('<div class="mvp-note">{}</div>{}', "📎 База знань", _join(kb)))
    return _join(out)


def _documents_badge(o, request):
    n = len(_documents(o))
    return Badge(n) if n else None


def _documents_summary(o, request):
    n = len(_documents(o))
    return f"{n} {_plural(n, 'файл', 'файли', 'файлів')}" if n else ""


# ── ✉️ Листування (лише власні скриньки) ───────────────────────────────────────

def _own(request):
    from crm.views import _own_messages
    return _own_messages(request.user)


def _primary_mailbox(request):
    """pk основної активної скриньки людини — лише її листи відкриває email_assistant:message."""
    cached = getattr(request, "_sales_primary_mailbox", False)
    if cached is False:
        from email_assistant.views import _get_account
        acc = _get_account(request)
        cached = acc.pk if acc is not None else None
        try:
            request._sales_primary_mailbox = cached
        except Exception:  # noqa: BLE001
            pass
    return cached


def _mail_counts(o, request):
    """(листів, непрочитаних) — власні листи, прив'язані до замовлення в хронології CRM; один запит."""
    def load():
        from django.db.models import Count, Q
        d = (_own(request).filter(crm_events__related_order_id=o.pk)
             .aggregate(n=Count("pk", distinct=True), unread=Count("pk", filter=Q(is_read=False), distinct=True)))
        return d["n"] or 0, d["unread"] or 0
    return sp.memo(o, "sales:mail_counts", load)


def _word(term):
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)


def _mail_messages(o, request) -> list:
    """Власні листи замовлення: прив'язані в хронології CRM + з номером замовлення (від 5 символів) або
    трек-номером (від 8) у темі — цілим словом, не раніше ніж за 30 днів до дати замовлення. Нові зверху."""
    def load():
        own = _own(request)
        found = {m.pk: m for m in own.filter(crm_events__related_order_id=o.pk).distinct()
                 .order_by("-sent_at", "-pk")[:MAIL_MAX]}
        terms = []
        number = (o.order_number or "").strip()
        if len(number) >= SUBJECT_MIN:
            terms.append(number)
        tracking = {(o.tracking_number or "").strip()}
        tracking |= {(t or "").strip() for t in o.shipments.exclude(tracking_number="")
                     .values_list("tracking_number", flat=True)[:SHIPMENTS_MAX]}
        terms += sorted(t for t in tracking if len(t) >= TRACKING_MIN and t not in terms)
        since = o.order_date - timedelta(days=MAIL_DAYS_BEFORE) if o.order_date else None
        for term in terms:
            qs = own.filter(subject__icontains=term)
            if since is not None:
                qs = qs.filter(sent_at__date__gte=since)
            rx = _word(term)
            for m in qs.order_by("-sent_at", "-pk")[:20]:
                if m.pk not in found and rx.search(m.subject or ""):
                    found[m.pk] = m
        return sorted(found.values(), key=lambda m: (m.sent_at.timestamp() if m.sent_at else float("-inf"), m.pk),
                      reverse=True)[:MAIL_MAX]
    return sp.memo(o, "sales:mail", load)


def _mail_html(m, request, number, inbox):
    from email_assistant import quotes
    from email_assistant.models import EmailMessage
    sent = m.folder == EmailMessage.FOLDER_SENT
    subject = sp.data(m.subject) if m.subject else _ui("(без теми)")
    if m.account_id == _primary_mailbox(request):
        subject = sp.link(_rev("email_assistant:message", m.pk), subject)
    elif inbox:                                               # інша власна скринька — сторінка листа не відкриє
        subject = format_html("{} {}", subject, sp.link(f"{inbox}?{urlencode({'q': m.subject or number})}",
                                                        "🔍 у пошті"))
    text = quotes.strip_signature(quotes.new_text(m.body_text or "")).strip()
    if len(text) > MAIL_TEXT_MAX:
        text = text[:MAIL_TEXT_MAX].rstrip() + "…"
    details = format_html('<details class="mvp-det"><summary>{}</summary><div class="mvp-quote" translate="no">{}'
                          '</div></details>', "Текст листа", linebreaksbr(text)) if text else ""
    marks = format_html("{}{}", mark_safe('<span class="mvp-unread" role="img" aria-label="Не прочитано" '
                                          'title="Не прочитано">●</span> ') if not (m.is_read or sent) else "",
                        mark_safe('<span role="img" aria-label="Надіслано" title="Надіслано">→</span> ')
                        if sent else "")
    return format_html('<div class="mvp-line" data-mvp-row><div class="mvp-row"><span>{}{}</span>'
                       '<span class="mvp-note">{}</span></div><div>{}</div>{}</div>',
                       marks, sp.data(m.from_name or m.from_email), sp.dt(m.sent_at), subject, details)


def mail(o, request):
    number = (o.order_number or "").strip()
    inbox = _rev("email_assistant:inbox")
    footer = sp.more(f"{inbox}?{urlencode({'q': number})}", f"🔍 Усі листи з № {number}") if inbox and number else ""
    msgs = _mail_messages(o, request)
    if not msgs:
        return format_html('<div class="mvp-empty">{}</div>{}', "Листів про це замовлення у ваших скриньках немає.",
                           footer)
    return format_html("{}{}", _join(_mail_html(m, request, number, inbox) for m in msgs), footer)


def _mail_badge(o, request):
    n, unread = _mail_counts(o, request)
    if not n:
        return None
    return Badge(n, "info", f"непрочитаних: {unread}") if unread else Badge(n)


def _mail_summary(o, request):
    n, unread = _mail_counts(o, request)
    if not n:
        return ""
    text = f"{n} {_plural(n, 'лист', 'листи', 'листів')}"
    return f"{text} · непрочитаних {unread}" if unread else text


# ── 📝 Нотатки й події (лише з правом бачити клієнтів CRM) ─────────────────────

def _notes(o) -> list:
    """Події хронології CRM замовлення — без листів: email_in / email_out без листа (старий fetch_emails) мають
    теми з чужих скриньок."""
    def load():
        from crm.models import CustomerTimeline
        return list(CustomerTimeline.objects.filter(related_order_id=o.pk, email_message__isnull=True)
                    .exclude(event_type__in=("email_in", "email_out")).order_by("-created_at", "-pk")[:NOTES_MAX])
    return sp.memo(o, "sales:notes", load)


def _calendar(o, request) -> list:
    """Свої невиконані події календаря замовлення."""
    if not sp.allowed("module:calendar_app", request):
        return []
    return sp.memo(o, "sales:calendar", lambda: list(
        o.calendar_events.filter(user=request.user, is_done=False).order_by("start_at")[:CALENDAR_MAX]))


def notes(o, request):
    out = []
    for e in _notes(o):
        icon = sp.split_icon(e.get_event_type_display())[0]
        body = (e.body or "").strip()
        clamp = (format_html('<div class="mvp-clamp" data-mvp-clamp translate="no">{}</div>',
                             linebreaksbr(body[:NOTE_BODY_MAX])) if body else "")
        out.append(format_html('<div class="mvp-line" data-mvp-row><div class="mvp-row"><span>{} {}</span>'
                               '<span class="mvp-note">{}</span></div>{}</div>',
                               icon, sp.data(e.title), sp.dt(e.created_at), clamp))
    cal = _calendar(o, request)
    if cal:
        out.append(format_html('<div class="mvp-note">{}</div>', "📅 Мій календар"))
        out += [_line_row(format_html("{} {}", sp.split_icon(ev.get_event_type_display())[0], sp.data(ev.title)),
                          format_html('<span class="mvp-note">{}</span>',
                                      sp.date(ev.start_at) if ev.all_day else sp.dt(ev.start_at)))
                for ev in cal]
    return _join(out)


def _notes_badge(o, request):
    n = len(_notes(o)) + len(_calendar(o, request))
    return Badge(n) if n else None


def _notes_summary(o, request):
    n = len(_notes(o)) + len(_calendar(o, request))
    return f"{n} {_plural(n, 'подія', 'події', 'подій')}" if n else ""


# ── 📑 Angebote й проєкти ──────────────────────────────────────────────────────

def _offers(o, request) -> list:
    """Angebote замовлення й запиту пропозиції (Offer.order / source_quote); /offers/ — поза /admin/."""
    if not sp.allowed("offers.view_offer", request):
        return []

    def load():
        from django.db.models import Q
        from offers.models import Offer
        return list(Offer.objects.filter(Q(order_id=o.pk) | Q(source_quote_id=o.pk))
                    .order_by("-created_at", "-pk")[:OFFERS_MAX])
    return sp.memo(o, "sales:offers", load)


def _projects(o, request) -> list:
    """Підтверджені проєкти замовлення + проєкти його Angebote."""
    if not sp.allowed("projects.view_project", request):
        return []

    def load():
        from django.db.models import Q
        from projects.models import Project, ProjectLink
        return list(Project.objects.filter(Q(links__order_id=o.pk, links__status=ProjectLink.CONFIRMED)
                                           | Q(offers__order_id=o.pk) | Q(offers__source_quote_id=o.pk))
                    .distinct().order_by("-pk")[:PROJECTS_MAX])
    return sp.memo(o, "sales:projects", load)


def links(o, request):
    out = []
    today = _date.today()
    for off in _offers(o, request):
        url = _rev("offers:quick_edit", off.pk)
        number = sp.data(off.display_number)
        late = off.valid_until and off.valid_until < today and off.status in OPEN_OFFER_STATUSES
        valid = (format_html(" · дійсна до {}{}", sp.data(sp.date(off.valid_until)),
                             mark_safe(' <span class="mvp-warn">⚠ минула</span>') if late else "")
                 if off.valid_until else "")
        out.append(_line_row(format_html("📝 {}", sp.link(url, number) if url else number),
                             _ui(off.get_status_display()),
                             [format_html("{}{}", sp.data(sp.money(off.gross_total, off.currency)), valid)]))
    for p in _projects(o, request):
        bits = [format_html("строк {}", sp.data(sp.date(p.due_date))) if p.due_date else "",
                format_html("далі: {}", sp.data(p.next_action)) if p.next_action else ""]
        out.append(_line_row(format_html("📁 {}", sp.admin_link(request, p, f"{p.code} · {p.name}")),
                             _ui(p.get_status_display()), [format_html_join(" · ", "{}", ((b,) for b in bits if b))]))
    if o.source == "digikey":
        out.append(format_html('<div class="mvp-note">{}</div>', sp.link(_rev("admin:sales_salesorder_change", o.pk),
                                                                       "💬 Повідомлення DigiKey — у картці")))
    return _join(out)


def _links_badge(o, request):
    n = len(_offers(o, request)) + len(_projects(o, request))
    return Badge(n) if n else None


# ── 👥 Клієнт CRM ──────────────────────────────────────────────────────────────

def crm(o, request):
    c = _customer(o)
    if c is None:
        return ""
    from crm import utils as crm_utils
    from shop.models import CustomerNumber
    crm_utils.prefetch_stats([c])
    stats = c.card_stats()
    rfm = c.rfm_score()
    last = stats.get("last")
    last_text = ""
    if last:
        days = stats.get("days")
        last_text = format_html("{}{}", sp.data(sp.date(last)),
                                format_html(" · {} дн. тому", sp.data(days)) if days is not None else "")
    rows = [
        ("Клієнт", sp.admin_link(request, c, c.company or c.name or c.email or f"#{c.pk}")),
        ("Сегмент", _ui(c.get_segment_display())),
        ("Тип", _ui("✅ Постійний" if crm_utils.is_repeat(stats.get("count")) else "🔸 Новий")),
        ("Замовлень", str(stats.get("count") or 0)),
        ("Виручка", stats.get("rev")),
        ("Середній чек", stats.get("avg")),
        ("Останнє замовлення", last_text),
        ("RFM", format_html("R{} F{} M{} · {}", rfm.get("R"), rfm.get("F"), rfm.get("M"), rfm.get("segment"))),
        ("Kundennummer", str(CustomerNumber.objects.filter(crm_customer=c).values_list("number", flat=True).first()
                             or "")),
    ]
    if sp.allowed("offers.view_offer", request):
        rows.append(("Відкритих Angebote", str(c.offers.filter(status__in=OPEN_OFFER_STATUSES).count())))
    return sp.kv(rows)


# ── 🔁 Рухи складу замовлення ─────────────────────────────────────────────────

def moves(o, request):
    from django.db.models import Q
    from inventory.models import InventoryTransaction
    from inventory.services import stock
    own = Q(external_key__startswith=stock.order_prefix(o)) | Q(external_key__startswith=f"so-bom:{o.pk}:")
    txs = list(InventoryTransaction.objects.filter(own).select_related("product", "location")
               .order_by("-tx_date", "-pk")[:MOVES_MAX])
    if not txs:
        return ""
    keys = {}
    for tx in txs:
        keys[f"so-cancel:{tx.pk}"] = tx.pk
        keys[f"so-writeoff:{tx.pk}"] = tx.pk
    closed = {}
    for comp in InventoryTransaction.objects.filter(external_key__in=list(keys)):
        what = "↩ повернено" if comp.external_key.startswith("so-cancel:") else "✕ списано без повернення"
        closed.setdefault(keys[comp.external_key], []).append(what)
    rows = []
    for tx in txs:
        kind = _ui(tx.get_tx_type_display())
        if tx.pk in closed:
            kind = format_html('{} <span class="mvp-note">{}</span>', kind, " · ".join(closed[tx.pk]))
        rows.append([sp.dt(tx.tx_date), kind, sp.admin_link(request, tx.product, tx.product.sku), sp.qty(tx.qty),
                     tx.location.code if tx.location_id else ""])
    return sp.table(["Коли", "Рух", "Товар", "К-сть", "Склад"], rows)


# ── швидкі зміни ───────────────────────────────────────────────────────────────

def _status_choices(o, request):
    from sales.models import SalesOrder
    return [(v, f"{STATUS_ICONS[v]} {lab}" if v in STATUS_ICONS else str(lab)) for v, lab in SalesOrder.STATUS_CHOICES]


STOCK_STATUSES = ("cancelled", "shipped", "delivered")   # зміна з / на них рухає склад (inventory/services/stock.py)


def _status_confirm(o, value):
    """Питати, коли статус іде НА або З «Скасовано / Відправлено / Доставлено»: склад рухається так само, як при
    зміні в картці, а скасувати зміну з панелі не можна (що саме зміниться — за режимом складу, тому без обіцянок)."""
    if value == o.status or (value not in STOCK_STATUSES and o.status not in STOCK_STATUSES):
        return ""
    n = o.order_number
    names = dict(o._meta.get_field("status").choices)
    same = "Склад зміниться так само, як при зміні статусу в картці."
    if value == "cancelled":
        return f"Скасувати замовлення № {n}? Товар, який воно тримає, повернеться на склад."
    if o.status == "cancelled":
        return f"Повернути № {n} у роботу («{names.get(value, value)}»)? Товар знову забронюється або спишеться. {same}"
    if value in ("shipped", "delivered"):
        return f"Позначити № {n} як «{names.get(value, value)}»? {same}"
    return f"Повернути № {n} з «{names.get(o.status, o.status)}» у «{names.get(value, value)}»? {same}"


EDITS = [
    Edit("status", "status", "Статус", "choice", place="head", choices=_status_choices, confirm=_status_confirm,
         tones=STATUS_TONES, cells=("status_badge", "shipped_badge", "deadline_display"), hotkey="KeyS"),
    Edit("flag", "is_flagged", "⭐ Важливе", "bool", place="bar", undo=True, cells=("flag_col",), hotkey="KeyF"),
    Edit("note", "internal_note", "Нотатка", "textarea", place="memo", maxlength=500, undo=True,
         cells=("note_col",), hotkey="KeyN"),
    Edit("tracking", "tracking_number", "Трек-номер", "text", undo=True, maxlength=128),
    Edit("courier", "shipping_courier", "Кур’єр", "text", undo=True, maxlength=64),
    Edit("deadline", "shipping_deadline", "Строк відправки", "date", undo=True, cells=("deadline_display",)),
]


# ── швидкі дії ─────────────────────────────────────────────────────────────────

def _compose(o, request):
    email = o.email or o.ship_email
    url = _rev("email_assistant:compose")
    if not (email and url):
        return ""
    return f"{url}?{urlencode({'to': email, 'subject': f'№ {o.order_number}'})}"


def _shipments_list(o, request):
    url = _rev("admin:shipping_shipment_changelist")
    return f"{url}?order__id__exact={o.pk}" if url else ""


def _ship_new(o, request):
    """Нове відправлення — лише для ще не відправленого замовлення без відправлень."""
    if o.status not in OPEN_STATUSES or _ship_count(o, request):
        return ""
    return _rev("admin:shipping_shipment_create", o.pk)


def _crm_url(o, request):
    c = _customer(o)
    if c is None:
        return ""
    from django.contrib import admin as dj_admin
    ma = dj_admin.site._registry.get(type(c))
    try:
        if ma is None or not ma.has_view_or_change_permission(request, c):
            return ""
    except Exception:  # noqa: BLE001 — права не перевірились — закрито
        return ""
    return _rev("admin:crm_customer_change", c.pk)


def _track_url(o, request):
    from api.serializers import carrier_tracking_url
    for s in _shipments(o, request):
        if s.tracking_number:
            url = carrier_tracking_url(s.carrier.carrier_type if s.carrier_id else "", s.tracking_number)
            if url:
                return url
    if o.tracking_number:
        return carrier_tracking_url(o.shipping_courier, o.tracking_number) or ""
    return ""


def _inbox_url(o, request):
    url = _rev("email_assistant:inbox")
    return f"{url}?{urlencode({'q': o.order_number})}" if url and o.order_number else ""


def _with_order(name):
    def href(o, request):
        url = _rev(name)
        return f"{url}?order={o.pk}" if url else ""
    return href


def _order_url(name):
    def href(o, request):
        return _rev(name, o.pk)
    return href


ACTIONS = [
    Action("label", "🏷 Етикетка", _order_url("labels:order"), kind="svl", perm="module:labels_app", hotkey="KeyL",
           attrs=lambda o, request: {"svl": "order", "order": o.pk, "sku": o.order_number, "svl-act": "print"}),
    Action("mail", "✉️ Лист клієнту", _compose, perm="module:email_assistant", hotkey="KeyM",
           reason=lambda o, request: "" if (o.email or o.ship_email) else "Немає email у замовленні"),
    Action("shipments", "🚚 Відправлення", _shipments_list, perm="shipping.view_shipment"),
    Action("ship_new", "🚚 Нове відправлення", _ship_new, default=False, perm="shipping.add_shipment"),
    Action("crm", "👤 Клієнт CRM", _crm_url, default=False, perm="crm.view_customer"),
    Action("track", "📍 Трекінг", _track_url, default=False, new_tab=True),
    Action("inbox", "🔍 Листи в пошті", _inbox_url, default=False, perm="module:email_assistant"),
    Action("oc", "✅ Auftragsbestätigung (PDF)", _order_url("admin:sales_salesorder_doc_order_confirmation"),
           default=False, new_tab=True),
    Action("proforma", "🧾 Proforma (PDF)", _order_url("admin:sales_salesorder_doc_proforma"), default=False,
           new_tab=True),
    Action("pl_new", "📦 Новий Packing List", _with_order("packing_list_new"), default=False,
           perm="shipping.add_packinglist"),
    Action("ci_new", "🛃 Новий Commercial Invoice", _with_order("commercial_invoice_new"), default=False,
           perm="shipping.add_commercialinvoice"),
    Action("open", "📋 Відкрити картку", _order_url("admin:sales_salesorder_change"), default=False),
]


# ── розділи ────────────────────────────────────────────────────────────────────

BLOCKS = [
    Block("summary", "Клієнт", summary, icon="👤", short="Клієнт", summary=_summary_brief, fieldset=1,
          empty="Даних клієнта немає."),
    Block("lines", "Позиції й склад", lines, icon="📦", short="Позиції", badge=_lines_badge, summary=_lines_summary,
          affects=("status",), fieldset=4, empty="Позицій немає."),
    Block("shipping", "Доставка", shipping, icon="🚚", short="Доставка", badge=_shipping_badge,
          summary=_shipping_summary, affects=("tracking", "courier", "deadline", "status"), fieldset=2,
          empty="Доставки ще немає."),
    Block("money", "Гроші й оплата", money, icon="💶", short="Гроші", badge=_money_badge, summary=_money_summary,
          fieldset=0, empty="Сум і оплат немає."),
    Block("documents", "Документи", documents, icon="📄", short="Документи", badge=_documents_badge,
          summary=_documents_summary, fieldset=9, empty="Документів немає."),
    Block("mail", "Листування", mail, icon="✉️", short="Листи", lazy=True, perm="module:email_assistant",
          badge=_mail_badge, summary=_mail_summary),
    Block("notes", "Нотатки й події", notes, icon="📝", short="Нотатки", perm="crm.view_customer",
          badge=_notes_badge, summary=_notes_summary, empty="Нотаток і подій немає."),
    Block("links", "Angebote й проєкти", links, icon="📑", short="Angebote", lazy=True, badge=_links_badge,
          empty="Angebote й проєктів немає."),
    Block("crm", "Клієнт CRM", crm, icon="👥", short="CRM", default=False, lazy=True, perm="crm.view_customer",
          empty="Клієнта в CRM не знайдено."),
    Block("moves", "Рухи складу", moves, icon="🔁", short="Рухи", default=False, lazy=True,
          perm="inventory.view_inventorytransaction", empty="Рухів складу за цим замовленням немає."),
]

PANEL = register(
    "sales.salesorder", title=title, label=label, subtitle=subtitle, blocks=BLOCKS, actions=ACTIONS, edits=EDITS,
    hide_cols=HIDE_COLS, extra_fields=EXTRA_FIELDS, seen_v1=SEEN_V1)
