"""Ціни й суми Angebot — лише з даних Minerva, у Decimal.

Порядок джерел ціни позиції:
1) магазин за замовчуванням (ступені за кількістю, діюча акція) — якщо валюта магазину = валюта Angebot;
2) «Ціна продажу» товару — якщо валюта Angebot = валюта Minerva за замовчуванням;
3) базові ціни товару (ступені) × курс із «Налаштувань цін» магазину;
інакше ціни немає — позиція «на запит», ціну вводить людина.
Округлення: ціна за одиницю — 4 знаки, суми — 2 знаки, ROUND_HALF_UP.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

Q2 = Decimal("0.01")
Q4 = Decimal("0.0001")
HUNDRED = Decimal("100")


@dataclass
class PriceQuote:
    unit_price: Decimal | None
    source: str
    note: str


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Q2, rounding=ROUND_HALF_UP)


def _num(value) -> str:
    """1.2300 → 1.23 для пояснень."""
    d = Decimal(str(value)).normalize()
    return format(d, "f")


def _reached(tiers, qty):
    """Ступінь, якого досягнуто для qty: (min_qty, price) або None."""
    best = None
    for min_qty, price in sorted(tiers, key=lambda t: t[0]):
        if qty >= min_qty:
            best = (min_qty, price)
    return best


def quote_price(product, qty, currency) -> PriceQuote:
    from offers.models import OfferLine
    qty = Decimal(str(qty or 1))
    currency = (currency or "").upper()

    try:
        from shop.models import Shop, ShopListing
        from shop.services import offer_percent, price_breaks
        shop = Shop.objects.filter(is_default=True, is_active=True).first()
        listing = (ShopListing.objects.filter(shop=shop, product=product).select_related("shop", "product").first()
                   if shop and shop.currency.upper() == currency else None)
        if listing is not None:
            reached = _reached([(t["min_qty"], t["unit_price"]) for t in price_breaks(listing)
                                if t["unit_price"] is not None], qty)
            if reached:
                pct = offer_percent(listing)
                note = f"Магазин «{shop.name}»: від {reached[0]} шт" + (f", акція −{_num(pct)}%" if pct else "")
                return PriceQuote(Decimal(reached[1]).quantize(Q4), OfferLine.SHOP, note)
    except ImportError:
        pass

    from config.models import SystemSettings
    if product.sale_price is not None and (SystemSettings.objects.first() or SystemSettings()).default_currency \
            .upper() == currency:
        return PriceQuote(Decimal(product.sale_price).quantize(Q4), OfferLine.SALE_PRICE, "Ціна продажу товару")

    from inventory.services.base_prices import tiers as base_tiers
    reached = _reached(base_tiers(product), qty)
    if reached:
        from shop.services import fx_rate
        base_cur = (product.base_price_currency or "").upper()
        rate = fx_rate(base_cur, currency)
        if rate is None:
            return PriceQuote(None, OfferLine.MANUAL,
                              f"Є базова ціна {_num(reached[1])} {base_cur}, але немає курсу {base_cur}→{currency}")
        unit = (Decimal(reached[1]) * rate).quantize(Q4, rounding=ROUND_HALF_UP)
        note = f"Базова ціна {_num(reached[1])} {base_cur} від {reached[0]} шт" + \
               ("" if rate == 1 else f" × курс {_num(rate)}")
        return PriceQuote(unit, OfferLine.BASE, note)

    return PriceQuote(None, OfferLine.MANUAL, "Ціни в Minerva немає — введіть вручну")


def line_total(qty, unit_price, discount_percent) -> Decimal:
    if unit_price is None:
        return Decimal("0.00")
    gross = Decimal(str(qty)) * Decimal(str(unit_price))
    k = (HUNDRED - Decimal(str(discount_percent or 0))) / HUNDRED
    return (gross * k).quantize(Q2, rounding=ROUND_HALF_UP)


def totals(line_totals, shipping_cost, vat_rate) -> dict:
    net = sum((Decimal(str(t)) for t in line_totals), Decimal("0")) + money(shipping_cost)
    net = net.quantize(Q2, rounding=ROUND_HALF_UP)
    vat = (net * Decimal(str(vat_rate or 0)) / HUNDRED).quantize(Q2, rounding=ROUND_HALF_UP)
    return {"net": net, "vat": vat, "gross": net + vat}
