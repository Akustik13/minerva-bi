"""Ціни й продажі товару — блок «📈 Ціни й продажі» в картці й сторінка «📄 Звіт по товару».

Ціна за 1 шт у часі (price_series) — ступінчасті ряди «з цього моменту діє ця ціна»:
  Minerva — базова ціна найменшого ступеня (зазвичай від 1 шт.): історія ProductPriceHistory (було → стало)
            і поточні Product.base_prices; базових цін немає й не було — «Ціна продажу» (без історії);
  DigiKey — ціна офера за 1 шт.: DigiKeyPriceLog (було → стало, applied_at) і поточні DigiKeyListing.dk_prices
            (валюта — bots.services.dk_prices.currency: API DigiKey її не повертає); лістингу може не бути;
  продано — фактична середня ціна місяця: виручка рядків з ціною / їхні штуки.
Ціна змінилась без запису в історії (ціни DigiKey, прочитані з API) — крок ставиться на час, коли Minerva
це помітила (price_checked_at / base_prices_updated_at), з приміткою.

Усе — у валюті Minerva за поточними курсами «Налаштувань цін» (offers.pricing.fx через sales.revenue.Rates).
Курсу немає — точка не показується (розрив лінії, сума — окремо «+ $… без курсу») і пишеться примітка:
валюти не змішуються.

Продажі (sales_series, kpis) — ті самі правила, що всі звіти: sales.revenue.sale_lines (SALE, не скасоване,
affects_stock), рядки — за товаром (FK), сума рядка — sales.revenue.line_amount у валюті рядка.

build() — усе одним словником: сторінка звіту, блок у картці (tables=False) і, згодом, лист зі звітом.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, F, Max, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from inventory.services.base_prices import normalize
from sales import revenue as rv

ZERO = Decimal("0")
MONTHS = 24                    # період звіту за замовчуванням (місяців, з поточним)
MONTH_CHOICES = (12, 24, 36)
CHANNELS = 3                   # стільки джерел замовлень окремо, решта — «інші»
TOP_CUSTOMERS = 5
LAST_ORDERS = 20
MONTH_NAMES = ["січ", "лют", "бер", "кві", "тра", "чер", "лип", "сер", "вер", "жов", "лис", "гру"]
OTHERS = "інші"
STOCK_STATUS = {      # як «📊 Аналіз запасів» картки (stock.reorder_advice)
    "critical": "🔥 критично — замовляйте зараз", "warning": "⚠️ мало — треба замовити",
    "low": "📉 низько — скоро замовляти", "ok": "✅ запасів достатньо", "no_sales": "💤 продажів не було",
}


# ── Дати ──────────────────────────────────────────────────────────────────────

def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def window(today: date, months: int = MONTHS) -> tuple[date, date]:
    """(перший день першого місяця, перший день наступного після поточного) — months місяців з поточним."""
    first = today.replace(day=1)
    return _add_months(first, -(max(1, months) - 1)), _add_months(first, 1)


def month_list(start: date, end: date) -> list[date]:
    out, d = [], start
    while d < end:
        out.append(d)
        d = _add_months(d, 1)
    return out


def month_label(d: date) -> str:
    """«лис 25»."""
    return f"{MONTH_NAMES[d.month - 1]} {d.year % 100:02d}"


def _month_key(value):
    """TruncMonth → date першого дня місяця (PostgreSQL може віддати datetime)."""
    if hasattr(value, "date") and callable(value.date):
        value = value.date()
    return value.replace(day=1) if value else value


def _year_back(d: date, years: int = 1) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:          # 29 лютого
        return d.replace(year=d.year - years, day=28)


def _aware(d: date) -> datetime:
    return timezone.make_aware(datetime.combine(d, time.min))


def _pct(curr, prev) -> float | None:
    if not prev:
        return None
    return round(float((Decimal(curr) - Decimal(prev)) / Decimal(prev) * 100), 1)


# ── Ціни за 1 шт ──────────────────────────────────────────────────────────────

def first_price(rows) -> Decimal | None:
    """Ціна найменшого ступеня ([{min_qty, unit_price}] або [{qty, price}]) — ціна за 1 шт."""
    rows = normalize(rows)
    return Decimal(rows[0]["unit_price"]) if rows else None


def price_text(value) -> str:
    """19.9 → «19,90», 0.0123 → «0,0123»: щонайменше 2 знаки після коми, без зайвих нулів."""
    if value is None:
        return "—"
    d = Decimal(value).normalize()
    if d.as_tuple().exponent > -2:
        d = d.quantize(Decimal("0.01"))
    return f"{d:f}".replace(".", ",")


def _tiers(rows) -> list[tuple[int, Decimal]]:
    return [(r["min_qty"], Decimal(r["unit_price"])) for r in normalize(rows)]


def _price_at(tiers, qty: int) -> Decimal | None:
    """Ціна для кількості qty за ступенями; менше за перший ступінь (MOQ) — None."""
    price = None
    for q, p in tiers:
        if q <= qty:
            price = p
        else:
            break
    return price


def dk_currency() -> str:
    """Валюта цін офера DigiKey — те саме правило, що «💲 Ціни DigiKey ← Minerva»."""
    try:
        from bots.services.dk_prices import currency
        return currency()
    except Exception:       # noqa: BLE001 — немає налаштувань / застосунку: як Locale Currency за замовчуванням
        return "EUR"


def dk_listing(product):
    try:
        from bots.models import DigiKeyListing
    except ImportError:
        return None
    return DigiKeyListing.objects.filter(product=product).first()


@dataclass
class Change:
    at: datetime
    old: Decimal | None
    new: Decimal | None
    currency: str
    who: str = ""
    note: str = ""


@dataclass
class Point:
    at: datetime
    orig: Decimal | None          # у валюті джерела
    currency: str
    price: Decimal | None = None  # у валюті Minerva; None — ціни немає або немає курсу
    change: bool = False          # точка зміни ціни (не початок періоду й не «зараз»)


def _steps(changes, t0, now, current, hint, notes, what) -> list[Point]:
    """Ступінчастий ряд від t0 до now. changes — [Change] за часом; current — (ціна, валюта) зараз;
    hint — коли ціна востаннє змінювалась без запису в історії (або коли з'явилась, якщо історії немає)."""
    before = [c for c in changes if c.at < t0]
    inside = [c for c in changes if t0 <= c.at <= now]
    pts: list[Point] = []
    if before:
        pts.append(Point(t0, before[-1].new, before[-1].currency))
    elif inside and inside[0].old is not None:
        pts.append(Point(t0, inside[0].old, inside[0].currency))      # ціна до першої відомої зміни
    for c in inside:
        pts.append(Point(c.at, c.new, c.currency, change=True))
    price, cur = current
    last = pts[-1] if pts else None
    if price is not None and (last is None or last.orig != price or last.currency != cur):
        at = min(now, max(hint or t0, t0, last.at if last else t0))
        if last is not None and changes:
            notes.append(f"{what}: ціна змінилась без запису в історії — показано з "
                         f"{timezone.localtime(at):%d.%m.%Y}.")
        pts.append(Point(at, price, cur, change=last is not None))
    if pts:      # «зараз»: лінія доходить до сьогодні; ціни зараз немає — обривається
        pts.append(Point(now, price, cur) if price is not None else Point(now, None, pts[-1].currency))
    while pts and pts[0].orig is None:
        pts.pop(0)
    return pts


def _convert(points, rates, notes, what):
    """Ціни точок у валюту Minerva; немає курсу — точка без ціни (розрив) і примітка."""
    missing = set()
    for p in points:
        if p.orig is None:
            continue
        rate = rates.rate(p.currency)
        if rate is None:
            missing.add(p.currency or "?")
        else:
            p.price = (p.orig * rate).quantize(Decimal("0.0001"))
    for cur in sorted(missing):
        notes.append(f"{what}: немає курсу {cur} → {rates.home} («Налаштування цін» магазину) — "
                     f"ціни в {cur} не показано.")
    return points


def _minerva(product, t0, now, rates, notes) -> dict:
    hist = list(product.price_history.order_by("created_at", "pk"))
    home_cur = (product.base_price_currency or rates.home).upper()
    changes = [Change(h.created_at, first_price(h.old_prices), first_price(h.new_prices),
                      (h.currency or home_cur).upper(), h.user_label, h.note or h.get_source_display())
               for h in hist]
    current = (first_price(product.base_prices), home_cur)
    label, source = "Minerva — базова ціна", "base"
    if current[0] is None and not changes and product.sale_price:
        # базових цін немає й не було — «Ціна продажу» (у валюті Minerva, як ціни DigiKey ← Minerva)
        current, label, source = (Decimal(product.sale_price), rates.home), "Minerva — ціна продажу", "sale"
        notes.append("Базових цін немає — показано «Ціну продажу» (історії змін вона не має).")
    pts = _steps(changes, t0, now, current, product.base_prices_updated_at, notes, "Minerva")
    _convert(pts, rates, notes, "Minerva")
    return {"label": label, "source": source, "points": pts, "changes": changes,
            "now": pts[-1].price if pts else None, "now_orig": current[0], "currency": current[1]}


def _digikey(product, t0, now, rates, notes, listing) -> dict | None:
    if listing is None:
        return None
    cur = dk_currency()
    changes = [Change(lg.applied_at, first_price(lg.old_prices), first_price(lg.new_prices), cur, lg.user)
               for lg in listing.price_logs.order_by("applied_at", "pk")]
    current = (first_price(listing.dk_prices), cur)
    hint = listing.price_checked_at or listing.last_synced_at or listing.created_at
    pts = _steps(changes, t0, now, current, hint, notes, "DigiKey")
    _convert(pts, rates, notes, "DigiKey")
    if listing.sync_status != listing.SYNC_PUBLISHED and pts:
        notes.append(f"Лістинг DigiKey — «{listing.get_sync_status_display()}»: ціни DigiKey ще не діють "
                     "для покупців.")
    return {"label": "DigiKey — офер", "points": pts, "changes": changes, "listing": listing,
            "now": pts[-1].price if pts else None, "now_orig": current[0], "currency": cur}


# ── Продажі ───────────────────────────────────────────────────────────────────

def _lines(product, start: date, end: date):
    """Рядки продажів товару з start (включно) до end (не включно)."""
    from sales.models import SalesOrderLine
    return rv.sale_lines(SalesOrderLine.objects.filter(
        product=product, order__order_date__gte=start, order__order_date__lt=end))


def _priced():
    return Q(_amt__gt=0)


def monthly_rows(product, start: date, end: date) -> list[dict]:
    """Групи (місяць, валюта рядка, джерело): штуки, сума, штуки й сума рядків з ціною — у БД."""
    return list(_lines(product, start, end)
                .annotate(_m=TruncMonth("order__order_date"), _cur=rv.line_currency(), _amt=rv.line_amount())
                .values("_m", "_cur", "order__source")
                .annotate(units=Sum("qty"), amount=Sum("_amt"),
                          priced_units=Sum("qty", filter=_priced()), priced_amount=Sum("_amt", filter=_priced()))
                .order_by())


def _realized(rows, months, rates) -> dict:
    """{місяць: (виручка рядків з ціною у валюті Minerva, їхні штуки)} — лише валюти з курсом."""
    out = {m: [ZERO, ZERO] for m in months}
    for r in rows:
        m = _month_key(r["_m"])
        rate = rates.rate(r["_cur"])
        if m in out and rate is not None and r["priced_units"]:
            out[m][0] += Decimal(r["priced_amount"] or 0) * rate
            out[m][1] += Decimal(r["priced_units"])
    return out


def price_series(product, months: int = MONTHS, today: date | None = None, rates=None, rows=None) -> dict:
    """Ціна за 1 шт у часі (див. опис модуля): {home, start, minerva, digikey, realized, events, notes}."""
    today = today or timezone.localdate()
    rates = rates or rv.Rates()
    start, end = window(today, months)
    t0 = _aware(start)
    now = max(t0, min(timezone.now(), _aware(today + timedelta(days=1))))
    notes: list[str] = []
    minerva = _minerva(product, t0, now, rates, notes)
    digikey = _digikey(product, t0, now, rates, notes, dk_listing(product))
    rows = monthly_rows(product, start, end) if rows is None else rows
    realized = []
    for m, (amount, units) in _realized(rows, month_list(start, end), rates).items():
        if units:
            at = min(now, _aware(m) + timedelta(days=14, hours=12))      # середина місяця
            realized.append({"month": m, "at": at, "price": (amount / units).quantize(Decimal("0.0001")),
                             "units": units})
    events = []
    for name, series in (("Minerva", minerva), ("DigiKey", digikey)):
        for c in (series or {}).get("changes", []):
            if c.at < t0:
                continue
            rate = rates.rate(c.currency)
            home_old = c.old * rate if rate is not None and c.old is not None else None
            home_new = c.new * rate if rate is not None and c.new is not None else None
            events.append({"at": c.at, "where": name, "old": c.old, "new": c.new, "currency": c.currency,
                           "old_text": price_text(c.old), "new_text": price_text(c.new),
                           "home_old": home_old, "home_new": home_new, "change_pct": _pct(c.new, c.old)
                           if c.new is not None and c.old else None, "who": c.who, "note": c.note})
    events.sort(key=lambda e: e["at"], reverse=True)
    return {"home": rates.home, "start": start, "end": end, "t0": t0, "now": now, "minerva": minerva,
            "digikey": digikey, "realized": realized, "events": events, "notes": list(dict.fromkeys(notes))}


def _source_names(slugs) -> dict:
    from sales.models import SalesSource
    names = dict(SalesSource.objects.filter(slug__in=[s for s in slugs if s]).values_list("slug", "name"))
    return {s: names.get(s) or s or "—" for s in slugs}


def sales_series(product, months: int = MONTHS, today: date | None = None, rates=None, rows=None) -> dict:
    """По місяцях: штуки, виручка (валюта Minerva), замовлень, середня ціна; штуки за джерелами
    (CHANNELS найбільших + «інші»). Місяці без продажів — 0."""
    today = today or timezone.localdate()
    rates = rates or rv.Rates()
    start, end = window(today, months)
    rows = monthly_rows(product, start, end) if rows is None else rows
    ms = month_list(start, end)
    idx = {m: i for i, m in enumerate(ms)}
    n = len(ms)
    units = [ZERO] * n
    money = [rv.Money(rates.home) for _ in ms]
    by_source: dict[str, list] = {}
    for r in rows:
        i = idx.get(_month_key(r["_m"]))
        if i is None:
            continue
        u = Decimal(r["units"] or 0)
        units[i] += u
        by_source.setdefault(r["order__source"] or "", [ZERO] * n)[i] += u
        money[i].add(r["amount"], r["_cur"], rates)
    orders = [0] * n
    for r in (_lines(product, start, end).annotate(_m=TruncMonth("order__order_date"))
              .values("_m").annotate(n=Count("order", distinct=True)).order_by()):
        i = idx.get(_month_key(r["_m"]))
        if i is not None:
            orders[i] = r["n"]
    realized = _realized(rows, ms, rates)
    avg = [(realized[m][0] / realized[m][1]).quantize(Decimal("0.0001")) if realized[m][1] else None for m in ms]

    ranked = sorted(by_source, key=lambda s: (-sum(by_source[s]), s))
    names = _source_names(ranked)
    top = ranked if len(ranked) <= CHANNELS + 1 else ranked[:CHANNELS]
    channels = [{"slug": s, "name": names[s], "units": by_source[s], "total": sum(by_source[s], ZERO)} for s in top]
    rest = [s for s in ranked if s not in top]
    if rest:
        data = [sum((by_source[s][i] for s in rest), ZERO) for i in range(n)]
        channels.append({"slug": "", "name": OTHERS, "units": data, "total": sum(data, ZERO),
                         "sources": [names[s] for s in rest]})

    total = rv.total_missing(money) or rv.Money(rates.home)
    table = [{"month": m, "label": month_label(m), "units": units[i], "orders": orders[i], "revenue": money[i],
              "avg_price": avg[i], "channels": [c["units"][i] for c in channels]} for i, m in enumerate(ms)]
    notes = [f"Виручка без перерахунку: {total.missing_text} — додайте курс у «Налаштуваннях цін» магазину."] \
        if total.missing else []
    return {"home": rates.home, "start": start, "end": end, "months": ms, "labels": [month_label(m) for m in ms],
            "units": units, "revenue": money, "orders": orders, "avg_price": avg, "channels": channels,
            "rows": table, "total_units": sum(units, ZERO), "total_orders": sum(orders), "total_revenue": total,
            "notes": notes}


# ── Клієнти, замовлення ───────────────────────────────────────────────────────

def _customer_key(r) -> str:
    """Один клієнт — ключ CRM, інакше email, компанія, контакт (як групує CRM)."""
    for f, lower in (("order__customer_key", False), ("order__email", True), ("order__client", True),
                     ("order__contact_name", True)):
        v = (r.get(f) or "").strip()
        if v:
            return f"{f}:{v.lower() if lower else v}"
    return ""


_CUSTOMER_FIELDS = ("order__customer_key", "order__email", "order__client", "order__contact_name")


def customers(product, start: date, end: date, rates, limit: int = TOP_CUSTOMERS) -> tuple[int, list[dict]]:
    """(клієнтів, найбільші за штуками) — клієнти з продажами товару за період."""
    lines = _lines(product, start, end).annotate(_cur=rv.line_currency(), _amt=rv.line_amount())
    agg: dict[str, dict] = {}
    for r in lines.values(*_CUSTOMER_FIELDS, "_cur").annotate(units=Sum("qty"), amount=Sum("_amt")).order_by():
        c = agg.setdefault(_customer_key(r), {"row": r, "units": ZERO, "revenue": rv.Money(rates.home),
                                              "orders": set(), "last": None})
        c["units"] += Decimal(r["units"] or 0)
        c["revenue"].add(r["amount"], r["_cur"], rates)
    for r in lines.values(*_CUSTOMER_FIELDS, "order_id").annotate(last=Max("order__order_date")).order_by():
        c = agg.get(_customer_key(r))
        if c is not None:
            c["orders"].add(r["order_id"])
            c["last"] = max(filter(None, (c["last"], r["last"])), default=None)
    ranked = sorted(agg.values(), key=lambda c: (-c["units"], -c["revenue"].total))[:limit]
    crm = {}
    keys = [c["row"]["order__customer_key"] for c in ranked if c["row"]["order__customer_key"]]
    if keys:
        from crm.models import Customer
        crm = {c.external_key: c for c in Customer.objects.filter(external_key__in=keys)}
    top = []
    for c in ranked:
        r = c["row"]
        cust = crm.get(r["order__customer_key"])
        name = ((cust.company or cust.name) if cust else "") or r["order__client"] or r["order__contact_name"] \
            or r["order__email"] or "—"
        top.append({"name": name, "customer": cust, "email": r["order__email"], "units": c["units"],
                    "revenue": c["revenue"], "orders": len(c["orders"]), "last": c["last"]})
    return len([k for k in agg if k]), top


def last_orders(product, rates, limit: int = LAST_ORDERS) -> list[dict]:
    """Останні продажі товару (усі часи): замовлення, штуки й сума рядків цього товару у валюті Minerva."""
    from sales.models import SalesOrderLine
    orders = list(rv.sale_orders().filter(lines__product=product).distinct()
                  .order_by(F("order_date").desc(nulls_last=True), "-pk")[:limit])
    lines = (SalesOrderLine.objects.filter(order__in=[o.pk for o in orders], product=product)
             .annotate(_cur=rv.line_currency(), _amt=rv.line_amount())
             .values("order_id", "_cur").annotate(units=Sum("qty"), amount=Sum("_amt")).order_by())
    per: dict[int, dict] = {}
    for r in lines:
        d = per.setdefault(r["order_id"], {"units": ZERO, "amount": rv.Money(rates.home), "orig": []})
        d["units"] += Decimal(r["units"] or 0)
        d["amount"].add(r["amount"], r["_cur"], rates)
        if r["amount"]:
            d["orig"].append(rv.fmt(r["amount"], r["_cur"], 2))
    names = _source_names({o.source for o in orders})
    out = []
    for o in orders:
        d = per.get(o.pk, {"units": ZERO, "amount": rv.Money(rates.home), "orig": []})
        unit = (d["amount"].total / d["units"]).quantize(Decimal("0.01")) \
            if d["units"] and d["amount"].total and not d["amount"].missing else None
        out.append({"order": o, "source": names.get(o.source, o.source), "units": d["units"], "amount": d["amount"],
                    "unit_price": unit, "orig": ", ".join(d["orig"]),
                    "client": o.client or o.contact_name or o.email or "—"})
    return out


# ── Підсумки (KPI) ────────────────────────────────────────────────────────────

def _totals(product, d1: date, d2: date, rates) -> dict:
    """Штуки, виручка й середня ціна за дні d1…d2 (обидва включно)."""
    lines = _lines(product, d1, d2 + timedelta(days=1))
    units, priced_units, priced_rev = ZERO, ZERO, ZERO
    money = rv.Money(rates.home)
    for r in (lines.annotate(_cur=rv.line_currency(), _amt=rv.line_amount()).values("_cur")
              .annotate(units=Sum("qty"), amount=Sum("_amt"), priced_units=Sum("qty", filter=_priced()),
                        priced_amount=Sum("_amt", filter=_priced())).order_by()):
        units += Decimal(r["units"] or 0)
        money.add(r["amount"], r["_cur"], rates)
        rate = rates.rate(r["_cur"])
        if rate is not None and r["priced_units"]:
            priced_units += Decimal(r["priced_units"])
            priced_rev += Decimal(r["priced_amount"] or 0) * rate
    return {"units": units, "revenue": money, "orders": lines.aggregate(n=Count("order", distinct=True))["n"],
            "avg_price": (priced_rev / priced_units).quantize(Decimal("0.0001")) if priced_units else None}


def stock_now(product, today: date) -> dict:
    """Склад зараз і на скільки місяців вистачить — ті самі числа, що «📊 Аналіз запасів» картки."""
    from inventory.models import Product
    from inventory.services import stock
    row = stock.annotate_stock(Product.objects.filter(pk=product.pk)).first()
    on_hand, reserved, available = (row._on_hand, row._reserved, row._available) if row else (ZERO, ZERO, ZERO)

    def sold(days):
        return stock.monthly_sales_qs(today - timedelta(days=days)).filter(product=product) \
            .aggregate(t=Sum("qty"))["t"] or ZERO
    monthly = float(sold(90)) / 3
    if monthly <= 0:
        monthly = float(sold(730)) / 24 / 6     # як «Що замовити»: давні продажі з меншою вагою
    incoming = stock.incoming_qty(product, stock.CONFIRMED_PO_STATUSES)
    advice = stock.reorder_advice(available=available, monthly=monthly, reorder_point=product.reorder_point,
                                  incoming=incoming)
    return {"on_hand": on_hand, "reserved": reserved, "available": available, "incoming": incoming,
            "monthly": advice["monthly"], "months_left": advice["months_left"], "status": advice["status"],
            "status_text": STOCK_STATUS.get(advice["status"], ""), "reorder_qty": advice["reorder_qty"]}


def tiers_side_by_side(product, rates, listing=None, dk_cur: str | None = None) -> dict:
    """Ступені зараз: Minerva (базові ціни, без них — ціна продажу) і DigiKey (офер) для однакових кількостей."""
    m_tiers, m_cur, m_label = _tiers(product.base_prices), (product.base_price_currency or rates.home).upper(), \
        "базові ціни"
    if not m_tiers and product.sale_price:
        m_tiers, m_cur, m_label = [(1, Decimal(product.sale_price))], rates.home, "ціна продажу"
    d_tiers = _tiers(listing.dk_prices) if listing is not None else []
    d_cur = dk_cur or (dk_currency() if d_tiers else "")
    m_rate, d_rate = rates.rate(m_cur), rates.rate(d_cur) if d_tiers else None
    m_qtys, d_qtys = {q for q, _ in m_tiers}, {q for q, _ in d_tiers}
    rows = []
    for qty in sorted(m_qtys | d_qtys):
        m, d = _price_at(m_tiers, qty), _price_at(d_tiers, qty)
        m_home = m * m_rate if m is not None and m_rate is not None else None
        d_home = d * d_rate if d is not None and d_rate is not None else None
        rows.append({"qty": qty, "minerva": m, "minerva_home": m_home, "minerva_break": qty in m_qtys,
                     "digikey": d, "digikey_home": d_home, "digikey_break": qty in d_qtys,
                     "minerva_text": price_text(m), "digikey_text": price_text(d),
                     "diff_pct": _pct(d_home, m_home) if d_home is not None and m_home else None})
    return {"rows": rows, "minerva_currency": m_cur, "minerva_label": m_label, "digikey_currency": d_cur,
            "minerva_rate_missing": bool(m_tiers) and m_rate is None,
            "digikey_rate_missing": bool(d_tiers) and d_rate is None, "has_digikey": bool(d_tiers)}


_FETCH = object()


def kpis(product, today: date | None = None, rates=None, months: int = MONTHS, listing=_FETCH) -> dict:
    """Останні 12 міс проти попередніх 12 (штуки, виручка, Δ %), середня ціна, клієнти й топ-5 за період звіту,
    склад зараз і на скільки вистачить, ступені цін Minerva / DigiKey поруч."""
    today = today or timezone.localdate()
    rates = rates or rv.Rates()
    year_ago, two_years = _year_back(today), _year_back(today, 2)
    cur = _totals(product, year_ago + timedelta(days=1), today, rates)
    prev = _totals(product, two_years + timedelta(days=1), year_ago, rates)
    start, end = window(today, months)
    n_customers, top = customers(product, start, end, rates)
    listing = dk_listing(product) if listing is _FETCH else listing
    return {
        "units_12": cur["units"], "units_prev": prev["units"], "units_pct": _pct(cur["units"], prev["units"]),
        "revenue_12": cur["revenue"], "revenue_prev": prev["revenue"],
        "revenue_pct": _pct(cur["revenue"].total, prev["revenue"].total),
        "orders_12": cur["orders"], "avg_price": cur["avg_price"], "avg_price_prev": prev["avg_price"],
        "customers": n_customers, "top_customers": top, "stock": stock_now(product, today),
        "tiers": tiers_side_by_side(product, rates, listing), "since_12": year_ago + timedelta(days=1),
    }


# ── Разом ─────────────────────────────────────────────────────────────────────

def _num(value, digits: int = 2):
    return None if value is None else round(float(value), digits)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def chart_data(prices: dict, sales: dict) -> dict:
    """Дані для графіків Chart.js (json_script): числа й рядки, без Decimal / дат."""
    def series(s):
        # перед розривом (ціни немає / немає курсу) — та сама точка з попередньою ціною: ступінчаста лінія малює
        # відрізок лише між двома точками з цінами, інакше попередня ціна обірвалась би на своїй точці
        out = []
        for p in (s or {}).get("points", []):
            y = _num(p.price, 4)
            if y is None and out and out[-1]["y"] is not None:
                out.append({"x": _ms(p.at), "y": out[-1]["y"]})
            out.append({"x": _ms(p.at), "y": y, **({"c": 1} if p.change else {})})
        return out
    return {
        "currency": prices["home"], "symbol": rv.symbol(prices["home"]), "month_names": MONTH_NAMES,
        "x_min": _ms(prices["t0"]), "x_max": _ms(prices["now"]),
        "minerva": series(prices["minerva"]), "minerva_label": prices["minerva"]["label"],
        "digikey": series(prices["digikey"]),
        "realized": [{"x": _ms(r["at"]), "y": _num(r["price"], 4), "units": _num(r["units"], 3),
                      "m": month_label(r["month"])} for r in prices["realized"]],
        "labels": sales["labels"],
        "channels": [{"name": c["name"], "data": [_num(v, 3) for v in c["units"]], "other": c["name"] == OTHERS}
                     for c in sales["channels"]],
        "revenue": [_num(m.total, 2) for m in sales["revenue"]],
        "orders": sales["orders"],
    }


def build(product, months: int = MONTHS, today: date | None = None, tables: bool = True) -> dict:
    """Усе для звіту: ряди цін і продажів, KPI, дані графіків; tables — ще останні замовлення (сторінка звіту)."""
    months = months if months in MONTH_CHOICES else MONTHS
    today = today or timezone.localdate()
    rates = rv.Rates()
    start, end = window(today, months)
    rows = monthly_rows(product, start, end)
    prices = price_series(product, months, today, rates, rows=rows)
    sales = sales_series(product, months, today, rates, rows=rows)
    listing = prices["digikey"]["listing"] if prices["digikey"] else None
    data = {
        "product": product, "today": today, "months": months, "home": rates.home, "symbol": rv.symbol(rates.home),
        "start": start, "prices": prices, "sales": sales,
        "kpis": kpis(product, today, rates, months, listing=listing),
        "chart": chart_data(prices, sales), "notes": prices["notes"] + sales["notes"],
    }
    if tables:
        data["orders"] = last_orders(product, rates)
    return data


def csv_rows(data: dict) -> list[list]:
    """Звіт таблицями для CSV: продажі по місяцях, потім історія цін."""
    s, cur = data["sales"], data["home"]
    head = ["Місяць", "Штук", "Замовлень", f"Виручка, {cur}", f"Сер. ціна, {cur}"] + [c["name"] for c in s["channels"]]
    out = [[f"{data['product'].sku} — продажі по місяцях"], head]
    for r in s["rows"]:
        out.append([r["month"].strftime("%Y-%m"), r["units"], r["orders"], r["revenue"].total.quantize(Decimal("0.01")),
                    r["avg_price"] if r["avg_price"] is not None else ""] + list(r["channels"]))
    if s["total_revenue"].missing:
        out.append([s["total_revenue"].missing_text])
    out += [[], [f"{data['product'].sku} — історія цін за 1 шт"],
            ["Коли", "Де", "Було", "Стало", "Валюта", f"Було, {cur}", f"Стало, {cur}", "Зміна, %", "Хто", "Примітка"]]
    for e in data["prices"]["events"]:
        out.append([timezone.localtime(e["at"]).strftime("%Y-%m-%d %H:%M"), e["where"],
                    "" if e["old"] is None else e["old"], "" if e["new"] is None else e["new"], e["currency"],
                    "" if e["home_old"] is None else e["home_old"].quantize(Decimal("0.0001")),
                    "" if e["home_new"] is None else e["home_new"].quantize(Decimal("0.0001")),
                    "" if e["change_pct"] is None else e["change_pct"], e["who"], e["note"]])
    return out
