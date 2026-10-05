"""Доставка магазину: регіони (країни), ціна доставки, безкоштовна доставка від порогу."""
from __future__ import annotations

import re
from decimal import Decimal

from .models import EU_COUNTRIES, REST_OF_WORLD, ShippingZone, Shop

_ISO2 = re.compile(r"^[A-Z]{2}$")


def parse_countries(text) -> list[str]:
    """«DE, AT ch; EU *» → ['DE', 'AT', 'CH', …країни ЄС…, '*']. ValueError з переліком помилкових кодів."""
    if isinstance(text, (list, tuple)):
        text = ",".join(str(t) for t in text)
    out, bad = [], []
    for token in re.split(r"[\s,;]+", (text or "").strip().upper()):
        if not token:
            continue
        if token in ("*", "WORLD", "REST"):
            items = [REST_OF_WORLD]
        elif token == "EU":
            items = EU_COUNTRIES
        elif _ISO2.match(token):
            items = [token]
        else:
            bad.append(token)
            continue
        out.extend(c for c in items if c not in out)
    if bad:
        raise ValueError("Невідомі коди країн: " + ", ".join(bad) + " (потрібні 2 літери ISO, напр. DE)")
    return out


def format_countries(codes) -> str:
    codes = list(codes or [])
    eu = set(EU_COUNTRIES)
    parts = []
    if eu.issubset(codes):
        parts.append("EU")
        codes = [c for c in codes if c not in eu]
    parts.extend(codes)
    return ", ".join(parts)


def active_zones(shop: Shop) -> list[ShippingZone]:
    return list(shop.shipping_zones.filter(is_active=True))


def zone_for(shop: Shop, country: str, zones: list[ShippingZone] | None = None) -> ShippingZone | None:
    """Регіон для країни: точний збіг, інакше «решта світу» (*), інакше None — не доставляємо."""
    country = (country or "").upper()
    zones = active_zones(shop) if zones is None else zones
    rest = None
    for z in zones:
        if country in (z.countries or []):
            return z
        if z.is_rest_of_world and rest is None:
            rest = z
    return rest


def is_free(shop: Shop, zone: ShippingZone, net) -> bool:
    return bool(shop.free_shipping_enabled and zone.free_shipping and shop.free_shipping_threshold is not None
                and Decimal(str(net)) >= shop.free_shipping_threshold)


def shipping_cost(shop: Shop, country: str, net) -> Decimal | None:
    """Вартість доставки (нетто) або None, якщо в цю країну магазин не доставляє."""
    zone = zone_for(shop, country)
    if zone is None:
        return None
    return Decimal("0.00") if is_free(shop, zone, net) else zone.price


def allowed_countries(zones: list[ShippingZone]) -> list[str] | None:
    """Країни, з яких можна замовити; None — усі (є регіон «решта світу»)."""
    codes = []
    for z in zones:
        if z.is_rest_of_world:
            return None
        codes.extend(c for c in z.countries or [] if c not in codes)
    return sorted(codes)


def public_config(shop: Shop) -> dict:
    """Налаштування доставки для сайту (GET /api/v1/shop/shipping/)."""
    zones = active_zones(shop)
    threshold = shop.free_shipping_threshold if shop.free_shipping_enabled else None
    return {
        "shop": shop.slug,
        "currency": shop.currency,
        "configured": bool(zones),
        "free_shipping": {"enabled": threshold is not None, "threshold": threshold},
        "allowed_countries": allowed_countries(zones) if zones else None,
        "zones": [{
            "name": z.name,
            "countries": z.countries,
            "price": z.price,
            "free_shipping": bool(z.free_shipping and threshold is not None),
            "free_from": threshold if (z.free_shipping and threshold is not None) else None,
        } for z in zones],
    }


def import_digikey_zones(shop: Shop, rates: list[dict]) -> tuple[int, int]:
    """Регіони з DigiKey (fetch_shipping_rates) → регіони доставки магазину. Повертає (створено, оновлено).
    Наявні регіони з тим самим кодом DigiKey оновлюються; ручні регіони не змінюються."""
    created = updated = 0
    taken: set[str] = set()  # країни, вже розподілені між імпортованими регіонами
    manual ={c for z in shop.shipping_zones.filter(is_active=True, dk_code="") for c in z.countries or []}
    for i, r in enumerate(rates):
        countries = [c for c in r["countries"] if c not in manual and c not in taken]
        if not countries:
            continue
        taken.update(countries)
        zone = shop.shipping_zones.filter(dk_code=r["code"]).first()
        price = Decimal(str(r["price"])) if r.get("price") is not None else Decimal("0")
        if zone:
            zone.countries, zone.price = countries, price
            zone.save(update_fields=["countries", "price"])
            updated += 1
        else:
            ShippingZone.objects.create(shop=shop, name=f"DigiKey: {r['code']}", countries=countries, price=price,
                                        dk_code=r["code"], sort_order=100 + i,
                                        is_active=True, free_shipping=True)
            created += 1
    return created, updated


# ── Базові регіони (за зразком правил доставки на DigiKey Marketplace) ───────
# DigiKey: Standard $19.90 — EUROPE; Express $29.00 — US-CONTINENTAL; Express $49.00 — решта світу.
# Ціни тут — нетто в EUR, округлені; Німеччина окремо дешевше. Після створення їх можна змінити.
EUROPE_NON_EU = ["CH", "GB", "NO", "LI", "IS"]
WORLD_EXPRESS = ["CA", "MX", "BR", "AR", "CL", "CO", "JP", "KR", "CN", "TW", "HK", "SG", "MY", "TH", "VN", "IN",
                 "AU", "NZ", "IL", "AE", "SA", "QA", "TR", "ZA", "UA", "MA", "EG"]
DEFAULT_ZONES = [
    {"name": "Deutschland", "countries": ["DE"], "price": Decimal("6.90"), "free_shipping": True},
    {"name": "Europa (Standard)", "countries": [c for c in EU_COUNTRIES if c != "DE"] + EUROPE_NON_EU,
     "price": Decimal("17.00"), "free_shipping": True},
    {"name": "USA (Express)", "countries": ["US"], "price": Decimal("25.00"), "free_shipping": False},
    {"name": "Welt (Express)", "countries": WORLD_EXPRESS, "price": Decimal("42.00"), "free_shipping": False},
]


def create_default_zones(shop: Shop) -> int:
    """Створює базові регіони; країни, що вже є в активних регіонах магазину, пропускаються.
    Повертає кількість створених регіонів."""
    taken = {c for z in active_zones(shop) for c in z.countries or []}
    created = 0
    for i, z in enumerate(DEFAULT_ZONES):
        countries = [c for c in z["countries"] if c not in taken]
        if not countries or shop.shipping_zones.filter(name=z["name"]).exists():
            continue
        ShippingZone.objects.create(shop=shop, name=z["name"], countries=countries, price=z["price"],
                                    free_shipping=z["free_shipping"], sort_order=(i + 1) * 10)
        taken.update(countries)
        created += 1
    if shop.free_shipping_threshold is None:
        shop.free_shipping_threshold = Decimal("250.00")  # поріг підготовлено, вмикається галочкою
        shop.save(update_fields=["free_shipping_threshold"])
    return created
