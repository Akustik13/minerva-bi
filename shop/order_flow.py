"""Блок «🚦 Шлях замовлення» картки магазину: кожен статус замовлення з сайту — хто його рухає і що тоді
відбувається (листи, рахунок, склад) за поточними налаштуваннями. Лише читання; посилання — до поля картки
(«↓», data-nt-go — static/admin/js/mv_notify_pick.js) або на сторінку налаштувань («→»).
"""
from __future__ import annotations

from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from .models import Shop

AUTO, PERSON, SITE = "🤖", "👤", "🌐"
OFFLINE_METHODS = ("prepayment", "invoice", "cod")
METHOD_NAMES = {"prepayment": "Vorkasse", "invoice": "рахунок", "cod": "Nachnahme"}


def _url(name: str) -> str:
    try:
        return reverse(name)
    except NoReverseMatch:
        return ""


def _go(field: str, label: str) -> tuple:
    return (field, label)


def _page(name: str, label: str) -> tuple:
    url = _url(name)
    return (url, label) if url else None


def _stock() -> dict:
    """Режим складу: коли товар бронюється / списується (`stock.order_target`)."""
    try:
        from inventory.models import InventorySettings
        s = InventorySettings.get()
    except Exception:  # noqa: BLE001
        return {"mode": "", "reserve": False}
    return {"mode": s.deduct_on, "reserve": bool(getattr(s, "use_reservation", False))}


def _invoice_auto() -> bool:
    try:
        from sales.models_invoice import InvoiceSettings
        return bool(InvoiceSettings.get().auto_send)
    except Exception:  # noqa: BLE001
        return False


def _ship_mail() -> str:
    """Лист покупцю з трек-номером: auto (сам після оформлення UPS) / button / off."""
    try:
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
    except Exception:  # noqa: BLE001
        return "off"
    if not (ns.customer_notify_enabled and ns.email_enabled):
        return "off"
    return "auto" if ns.customer_notify_auto else "button"


def _tracking_on() -> bool:
    try:
        from shipping.models import TrackingRule
        return TrackingRule.objects.filter(enabled=True).exists()
    except Exception:  # noqa: BLE001
        return False


def steps(shop: Shop) -> list[dict]:
    """[{status, who: [(значок, текст)], what: [текст], links: [(поле | адреса, підпис)]}] — по порядку."""
    methods = list(shop.payment_methods or [])
    stripe = "stripe" in methods or (not methods and shop.stripe_enabled)
    offline = [METHOD_NAMES[m] for m in methods if m in OFFLINE_METHODS] or (
        [] if methods else list(METHOD_NAMES.values()))
    st = _stock()
    no_stock = shop.is_test and not shop.test_affects_stock
    invoice = _invoice_auto()
    paid_to = shop.paid_order_status
    stock_page = _page("admin:inventory_inventorysettings_changelist", "Склад → налаштування")
    notify_page = _page("admin:config_notificationsettings_changelist", "Налаштування → Сповіщення")

    def stock_at(status: str) -> str:
        if no_stock:
            return "📦 склад не рухається (тестовий магазин)" if status == "received" else ""
        mode, reserve = st["mode"], st["reserve"]
        if status == "received":
            if reserve:
                return "📦 товар бронюється (резерв)"
            if mode == "creation":
                return "📦 товар списується зі складу одразу"
            return "📦 товар тримається для замовлення (бронь), списання — при «{}»".format(
                "Відправлено" if mode == "shipped" else "Доставлено")
        if status == "shipped" and (reserve or mode == "shipped"):
            return "📦 товар списується зі складу"
        if status == "delivered" and not reserve and mode == "delivered":
            return "📦 товар списується зі складу"
        if status == "cancelled":
            return "📦 товар повертається на склад"
        return ""

    rows = []
    confirm = shop.site_order_confirmation or not shop.site_mail_managed
    rows.append({
        "status": "🆕 Отримано",
        "who": [(SITE, "Сайт — покупець оформив замовлення")],
        "what": [x for x in (
            "✉️ покупцю — «Bestellbestätigung» від сайту" if confirm else "✉️ підтвердження покупцю вимкнено",
            ("🧾 рахунок PDF покупцю — одразу (" + ", ".join(offline) + ")") if invoice and offline else
            ("🧾 рахунок — вручну" if not invoice else ""),
            stock_at("received")) if x],
        "links": [_go("mail_map_col", "Листи"), _go("invoice_auto_send", "Рахунок"), stock_page],
    })
    who = []
    if stripe:
        who.append((AUTO, "Stripe — сам, щойно покупець заплатив"))
    if offline:
        who.append((PERSON, "Ви — " + ", ".join(offline) + ": «Статус оплати» → «Оплачено» в картці замовлення"))
    rows.append({
        "status": "💳 Оплачено",
        "who": who,
        "what": [x for x in (
            "✉️ покупцю — «Zahlung erhalten» (Stripe)" if stripe else "",
            "🧾 рахунок для Stripe — після оплати" if stripe and invoice else "",
            "➡ статус стає «В обробці»" if paid_to else "➡ статус лишається «Отримано»") if x],
        "links": [_go("paid_order_status", "Після оплати — статус")],
    })
    rows.append({
        "status": "⚙️ В обробці",
        "who": ([(AUTO, "Minerva — після оплати")] if paid_to else []) + [(PERSON, "Ви — у картці замовлення")],
        "what": ["збираєте й пакуєте замовлення"],
        "links": [],
    })
    mail = _ship_mail()
    rows.append({
        "status": "🚚 Відправлено",
        "who": [(PERSON, "Ви — оформили посилку й «✅ Відправлено» на сторінці відправлення")],
        "what": [x for x in (
            {"auto": "✉️ покупцю — лист з трек-номером, сам після оформлення UPS",
             "button": "✉️ покупцю — лист з трек-номером кнопкою «Надіслати клієнту»",
             "off": "✉️ листа з трек-номером немає (вимкнено)"}[mail],
            stock_at("shipped")) if x],
        "links": [notify_page],
    })
    tracking = _tracking_on()
    rows.append({
        "status": "✅ Доставлено",
        "who": [(AUTO, "Трекінг перевізника — сам")] if tracking else [(PERSON, "Ви — трекінг не налаштовано")],
        "what": [x for x in (stock_at("delivered"),) if x],
        "links": [_page("admin:shipping_trackingrule_changelist", "Правила трекінгу")],
    })
    rows.append({
        "status": "✖ Скасовано",
        "who": [(PERSON, "Ви — у картці замовлення"), (SITE, "або сайт (скасування через API)")],
        "what": [x for x in (stock_at("cancelled"),) if x],
        "links": [],
    })
    for r in rows:
        r["links"] = [x for x in r["links"] if x]
    return rows


def _links(items) -> str:
    def one(target, label):
        if target.startswith("/"):
            return format_html('<a href="{}">{} →</a>', target, label)
        return format_html('<a href="#" data-nt-go="{}">{} ↓</a>', target, label)
    return format_html_join(" · ", "{}", ((one(*i),) for i in items))


STYLE = mark_safe("<style>"
                    ".form-row.field-order_flow_col .flex-container>label{display:none}"
                    ".form-row.field-order_flow_col .readonly{margin-left:0;width:100%;max-width:none}"
                    ".of-table{font-size:12px;width:100%;border-collapse:collapse}"
                    ".of-table th{text-align:left;font-weight:600;padding:4px 6px;"
                    "border-bottom:1px solid var(--hairline-color,#ddd)}"
                    ".of-table td{padding:5px 6px;vertical-align:top;border-bottom:1px solid var(--hairline-color,#eee)}"
                    ".of-table .of-st{white-space:nowrap;font-weight:600}"
                    ".of-table .of-ln{color:var(--body-quiet-color,#777);font-size:11px;margin-top:2px}"
                    "</style>")


def render(shop: Shop) -> str:
    rows = format_html_join("", '<tr><td class="of-st">{}</td><td>{}</td><td>{}<div class="of-ln">{}</div></td></tr>', (
        (r["status"],
         format_html_join(format_html("<br>"), "{} {}", r["who"]),
         format_html_join(format_html("<br>"), "{}", ((t,) for t in r["what"])) if r["what"] else "—",
         _links(r["links"])) for r in steps(shop)))
    return format_html(
        '{}<table class="of-table"><tr><th>{}</th><th>{}</th><th>{}</th></tr>{}</table>'
        '<div class="of-ln" style="margin-top:6px">{}</div>',
        STYLE, "Статус", "Хто рухає", "Що тоді відбувається", rows,
        "🤖 — Minerva або сервіс сам, 👤 — людина, 🌐 — сайт. Покупець бачить статус у кабінеті на сайті.")
