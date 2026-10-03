"""Ціноутворення магазинів: ціна позиції, ступені за кількістю, масові зміни, асортимент."""
from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from django.db import transaction

from .models import Shop, ShopListing, ShopPriceTier, ShopSettings

_Q4 = Decimal("0.0001")


# ── Магазин для ключа API ────────────────────────────────────────────────────

def shop_for_key(api_key) -> Shop | None:
    """Магазин ключа: прив'язаний → за кодом «Джерело замовлень» → магазин за замовчуванням."""
    if api_key is not None:
        if getattr(api_key, "shop_id", None):
            return Shop.objects.filter(pk=api_key.shop_id, is_active=True).first()
        if getattr(api_key, "default_source", ""):
            shop = Shop.objects.filter(slug=api_key.default_source, is_active=True).first()
            if shop:
                return shop
    return Shop.objects.filter(is_default=True, is_active=True).first()


# ── Ціни ─────────────────────────────────────────────────────────────────────

def round_price(value, mode: str | None = None) -> Decimal | None:
    """Округлення ціни за правилом із налаштувань (крок або «психологічне» закінчення)."""
    if value is None:
        return None
    value = Decimal(str(value))
    mode = mode or ShopSettings.get().rounding
    if mode == "none":
        return value.quantize(_Q4)
    if mode in ("x.90", "x.99"):
        ending = Decimal(mode[1:])
        result = value.to_integral_value(rounding=ROUND_FLOOR) + ending
        if result - value > Decimal("0.5"):             # напр. 12.05 → 11.99, а не 12.99
            result -= 1
        return max(result, ending).quantize(_Q4)
    step = Decimal(mode)
    return ((value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step).quantize(_Q4)


def price_breaks(listing) -> list[dict]:
    """[{min_qty, unit_price}] починаючи з 1 шт.; порожньо, якщо в позиції немає ціни."""
    base = listing.effective_price
    if base is None:
        return []
    tiers = [{"min_qty": 1, "unit_price": Decimal(base)}]
    for t in sorted(listing.tiers.all(), key=lambda r: r.min_qty):
        if t.min_qty > 1:
            tiers.append({"min_qty": t.min_qty, "unit_price": t.unit_price})
    return tiers


def unit_price_for(listing, qty) -> Decimal | None:
    """Ціна за штуку для кількості qty (найвищий ступінь, якого досягнуто)."""
    price = None
    qty = Decimal(str(qty))
    for tier in price_breaks(listing):
        if qty >= tier["min_qty"]:
            price = tier["unit_price"]
    return price


# ── Масові дії над позиціями ─────────────────────────────────────────────────

@transaction.atomic
def generate_tiers(listings, schedule: list[dict] | None = None, rounding: str | None = None) -> int:
    """Перестворює ступені цін від ціни позиції за шаблоном знижок. Повертає к-сть позицій."""
    settings = ShopSettings.get()
    schedule = settings.price_breaks if schedule is None else schedule
    rounding = rounding or settings.rounding
    done = 0
    for listing in listings:
        for t in listing.tiers.all():
            t.delete()  # по одному — сигнал вебхука для сайту
        base = listing.effective_price
        if base is None:
            continue
        for step in sorted(schedule or [], key=lambda s: int(s["min_qty"])):
            qty = int(step["min_qty"])
            if qty < 2:
                continue
            price = Decimal(base) * (Decimal(100) - Decimal(str(step["discount"]))) / 100
            ShopPriceTier.objects.create(listing=listing, min_qty=qty, unit_price=round_price(price, rounding))
        done += 1
    return done


@transaction.atomic
def clear_tiers(listings) -> int:
    n = 0
    for t in ShopPriceTier.objects.filter(listing__in=list(listings)):
        t.delete()
        n += 1
    return n


@transaction.atomic
def set_price(listings, price, rounding: str | None = None, regenerate_tiers: bool = False) -> int:
    """Однакова ціна (нетто, 1 шт.) для всіх вибраних позицій."""
    value = round_price(Decimal(str(price)), rounding or ShopSettings.get().rounding)
    listings = list(listings)
    for l in listings:
        l.price = value
        l.save(update_fields=["price", "updated_at"])
    if regenerate_tiers:
        generate_tiers(listings)
    return len(listings)


@transaction.atomic
def adjust_prices(listings, percent, rounding: str | None = None, scale_tiers: bool = True) -> int:
    """Ціна ±percent %. Якщо власної ціни немає — від «Ціни продажу» товару."""
    rounding = rounding or ShopSettings.get().rounding
    factor = (Decimal(100) + Decimal(str(percent))) / 100
    done = 0
    for l in listings:
        base = l.effective_price
        if base is None:
            continue
        l.price = round_price(Decimal(base) * factor, rounding)
        l.save(update_fields=["price", "updated_at"])
        if scale_tiers:
            for t in l.tiers.all():
                t.unit_price = round_price(t.unit_price * factor, rounding)
                t.save(update_fields=["unit_price"])
        done += 1
    return done


@transaction.atomic
def price_from_purchase(listings, markup, rounding: str | None = None) -> tuple[int, list[str]]:
    """Ціна = закупівля × (1 + націнка %). Повертає (к-сть, SKU без закупівельної ціни)."""
    rounding = rounding or ShopSettings.get().rounding
    factor = (Decimal(100) + Decimal(str(markup))) / 100
    done, skipped = 0, []
    for l in listings:
        cost = l.product.purchase_price
        if not cost:
            skipped.append(l.product.sku)
            continue
        l.price = round_price(Decimal(cost) * factor, rounding)
        l.save(update_fields=["price", "updated_at"])
        done += 1
    return done, skipped


@transaction.atomic
def reset_to_sale_price(listings) -> int:
    done = 0
    for l in listings:
        if l.price is not None:
            l.price = None
            l.save(update_fields=["price", "updated_at"])
            done += 1
    return done


@transaction.atomic
def round_all(listings, rounding: str | None = None) -> int:
    rounding = rounding or ShopSettings.get().rounding
    done = 0
    for l in listings:
        if l.price is not None:
            l.price = round_price(l.price, rounding)
            l.save(update_fields=["price", "updated_at"])
        for t in l.tiers.all():
            t.unit_price = round_price(t.unit_price, rounding)
            t.save(update_fields=["unit_price"])
        done += 1
    return done


@transaction.atomic
def set_visibility(listings, visible: bool) -> int:
    n = 0
    for l in listings:
        if l.is_visible != visible:
            l.is_visible = visible
            l.save(update_fields=["is_visible", "updated_at"])
            n += 1
    return n


# ── Асортимент ───────────────────────────────────────────────────────────────

@transaction.atomic
def add_products(shop: Shop, products, visible: bool = True, markup=None, with_tiers: bool = False) -> tuple[int, int]:
    """Додає товари в магазин. markup=None — ціна = «Ціна продажу»; інакше від закупівлі.
    Повертає (додано, вже були)."""
    added, existed = 0, 0
    new = []
    for p in products:
        listing, created = ShopListing.objects.get_or_create(shop=shop, product=p, defaults={"is_visible": visible})
        if not created:
            existed += 1
            continue
        added += 1
        new.append(listing)
    if markup is not None:
        price_from_purchase(new, markup)
    if with_tiers:
        generate_tiers(new)
    return added, existed


@transaction.atomic
def copy_listings(listings, target: Shop, factor=Decimal("1"), with_tiers: bool = True,
                  overwrite: bool = False, rounding: str | None = None) -> tuple[int, int]:
    """Копіює позиції в інший магазин з коефіцієнтом цін. Повертає (скопійовано, пропущено)."""
    rounding = rounding or ShopSettings.get().rounding
    factor = Decimal(str(factor))
    copied, skipped = 0, 0
    for src in listings:
        if src.shop_id == target.pk:
            skipped += 1
            continue
        dst, created = ShopListing.objects.get_or_create(shop=target, product=src.product,
                                                         defaults={"is_visible": src.is_visible})
        if not created and not overwrite:
            skipped += 1
            continue
        base = src.effective_price
        dst.is_visible = src.is_visible
        dst.price = round_price(Decimal(base) * factor, rounding) if base is not None else None
        dst.save()
        if with_tiers:
            for t in dst.tiers.all():
                t.delete()
            for t in src.tiers.all():
                ShopPriceTier.objects.create(listing=dst, min_qty=t.min_qty,
                                             unit_price=round_price(t.unit_price * factor, rounding))
        copied += 1
    return copied, skipped
