"""Копія магазину з усіма налаштуваннями, крім ключів — щоб швидко підключити новий сайт («📋 Копіювати магазин»).

Копіюється: налаштування картки (доставка, кабінет, листи, оплата, помічник), зони доставки, асортимент із цінами
й ступенями, шаблони листів. Не копіюється: ключі й секрети (Stripe, ключ помічника), ключі API сайту, покупці,
замовлення, оплати, звіт сайту про фото — це належить конкретному сайту.
Копія вимкнена й не за замовчуванням. Поля, які треба змінити (назва, код, адреса сайту, ключі, «Активний»),
запам'ятовуються в Shop.review_fields зі значенням у момент копіювання і підсвічуються червоним, доки їх не змінили.
"""
from __future__ import annotations

import copy
import re

from django.db import transaction

from .models import ShippingZone, Shop, ShopListing, ShopMailTemplate, ShopPriceTier

SECRETS = ("stripe_secret_key", "stripe_webhook_secret", "assistant_key")
# Значення копії, які не беруться з оригіналу
RESET = {"is_default": False, "is_active": False, "account_links": {}, "account_link_error": "",
         "stripe_poll_last_at": None, "stripe_webhook_last_at": None}
SKIP = {"id", "created_at", "copied_from_id", "review_fields"}


def free_slug(base: str) -> str:
    base = base[:28].rstrip("-_") or "shop"
    slug, n = base, 2
    while Shop.objects.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def suggest(src: Shop) -> dict:
    """Значення за замовчуванням для форми копіювання."""
    return {"name": f"{src.name} (копія)", "slug": free_slug(f"{src.slug}-2"), "url": ""}


def to_review(src: Shop, name: str = "", slug: str = "", url: str = "") -> list[str]:
    """Що треба змінити в копії: назва й код, якщо лишилися запропоновані; адреса сайту, якщо не задана; назва
    для покупців, якщо була; ключі, які були в оригіналі (їх не скопійовано); «Активний» (копія вимкнена)."""
    fields = []
    if name in ("", src.name, f"{src.name} (копія)"):
        fields.append("name")
    if not slug or re.fullmatch(re.escape(src.slug) + r"-\d+", slug):
        fields.append("slug")
    if not url:
        fields.append("url")
    if src.public_name:
        fields.append("public_name")
    fields += [f for f in SECRETS if getattr(src, f)]
    return fields + ["is_active"]


@transaction.atomic
def copy_shop(src: Shop, name: str, slug: str, url: str = "", *, listings: bool = True, zones: bool = True,
              templates: bool = True) -> Shop:
    new = Shop(**{f.attname: copy.deepcopy(getattr(src, f.attname))
                  for f in Shop._meta.concrete_fields if f.attname not in SKIP})
    for field, value in RESET.items():
        setattr(new, field, copy.deepcopy(value))
    for field in SECRETS:
        setattr(new, field, "")
    new.name, new.slug, new.url = name, slug, url
    new.copied_from = src
    new.review_fields = {f: getattr(new, f) for f in to_review(src, name, slug, url)}
    new.save()
    if zones:
        ShippingZone.objects.bulk_create([
            ShippingZone(**{f.attname: copy.deepcopy(getattr(z, f.attname)) for f in ShippingZone._meta.concrete_fields
                            if f.attname not in ("id", "shop_id")}, shop=new)
            for z in src.shipping_zones.all()])
    if listings:
        _copy_listings(src, new)
    if templates:
        ShopMailTemplate.objects.bulk_create([
            ShopMailTemplate(**{f.attname: getattr(t, f.attname) for f in ShopMailTemplate._meta.concrete_fields
                                if f.attname not in ("id", "shop_id", "updated_at")}, shop=new)
            for t in src.mail_templates.all()])
    return new


def _copy_listings(src: Shop, new: Shop):
    """Асортимент зі ступенями — bulk_create без сигналів: новий магазин вимкнений, сайтам нічого повідомляти."""
    skip = ("id", "shop_id", "created_at", "updated_at")
    rows = list(src.listings.prefetch_related("tiers"))
    created = ShopListing.objects.bulk_create([
        ShopListing(**{f.attname: getattr(l, f.attname) for f in ShopListing._meta.concrete_fields
                       if f.attname not in skip}, shop=new) for l in rows])
    ShopPriceTier.objects.bulk_create([
        ShopPriceTier(**{f.attname: getattr(t, f.attname) for f in ShopPriceTier._meta.concrete_fields
                         if f.attname not in ("id", "listing_id")}, listing=made)
        for old, made in zip(rows, created) for t in old.tiers.all()])


def pending(shop: Shop) -> list[str]:
    """Поля, які ще не змінено після копіювання."""
    review = shop.review_fields if isinstance(shop.review_fields, dict) else {}
    return [f for f, initial in review.items() if hasattr(shop, f) and getattr(shop, f) == initial]


def refresh(shop: Shop) -> list[str]:
    """Після збереження: прибрати змінені поля зі списку. Повертає ті, що лишилися."""
    left = pending(shop)
    if set(left) != set(shop.review_fields or {}):
        keep = {f: v for f, v in (shop.review_fields or {}).items() if f in left}
        Shop.objects.filter(pk=shop.pk).update(review_fields=keep)
        shop.review_fields = keep
    return left


def labels(shop: Shop, fields) -> list[str]:
    return [str(Shop._meta.get_field(f).verbose_name) for f in fields]


def summary(src: Shop) -> dict:
    """Що буде скопійовано — для сторінки копіювання."""
    return {
        "listings": src.listings.count(),
        "tiers": ShopPriceTier.objects.filter(listing__shop=src).count(),
        "zones": src.shipping_zones.count(),
        "templates": src.mail_templates.count(),
        "secrets": [str(Shop._meta.get_field(f).verbose_name) for f in SECRETS if getattr(src, f)],
        "customers": src.customers.count(),
        "orders": src.shop_orders.count(),
        "keys": src.api_keys.filter(is_active=True).count(),
    }
