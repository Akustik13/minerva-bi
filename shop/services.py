"""Ціноутворення магазину: базова ціна, ступені за кількістю, масові зміни."""
from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from django.db import transaction

from .models import ShopPriceTier, ShopSettings

_Q4 = Decimal("0.0001")


def round_price(value, mode: str | None = None) -> Decimal | None:
    """Округлення ціни за правилом із налаштувань (крок або «психологічне» закінчення)."""
    if value is None:
        return None
    value = Decimal(str(value))
    mode = mode or ShopSettings.get().rounding
    if mode == "none":
        return value.quantize(_Q4)
    if mode in ("x.90", "x.99"):
        ending = Decimal(mode[1:])                      # 0.90 / 0.99
        result = value.to_integral_value(rounding=ROUND_FLOOR) + ending
        if result - value > Decimal("0.5"):             # напр. 12.05 → 11.99, а не 12.99
            result -= 1
        return max(result, ending).quantize(_Q4)
    step = Decimal(mode)
    return ((value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step).quantize(_Q4)


def price_breaks(product) -> list[dict]:
    """[{min_qty, unit_price}] починаючи з 1 шт.; порожньо, якщо в товару немає ціни."""
    base = product.shop_effective_price
    if base is None:
        return []
    tiers = [{"min_qty": 1, "unit_price": Decimal(base)}]
    for t in sorted(product.shop_tiers.all(), key=lambda r: r.min_qty):
        if t.min_qty > 1:
            tiers.append({"min_qty": t.min_qty, "unit_price": t.unit_price})
    return tiers


def unit_price_for(product, qty) -> Decimal | None:
    """Ціна за штуку для кількості qty (найвищий ступінь, якого досягнуто)."""
    price = None
    qty = Decimal(str(qty))
    for tier in price_breaks(product):
        if qty >= tier["min_qty"]:
            price = tier["unit_price"]
    return price


@transaction.atomic
def generate_tiers(products, schedule: list[dict] | None = None, rounding: str | None = None) -> int:
    """Перестворює ступені цін від базової ціни за шаблоном знижок. Повертає к-сть товарів."""
    settings = ShopSettings.get()
    schedule = settings.price_breaks if schedule is None else schedule
    rounding = rounding or settings.rounding
    done = 0
    for product in products:
        ShopPriceTier.objects.filter(product=product).delete()
        base = product.shop_effective_price
        if base is None:
            continue
        rows = []
        for step in sorted(schedule or [], key=lambda s: int(s["min_qty"])):
            qty = int(step["min_qty"])
            if qty < 2:
                continue
            price = Decimal(base) * (Decimal(100) - Decimal(str(step["discount"]))) / 100
            rows.append(ShopPriceTier(product=product, min_qty=qty, unit_price=round_price(price, rounding)))
        for row in rows:
            row.save()  # save() по одному — сигнал вебхука для сайту
        done += 1
    return done


@transaction.atomic
def clear_tiers(products) -> int:
    n = 0
    for t in ShopPriceTier.objects.filter(product__in=list(products)):
        t.delete()
        n += 1
    return n


@transaction.atomic
def adjust_prices(products, percent, rounding: str | None = None, scale_tiers: bool = True) -> int:
    """Ціна магазину ±percent %. Якщо ціни магазину немає — від «Ціни продажу»."""
    rounding = rounding or ShopSettings.get().rounding
    factor = (Decimal(100) + Decimal(str(percent))) / 100
    done = 0
    for p in products:
        base = p.shop_effective_price
        if base is None:
            continue
        p.shop_price = round_price(Decimal(base) * factor, rounding)
        p.save(update_fields=["shop_price"])
        if scale_tiers:
            for t in p.shop_tiers.all():
                t.unit_price = round_price(t.unit_price * factor, rounding)
                t.save(update_fields=["unit_price"])
        done += 1
    return done


@transaction.atomic
def price_from_purchase(products, markup, rounding: str | None = None) -> tuple[int, list[str]]:
    """Ціна магазину = закупівля × (1 + націнка %). Повертає (к-сть, SKU без закупівельної ціни)."""
    rounding = rounding or ShopSettings.get().rounding
    factor = (Decimal(100) + Decimal(str(markup))) / 100
    done, skipped = 0, []
    for p in products:
        if not p.purchase_price:
            skipped.append(p.sku)
            continue
        p.shop_price = round_price(Decimal(p.purchase_price) * factor, rounding)
        p.save(update_fields=["shop_price"])
        done += 1
    return done, skipped


@transaction.atomic
def copy_sale_price(products) -> int:
    """Ціна магазину = «Ціна продажу» (скинути окрему ціну магазину)."""
    done = 0
    for p in products:
        if p.shop_price is not None:
            p.shop_price = None
            p.save(update_fields=["shop_price"])
            done += 1
    return done


@transaction.atomic
def round_all(products, rounding: str | None = None) -> int:
    rounding = rounding or ShopSettings.get().rounding
    done = 0
    for p in products:
        if p.shop_price is not None:
            p.shop_price = round_price(p.shop_price, rounding)
            p.save(update_fields=["shop_price"])
        for t in p.shop_tiers.all():
            t.unit_price = round_price(t.unit_price, rounding)
            t.save(update_fields=["unit_price"])
        done += 1
    return done


@transaction.atomic
def set_visibility(products, visible: bool) -> int:
    n = 0
    for p in products:
        if p.shop_visible != visible:
            p.shop_visible = visible
            p.save(update_fields=["shop_visible"])  # сигнал → вебхук для сайту
            n += 1
    return n
