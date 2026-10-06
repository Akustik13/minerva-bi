"""Базові ціни товару (ступені за кількістю): нормалізація, зміна з історією, імпорт з DigiKey."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

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
    """Базові ціни ← ціни офера DigiKey (DigiKeyListing.dk_prices). None — у DigiKey цін немає."""
    from django.core.exceptions import ObjectDoesNotExist
    try:
        listing = product.dk_listing
    except ObjectDoesNotExist:
        return None
    rows = normalize(listing.dk_prices if listing else [])
    if not rows:
        return None
    try:
        from bots.models import DigiKeyConfig
        currency = DigiKeyConfig.get().locale_currency or "USD"
    except Exception:
        currency = "USD"
    return set_base_prices(product, rows, source="digikey", user=user, currency=currency,
                           source_label=digikey_source_label(listing))


# ── Масові операції (дія «💰 Базові ціни: масова зміна…» у списку товарів) ──

def _round(value: Decimal, mode: str | None) -> Decimal:
    from shop.services import round_price
    return round_price(value, mode) if mode else value.quantize(_Q)


def scale(rows, factor, rounding: str | None = None) -> list[dict]:
    """Усі ступені × factor (напр. 1.05 = +5 %, курс валюти)."""
    factor = Decimal(str(factor))
    return normalize([{"min_qty": r["min_qty"], "unit_price": _round(Decimal(r["unit_price"]) * factor, rounding)}
                      for r in normalize(rows)])


def round_all(rows, rounding: str) -> list[dict]:
    return normalize([{"min_qty": r["min_qty"], "unit_price": _round(Decimal(r["unit_price"]), rounding)}
                      for r in normalize(rows)])


def from_template(rows, schedule, rounding: str | None = None) -> list[dict]:
    """Ступені від ціни за 1 шт. за шаблоном знижок [{min_qty, discount}] (як «Налаштування цін»)."""
    rows = normalize(rows)
    if not rows:
        return []
    base = Decimal(rows[0]["unit_price"])
    out = [{"min_qty": 1, "unit_price": base}]
    for step in sorted(schedule or [], key=lambda s: int(s["min_qty"])):
        qty = int(step["min_qty"])
        if qty >= 2:
            out.append({"min_qty": qty,
                        "unit_price": _round(base * (Decimal(100) - Decimal(str(step["discount"]))) / 100, rounding)})
    return normalize(out)


def fx_factor(from_cur: str, to_cur: str) -> Decimal | None:
    """Курс from→to з «Налаштувань цін» магазину (курси задано відносно валюти магазину)."""
    from shop.models import Shop, ShopSettings
    from_cur, to_cur = (from_cur or "").upper(), (to_cur or "").upper()
    if from_cur == to_cur:
        return Decimal(1)
    rates = {k.upper(): v for k, v in (ShopSettings.get().fx_rates or {}).items()}
    home = (getattr(Shop.objects.filter(is_default=True).first(), "currency", None) or "EUR").upper()
    rates[home] = 1

    def rate(c):
        try:
            r = Decimal(str(rates[c]))
            return r if r > 0 else None
        except (KeyError, ValueError, ArithmeticError):
            return None
    a, b = rate(from_cur), rate(to_cur)
    return (a / b) if a and b else None
