"""Базові ціни товару (ступені за кількістю): нормалізація, зміна з історією, імпорт з DigiKey, переведення у
валюту Minerva. Базові ціни тримаються у валюті Minerva (`offers.pricing.home_currency`, EUR): DigiKey, магазин і
Angebote перераховують їх у свою валюту за курсом."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.utils import timezone

_Q = Decimal("0.0001")


def normalize(rows) -> list[dict]:
    """[{min_qty, unit_price}] → відсортовано, без дублікатів, ціни рядками (Decimal)."""
    out = {}
    for r in rows or []:
        try:
            qty = int(r.get("min_qty", r.get("qty")))
            price = Decimal(str(r.get("unit_price", r.get("price")))).quantize(_Q)
        except (TypeError, ValueError, InvalidOperation, AttributeError):
            continue
        if qty >= 1 and price > 0:
            out[qty] = price
    return [{"min_qty": q, "unit_price": format(p.normalize(), "f")} for q, p in sorted(out.items())]


def tiers(product) -> list[tuple[int, Decimal]]:
    """[(кількість, ціна)] базових цін товару."""
    return [(r["min_qty"], Decimal(r["unit_price"])) for r in normalize(product.base_prices)]


def parse_text(text: str) -> list[dict]:
    """«1: 4,89\n10: 4.52» → [{min_qty, unit_price}]. ValueError з номерами помилкових рядків."""
    rows, bad = [], []
    for i, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\s*(?:шт\.?|pcs)?\s*[:=;\t ]\s*([\d.,]+)$", line, re.I)
        if not m:
            bad.append(str(i))
            continue
        rows.append({"min_qty": int(m.group(1)), "unit_price": m.group(2).replace(",", ".")})
    if bad:
        raise ValueError("Рядки " + ", ".join(bad) + ": формат «кількість: ціна», напр. «10: 4.52»")
    rows = normalize(rows)
    if rows and rows[0]["min_qty"] != 1:
        raise ValueError("Перший ступінь має бути від 1 шт.")
    return rows


def format_text(rows) -> str:
    return "\n".join(f'{r["min_qty"]}: {r["unit_price"]}' for r in normalize(rows))


def home_currency() -> str:
    from offers.pricing import home_currency as _home
    return _home()


def rate_to_home(src: str, home: str | None = None) -> Decimal | None:
    """Курс src → валюта Minerva (1 src = N home) — той самий, яким ціни Minerva стають цінами DigiKey
    («💲 Налаштування цін DigiKey» → «Курс для цін DigiKey»), тож після переведення ціни на DigiKey не зрушать.
    None — курсу немає."""
    home = (home or home_currency()).upper()
    src = (src or home).upper()
    if src == home:
        return Decimal(1)
    try:
        from bots.services.dk_prices import fx
        return fx(src, home)
    except ImportError:
        from offers.pricing import fx
        return fx(src, home)


def converted(price, rate, cents: bool = False) -> Decimal:
    """Ціна × курс. За замовчуванням — 4 знаки (точність базових цін): тоді зворотний перерахунок для DigiKey дає ту
    саму ціну (268,39 $ → 230,8154 € → 268,39 $; з центами 230,82 € → 268,40 $). `cents` — від 1 до центів."""
    value = Decimal(str(price)) * Decimal(str(rate))
    q = Decimal("0.01") if cents and value >= 1 else _Q
    return value.quantize(q, rounding=ROUND_HALF_UP).normalize()


def rate_text(rate) -> str:
    return format(Decimal(str(rate)).quantize(Decimal("0.000001")).normalize(), "f")


def convert(product, to: str, rate, *, user=None, note: str = "", cents: bool = False) -> bool:
    """Базові ціни товару → валюта `to` за курсом (1 стара валюта = `rate` нової), з історією
    «Переведено з USD в EUR за курсом 0.86». `cents` — округлити до центів (ціни DigiKey можуть зрушити на цент).
    False — немає цін або вже в цій валюті."""
    rows = normalize(product.base_prices)
    src = (product.base_price_currency or "EUR").upper()
    to = (to or "").upper()
    if not rows or src == to:
        return False
    rate = Decimal(str(rate))
    if rate <= 0:
        raise ValueError("Курс має бути більшим за 0")
    new = [{"min_qty": r["min_qty"], "unit_price": format(converted(r["unit_price"], rate, cents), "f")} for r in rows]
    label = f"Переведено з {src} в {to} за курсом {rate_text(rate)}" + (", до центів" if cents else "")
    return set_base_prices(product, new, source="manual", user=user, currency=to, source_label=label[:200],
                           note=(f"{label} · {note}" if note else label)[:255])


def not_home(qs=None, home: str | None = None):
    """Товари з базовими цінами не у валюті Minerva (список, бо «є ціни» — JSON)."""
    from inventory.models import Product
    home = (home or home_currency()).upper()
    qs = Product.objects.all() if qs is None else qs
    return [p for p in qs.exclude(base_price_currency__iexact=home).order_by("sku") if normalize(p.base_prices)]


def digikey_source_label(listing) -> str:
    parts = ["DigiKey"]
    if listing.dk_offer_id:
        parts.append(f"офер {listing.dk_offer_id}")
    if listing.last_synced_at:
        parts.append("синхр. " + timezone.localtime(listing.last_synced_at).strftime("%d.%m.%Y %H:%M"))
    return " · ".join(parts)


def set_base_prices(product, rows, *, source: str = "manual", user=None, currency: str | None = None,
                    source_label: str | None = None, note: str = "") -> bool:
    """Змінює базові ціни і пише історію. Повертає False, якщо нічого не змінилось."""
    from inventory.models import ProductPriceHistory

    new = normalize(rows)
    old = normalize(product.base_prices)
    currency = (currency or product.base_price_currency or "EUR").upper()
    if new == old and currency == product.base_price_currency:
        return False
    who = (user.get_username() if user is not None and getattr(user, "is_authenticated", False) else "") or "система"
    label = source_label if source_label is not None else (
        f"Змінено вручну: {who}" if source == "manual" else dict(ProductPriceHistory.SOURCE_CHOICES).get(source, source))
    product.base_prices = new
    product.base_price_currency = currency
    product.base_prices_source = label[:200]
    product.base_prices_updated_at = timezone.now()
    product.base_prices_updated_by = who[:150]
    product.save(update_fields=["base_prices", "base_price_currency", "base_prices_source",
                                "base_prices_updated_at", "base_prices_updated_by"])
    ProductPriceHistory.objects.create(
        product=product, user=user if getattr(user, "is_authenticated", False) else None, user_label=who[:150],
        source=source, currency=currency, old_prices=old, new_prices=new, note=(note or label)[:255])
    return True


def import_from_digikey(product, user=None) -> bool | None:
    """Базові ціни ← ціни офера DigiKey (DigiKeyListing.dk_prices), переведені у валюту Minerva за курсом для
    цін DigiKey (`rate_to_home`); курсу немає — у валюті DigiKey з приміткою. None — у DigiKey цін немає."""
    from django.core.exceptions import ObjectDoesNotExist
    try:
        listing = product.dk_listing
    except ObjectDoesNotExist:
        return None
    rows = normalize(listing.dk_prices if listing else [])
    if not rows:
        return None
    try:
        from bots.services.dk_prices import currency as dk_currency   # як «Валюта цін на DigiKey»
        currency = dk_currency() or "USD"
    except Exception:
        currency = "USD"
    label = digikey_source_label(listing)
    home = home_currency()
    if currency.upper() != home:
        rate = rate_to_home(currency, home)
        if rate:
            rows = [{"min_qty": r["min_qty"], "unit_price": format(converted(r["unit_price"], rate), "f")}
                    for r in rows]
            label += f" · {currency} → {home} за курсом {rate_text(rate)}"
            currency = home
        else:
            label += f" · у {currency}: немає курсу {currency} → {home}"
    return set_base_prices(product, rows, source="digikey", user=user, currency=currency,
                           source_label=label[:200])
