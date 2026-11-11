"""
suppliers/services/overview.py — 📊 огляд постачальника з бази, без AI і без пошти: закупівлі (скільки, на яку суму
у валюті Minerva, вчасно чи ні, строк від замовлення до отримання), що в них купували (товари), документи закупівель,
запити (RFQ) — скільки відповіли й скільки вибрано, і оцінка Minerva 0–100 з поясненням.

Пошта (швидкість відповіді, проблеми) — suppliers/services/mailscan.py, її частина оцінки додається там.
"""
from collections import defaultdict
from decimal import Decimal
from statistics import median

from django.db.models import Count, Q
from django.utils import timezone

TOP_PRODUCTS = 12
RECENT_ORDERS = 25


def purchases(supplier) -> dict:
    """Закупівлі постачальника: {count, open, cancelled, spend (Money), last, avg_days, median_days, on_time,
    late, late_list, orders: [...], products: [...], documents: N, by_year: {рік: Money}}."""
    from inventory.models import PurchaseOrder
    from sales.revenue import Money, Rates, home_currency
    home = home_currency()
    rates = Rates(home)
    pos = list(PurchaseOrder.objects.filter(supplier=supplier).prefetch_related("lines__product", "documents")
               .order_by("-order_date", "-pk"))
    live = [po for po in pos if po.status != PurchaseOrder.Status.CANCELLED]
    spend, by_year = Money(home), defaultdict(lambda: Money(home))
    days, on_time, late, late_list = [], 0, 0, []
    products = {}
    docs = 0
    for po in live:
        amount = po.total_price
        if amount is None:
            amount = sum(((ln.unit_price or 0) * (ln.qty_ordered or 0) for ln in po.lines.all()), Decimal(0))
        spend.add(amount, po.currency, rates)
        by_year[po.order_date.year if po.order_date else 0].add(amount, po.currency, rates)
        docs += len(po.documents.all())
        if po.received_date and po.order_date:
            days.append((po.received_date - po.order_date).days)
        if po.received_date and po.expected_date:
            if po.received_date <= po.expected_date:
                on_time += 1
            else:
                late += 1
                late_list.append({"po": po, "days": (po.received_date - po.expected_date).days})
        elif po.expected_date and po.status in (PurchaseOrder.Status.ORDERED, PurchaseOrder.Status.PARTIAL) \
                and po.expected_date < timezone.localdate():
            late += 1
            late_list.append({"po": po, "days": (timezone.localdate() - po.expected_date).days, "open": True})
        for ln in po.lines.all():
            key = ln.product_id or f"d:{(ln.description or '').strip().lower()[:80]}"
            row = products.setdefault(key, {"product": ln.product, "name": ln.description or "", "qty": Decimal(0),
                                            "orders": set(), "last_price": None, "currency": "", "last": None})
            row["qty"] += ln.qty_ordered or 0
            row["orders"].add(po.pk)
            if ln.unit_price is not None and (row["last"] is None or (po.order_date and po.order_date > row["last"])):
                row["last_price"], row["currency"], row["last"] = ln.unit_price, ln.currency or po.currency, po.order_date
    top = sorted(products.values(), key=lambda r: (-len(r["orders"]), -r["qty"]))[:TOP_PRODUCTS]
    for r in top:
        r["orders"] = len(r["orders"])
    return {
        "count": len(live), "cancelled": len(pos) - len(live), "home": home,
        "open": sum(1 for po in live if po.status in (PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.ORDERED,
                                                      PurchaseOrder.Status.PARTIAL)),
        "spend": spend, "by_year": dict(sorted(by_year.items(), reverse=True)),
        "last": live[0].order_date if live else None,
        "first": live[-1].order_date if live else None,
        "avg_days": round(sum(days) / len(days)) if days else None,
        "median_days": round(median(days)) if days else None,
        "on_time": on_time, "late": late, "late_list": late_list[:10],
        "orders": pos[:RECENT_ORDERS], "products": top, "products_total": len(products), "documents": docs,
    }


def requests_stats(supplier) -> dict:
    """Запити (RFQ), що йшли цьому постачальнику: скільки, відповів, пропозицій, відмов, вибрано, мовчить."""
    from suppliers.models import SupplierRequestRecipient as R
    qs = R.objects.filter(supplier=supplier).exclude(state=R.PLANNED)
    agg = qs.aggregate(
        total=Count("pk"),
        replied=Count("pk", filter=Q(state__in=[R.REPLIED, R.QUOTED, R.DECLINED])),
        quoted=Count("pk", filter=Q(state=R.QUOTED)), declined=Count("pk", filter=Q(state=R.DECLINED)),
        silent=Count("pk", filter=Q(state=R.SILENT)), chosen=Count("pk", filter=Q(chosen=True)),
        waiting=Count("pk", filter=Q(state=R.SENT)))
    hours = [(r.replied_at - r.sent_at).total_seconds() / 3600 for r in qs.filter(replied_at__isnull=False,
                                                                                   sent_at__isnull=False)
             if r.replied_at >= r.sent_at]
    agg["reply_hours"] = round(median(hours), 1) if hours else None
    agg["recent"] = list(qs.select_related("request").order_by("-sent_at")[:10])
    return agg


def score(p: dict, mail: dict | None = None, rfq: dict | None = None) -> dict:
    """Оцінка Minerva 0–100 і з чого вона: вчасність поставок, швидкість відповіді на листи, відповіді на запити,
    проблеми в листах. Немає даних для частини — вона не рахується (а не нуль). → {value, parts, stars}"""
    parts = []
    done = (p or {}).get("on_time", 0) + (p or {}).get("late", 0)
    if done:
        parts.append(("🚚 вчасні поставки", round(100 * p["on_time"] / done), f"{p['on_time']} з {done}"))
    if mail and mail.get("their_reply_hours") is not None:
        h = mail["their_reply_hours"]
        value = 100 if h <= 8 else 85 if h <= 24 else 65 if h <= 48 else 40 if h <= 96 else 20
        parts.append(("✉️ швидкість відповіді", value, f"медіана {h:g} год"))
    if rfq and rfq.get("total"):
        value = round(100 * rfq["replied"] / rfq["total"])
        parts.append(("📨 відповідає на запити", value, f"{rfq['replied']} з {rfq['total']}"))
    if mail and mail.get("problems") is not None and mail.get("messages"):
        n = len(mail["problems"])
        value = 100 if n == 0 else 75 if n <= 2 else 50 if n <= 5 else 25
        parts.append(("⚠️ проблеми в листах", value, f"{n} листів з ознаками проблем"))
    if not parts:
        return {"value": None, "parts": [], "stars": None}
    value = round(sum(v for _n, v, _w in parts) / len(parts))
    return {"value": value, "parts": parts, "stars": max(1, min(5, round(value / 20)))}
