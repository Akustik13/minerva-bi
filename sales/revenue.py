"""Виручка й кількість продажів для звітів — одне правило для дашборду, тенденцій, сигналів, дайджестів і RFM.

Що рахується як продаж — sale_orders(): document_type SALE (не запит пропозиції, зразок, гарантія, переміщення,
інше), не скасоване, з «Affects stock» (замовлення тестового магазину й запити пропозицій його не мають — у
статистику не потрапляють). Скасування «Affects stock» не знімає, тому скасовані виключаються окремо.

Що чекає відправки — to_ship(): з «Affects stock», ще не відправлене, не скасоване / відправлене / доставлене;
зразки, гарантія й переміщення теж (їх теж треба відправити), запити пропозицій — ні.

Сума замовлення — SalesOrder.total_price (DigiKey: разом з рядками, які не зіставились з товаром); порожня —
сума рядків (total_price рядка, інакше unit_price × qty) у валюті рядка (порожня — валюта замовлення).

Валюта → валюта Minerva (SystemSettings.default_currency) за курсами «Налаштувань цін» магазину
(offers.pricing.fx, ShopSettings.fx_rates — ті самі, що для веб-магазину). Порожня валюта = валюта Minerva.
Курсу немає — сума не додається мовчки: вона в Money.missing і показується окремо («+ $1,000 без курсу»).
Групування й суми — у БД (окремо для кожної валюти), перерахунок — у Python: однаково на PostgreSQL і SQLite.
"""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Case, CharField, DecimalField, ExpressionWrapper, F, Sum, Value, When
from django.db.models.functions import Coalesce

SALE_TYPES = ("SALE",)
NO_SHIP_TYPES = ("QUOTE",)
CANCELLED = "cancelled"
CLOSED_STATUSES = ("cancelled", "shipped", "delivered")

SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "UAH": "₴"}

ZERO = Decimal("0")
_DEC = DecimalField(max_digits=24, decimal_places=4)


# ── Що рахується ──────────────────────────────────────────────────────────────

def sale_orders(qs=None):
    """Продажі для статистики (див. опис модуля)."""
    if qs is None:
        from sales.models import SalesOrder
        qs = SalesOrder.objects.all()
    return qs.filter(affects_stock=True, document_type__in=SALE_TYPES).exclude(status=CANCELLED)


def sale_lines(qs=None):
    """Рядки продажів (ті самі умови, що sale_orders, на замовленні рядка)."""
    if qs is None:
        from sales.models import SalesOrderLine
        qs = SalesOrderLine.objects.all()
    return (qs.filter(order__affects_stock=True, order__document_type__in=SALE_TYPES)
            .exclude(order__status=CANCELLED))


def to_ship(qs=None):
    """Замовлення, що чекають відправки (див. опис модуля)."""
    if qs is None:
        from sales.models import SalesOrder
        qs = SalesOrder.objects.all()
    return (qs.filter(affects_stock=True, shipped_at__isnull=True)
            .exclude(document_type__in=NO_SHIP_TYPES)
            .exclude(status__in=CLOSED_STATUSES))


def line_amount():
    """Сума рядка: total_price, інакше unit_price × qty, інакше 0."""
    return Coalesce(
        "total_price",
        ExpressionWrapper(F("unit_price") * F("qty"), output_field=_DEC),
        Value(ZERO, output_field=_DEC),
        output_field=_DEC,
    )


def line_currency():
    """Валюта рядка; порожня — валюта замовлення."""
    return Case(When(currency="", then=F("order__currency")), default=F("currency"), output_field=CharField())


# ── Курси й суми ──────────────────────────────────────────────────────────────

def home_currency() -> str:
    from offers.pricing import home_currency as _home
    return _home()


def fmt(amount, currency: str, digits: int = 0) -> str:
    """€12,345 / $1,000 / 1,000 CHF."""
    currency = (currency or "").upper()
    num = f"{float(amount or 0):,.{digits}f}"
    sym = SYMBOLS.get(currency)
    return f"{sym}{num}" if sym else f"{num} {currency}".strip()


def symbol(currency: str) -> str:
    currency = (currency or "").upper()
    return SYMBOLS.get(currency, currency)


class Rates:
    """Курси до валюти Minerva (offers.pricing.fx), один раз на валюту за звіт."""

    def __init__(self, home: str | None = None):
        self.home = (home or home_currency()).upper()
        self._cache: dict = {}

    def rate(self, currency) -> Decimal | None:
        cur = (currency or "").strip().upper()
        if not cur or cur == self.home:
            return Decimal(1)
        if cur not in self._cache:
            from offers.pricing import fx
            self._cache[cur] = fx(cur, self.home)
        return self._cache[cur]


class Money:
    """Сума у валюті Minerva + суми у валютах без курсу (не додані)."""

    __slots__ = ("home", "total", "missing")

    def __init__(self, home: str, total: Decimal = ZERO, missing: dict | None = None):
        self.home = home
        self.total = Decimal(total or 0)
        self.missing = dict(missing or {})

    def add(self, amount, currency, rates: Rates) -> "Money":
        amount = Decimal(str(amount or 0))
        if not amount:
            return self
        rate = rates.rate(currency)
        if rate is None:
            cur = (currency or "").strip().upper()
            self.missing[cur] = self.missing.get(cur, ZERO) + amount
        else:
            self.total += amount * rate
        return self

    def __add__(self, other: "Money") -> "Money":
        out = Money(self.home, self.total + other.total, self.missing)
        for cur, amount in other.missing.items():
            out.missing[cur] = out.missing.get(cur, ZERO) + amount
        return out

    def __float__(self) -> float:
        return float(self.total)

    def __bool__(self) -> bool:
        return bool(self.total) or bool(self.missing)

    @property
    def text(self) -> str:
        """«€12,345» (без валют без курсу); нуль без жодних сум — «—»."""
        return fmt(self.total, self.home) if self else "—"

    @property
    def missing_text(self) -> str:
        """«+ $1,000 без курсу» — що не додано до суми; курси є для всіх валют — ''."""
        if not self.missing:
            return ""
        parts = ", ".join(fmt(v, c or "?") for c, v in sorted(self.missing.items()))
        return f"+ {parts} без курсу"

    @property
    def full_text(self) -> str:
        return " ".join(p for p in (self.text, self.missing_text) if p and p != "—") or "—"

    def __repr__(self):
        return f"Money({self.full_text})"


def converted(qs, amount, currency, by=(), rates: Rates | None = None):
    """Sum(amount) у БД окремо для кожної валюти (і кожної групи by), перерахунок у валюту Minerva.

    qs — queryset (групи by — його поля чи анотації), amount / currency — поле або вираз.
    Без by — Money; з by — {кортеж значень by: Money}."""
    rates = rates or Rates()
    if isinstance(amount, str):
        amount = F(amount)
    if isinstance(currency, str):
        currency = F(currency)
    rows = qs.order_by().annotate(_rev_cur=currency).values(*by, "_rev_cur").annotate(_rev_sum=Sum(amount))
    out: dict = {}
    for r in rows:
        key = tuple(r[b] for b in by)
        out.setdefault(key, Money(rates.home)).add(r["_rev_sum"], r["_rev_cur"], rates)
    if by:
        return out
    return out.get((), Money(rates.home))


def _group_exprs(by: dict, prefix: str) -> dict:
    return {f"g_{name}": (f(prefix) if callable(f) else F(prefix + f)) for name, f in by.items()}


def _merge(*parts: dict) -> dict:
    out: dict = {}
    for part in parts:
        for key, money in part.items():
            out[key] = out[key] + money if key in out else money
    return out


def revenue(orders, by=None, rates: Rates | None = None):
    """Виручка замовлень orders (queryset SalesOrder, напр. sale_orders()) у валюті Minerva.

    by — поля замовлення (список) або {назва: поле | функція(префікс) → вираз}, напр.
    {"month": lambda p: TruncMonth(p + "order_date")}. Без by — Money, з by — {кортеж: Money}."""
    from sales.models import SalesOrderLine

    rates = rates or Rates()
    by = by or {}
    if isinstance(by, (list, tuple)):
        by = {b: b for b in by}
    names = tuple(f"g_{n}" for n in by)
    with_total = converted(orders.filter(total_price__isnull=False).annotate(**_group_exprs(by, "")),
                           "total_price", "currency", names, rates)
    lines = SalesOrderLine.objects.filter(order__in=orders.filter(total_price__isnull=True).values("pk"))
    from_lines = converted(lines.annotate(**_group_exprs(by, "order__")), line_amount(), line_currency(),
                           names, rates)
    if not by:
        return with_total + from_lines
    return _merge(with_total, from_lines)


def line_revenue(lines, by=None, rates: Rates | None = None):
    """Виручка рядків (queryset SalesOrderLine) у валюті Minerva: сума рядка, валюта рядка.

    by — поля рядка (список) або {назва: поле | вираз}. Без by — Money, з by — {кортеж: Money}."""
    rates = rates or Rates()
    by = by or {}
    if isinstance(by, (list, tuple)):
        by = {b: b for b in by}
    exprs = {f"g_{n}": (F(f) if isinstance(f, str) else f) for n, f in by.items()}
    return converted(lines.annotate(**exprs), line_amount(), line_currency(), tuple(exprs), rates)


def total_missing(groups) -> Money | None:
    """Сума всіх груп (для примітки «+ … без курсу» під таблицею)."""
    groups = list(groups.values()) if isinstance(groups, dict) else list(groups)
    if not groups:
        return None
    out = groups[0]
    for m in groups[1:]:
        out = out + m
    return out
