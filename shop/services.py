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


KEY_LINKED, KEY_SOURCE, KEY_DEFAULT = "linked", "source", "default"


def keys_for_shop(shop: Shop) -> list[tuple]:
    """Активні ключі API, що працюють з магазином: [(ключ, як)] — як = linked (поле «Магазин»),
    source («Джерело замовлень» = код магазину) або default (без прив'язки → магазин за замовчуванням)."""
    from api.models import APIKey
    out = []
    for k in APIKey.objects.filter(is_active=True).order_by("name"):
        if getattr(shop_for_key(k), "pk", None) != shop.pk:
            continue
        mode = KEY_LINKED if k.shop_id == shop.pk else KEY_SOURCE if k.default_source == shop.slug else KEY_DEFAULT
        out.append((k, mode))
    return out


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


def regular_price_breaks(listing) -> list[dict]:
    """[{min_qty, unit_price}] без акції, починаючи з 1 шт.; порожньо, якщо в позиції немає ціни."""
    base = listing.effective_price
    if base is None:
        return []
    tiers = [{"min_qty": 1, "unit_price": Decimal(base)}]
    for t in sorted(listing.tiers.all(), key=lambda r: r.min_qty):
        if t.min_qty > 1:
            tiers.append({"min_qty": t.min_qty, "unit_price": t.unit_price})
    return tiers


def offer_percent(listing, today=None) -> Decimal | None:
    """Знижка діючої акції (Sonderangebot) або None."""
    from django.utils import timezone
    pct = Decimal(listing.discount_percent or 0)
    if pct <= 0:
        return None
    if listing.discount_until and listing.discount_until < (today or timezone.localdate()):
        return None
    return pct


def is_new(listing, today=None) -> bool:
    from django.utils import timezone
    return bool(listing.new_until and listing.new_until >= (today or timezone.localdate()))


def price_breaks(listing) -> list[dict]:
    """Ціни, які платить покупець: ступені з урахуванням діючої акції (округлення до 0,01)."""
    rows = regular_price_breaks(listing)
    pct = offer_percent(listing)
    if pct is None:
        return rows
    k = (Decimal(100) - pct) / 100
    return [{"min_qty": r["min_qty"], "unit_price": round_price(r["unit_price"] * k, "0.01")} for r in rows]


def offer_info(listing) -> dict | None:
    """Для сайту: {percent, until, regular_price, regular_price_breaks} діючої акції або None."""
    pct = offer_percent(listing)
    regular = regular_price_breaks(listing)
    if pct is None or not regular:
        return None
    return {"percent": pct, "until": listing.discount_until, "regular_price": regular[0]["unit_price"],
            "regular_price_breaks": regular}


def unit_price_for(listing, qty) -> Decimal | None:
    """Ціна за штуку для кількості qty (найвищий ступінь, якого досягнуто)."""
    price = None
    qty = Decimal(str(qty))
    for tier in price_breaks(listing):
        if qty >= tier["min_qty"]:
            price = tier["unit_price"]
    return price


# ── Масові дії над позиціями ─────────────────────────────────────────────────
# Ручні зміни цін від'єднують позицію від базових цін, інакше наступна зміна базових їх перезапише.

def _save_manual(listing, *fields):
    listing.price_source = ShopListing.PRICE_MANUAL
    listing.save(update_fields=[*fields, "price_source", "updated_at"])


@transaction.atomic
def generate_tiers(listings, schedule: list[dict] | None = None, rounding: str | None = None) -> int:
    """Перестворює ступені цін від ціни позиції за шаблоном знижок. Повертає к-сть позицій."""
    settings = ShopSettings.get()
    schedule = settings.price_breaks if schedule is None else schedule
    rounding = rounding or settings.rounding
    done = 0
    for listing in listings:
        if listing.price_source != ShopListing.PRICE_MANUAL:
            _save_manual(listing)
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
    listings = list(listings)
    for l in listings:
        if l.price_source != ShopListing.PRICE_MANUAL:
            _save_manual(l)
    for t in ShopPriceTier.objects.filter(listing__in=listings):
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
        _save_manual(l, "price")
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
        _save_manual(l, "price")
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
        _save_manual(l, "price")
        done += 1
    return done, skipped


@transaction.atomic
def reset_to_sale_price(listings) -> int:
    done = 0
    for l in listings:
        if l.price is not None or l.price_source != ShopListing.PRICE_MANUAL:
            l.price = None
            _save_manual(l, "price")
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
def set_offer(listings, percent, until=None) -> int:
    """Акція для вибраних позицій: знижка % до дати (percent=0 — зняти акцію)."""
    n = 0
    for l in listings:
        l.discount_percent = Decimal(str(percent))
        l.discount_until = until if percent else None
        l.save(update_fields=["discount_percent", "discount_until", "updated_at"])
        n += 1
    return n


@transaction.atomic
def set_new(listings, until=None) -> int:
    """Позначка «новинка» до дати (None — зняти)."""
    n = 0
    for l in listings:
        l.new_until = until
        l.save(update_fields=["new_until", "updated_at"])
        n += 1
    return n


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
def add_products(shop: Shop, products, visible: bool = True, markup=None, with_tiers: bool = False,
                 base: bool = False) -> tuple[int, int]:
    """Додає товари в магазин. markup=None — ціна = «Ціна продажу»; інакше від закупівлі;
    base=True — базові ціни товару (де є). Повертає (додано, вже були)."""
    added, existed = 0, 0
    new = []
    for p in products:
        listing, created = ShopListing.objects.get_or_create(shop=shop, product=p, defaults={"is_visible": visible})
        if not created:
            existed += 1
            continue
        added += 1
        new.append(listing)
    if base:
        use_base_prices(new)
        done_ids = {l.pk for l in new if l.price_source == ShopListing.PRICE_BASE}
        new = [l for l in new if l.pk not in done_ids]  # без базових цін / курсу — як «Ціна продажу»
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
        dst.price_source = ShopListing.PRICE_MANUAL
        dst.save()
        if with_tiers:
            for t in dst.tiers.all():
                t.delete()
            for t in src.tiers.all():
                ShopPriceTier.objects.create(listing=dst, min_qty=t.min_qty,
                                             unit_price=round_price(t.unit_price * factor, rounding))
        copied += 1
    return copied, skipped


# ── Базові ціни товару ───────────────────────────────────────────────────────
# Базові ціни (Product.base_prices, ступені за кількістю) — власні ціни Minerva; початково можуть бути
# імпортовані з DigiKey. Позиції з price_source=base підхоплюють їх зміни автоматично (shop/signals.py),
# з переведенням у валюту магазину за курсом із «Налаштувань цін».

def fx_rate(currency: str, shop_currency: str) -> Decimal | None:
    """Скільки одиниць валюти магазину за 1 одиницю currency; None — курс не задано."""
    currency, shop_currency = (currency or "").upper(), (shop_currency or "").upper()
    if not currency or currency == shop_currency:
        return Decimal(1)
    rate = (ShopSettings.get().fx_rates or {}).get(currency)
    try:
        rate = Decimal(str(rate))
    except (TypeError, ValueError, ArithmeticError):
        return None
    return rate if rate > 0 else None


def base_tiers(product) -> list[tuple[int, Decimal]]:
    """[(кількість, ціна)] базових цін товару (у валюті товару)."""
    from inventory.services.base_prices import tiers
    return tiers(product)


def apply_base_prices(listing, rounding: str | None = None) -> str:
    """Ціна позиції = базовий ступінь 1 шт. × курс × %, ступені — решта.
    Повертає "ok", "no_prices" (у товару немає базових цін) або "no_rate" (немає курсу валюти).
    Пише в БД лише те, що змінилось (щоб не надсилати зайвих вебхуків)."""
    tiers = base_tiers(listing.product)
    if not tiers:
        return "no_prices"
    rate = fx_rate(listing.product.base_price_currency, listing.shop.currency)
    if rate is None:
        return "no_rate"
    rounding = rounding or ShopSettings.get().rounding
    factor = rate * Decimal(listing.price_factor) / 100
    price = round_price(tiers[0][1] * factor, rounding)
    want = {qty: round_price(p * factor, rounding) for qty, p in tiers[1:] if qty >= 2}
    if listing.price != price:
        listing.price = price
        listing.save(update_fields=["price", "updated_at"])
    for t in list(listing.tiers.all()):
        if t.min_qty not in want:
            t.delete()
        elif t.unit_price != want[t.min_qty]:
            t.unit_price = want.pop(t.min_qty)
            t.save(update_fields=["unit_price"])
        else:
            want.pop(t.min_qty)
    for qty, p in sorted(want.items()):
        ShopPriceTier.objects.create(listing=listing, min_qty=qty, unit_price=p)
    return "ok"


@transaction.atomic
def use_base_prices(listings, factor=Decimal("100")) -> tuple[int, list[str], list[str]]:
    """Перемикає позиції на базові ціни товару. Повертає (к-сть, SKU без базових цін, SKU без курсу)."""
    done, missing, no_rate = 0, [], []
    for l in listings:
        if not base_tiers(l.product):
            missing.append(l.product.sku)
            continue
        if fx_rate(l.product.base_price_currency, l.shop.currency) is None:
            no_rate.append(f"{l.product.sku} ({l.product.base_price_currency})")
            continue
        l.price_source = ShopListing.PRICE_BASE
        l.price_factor = Decimal(str(factor))
        l.save(update_fields=["price_source", "price_factor", "updated_at"])
        apply_base_prices(l)
        done += 1
    return done, missing, no_rate


def reapply_base_prices(product_id=None) -> int:
    """Перерахувати позиції з базовими цінами (товару або всі — напр. після зміни курсу)."""
    qs = ShopListing.objects.filter(price_source=ShopListing.PRICE_BASE).select_related("product", "shop")
    if product_id is not None:
        qs = qs.filter(product_id=product_id)
    return sum(1 for l in qs if apply_base_prices(l) == "ok")


@transaction.atomic
def use_manual_prices(listings) -> int:
    """Від'єднує від базових цін: поточні ціни залишаються, далі змінюються лише вручну."""
    n = 0
    for l in listings:
        if l.price_source != ShopListing.PRICE_MANUAL:
            _save_manual(l)
            n += 1
    return n
