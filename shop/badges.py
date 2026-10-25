"""Бейджі «у яких магазинах товар» — однакові в списку товарів, каталозі магазину й огляді складу.

Зелений — показується, сірий — сховано, з пунктирною рамкою — сам магазин вимкнений (напр. свіжа копія).
Підказка: назва магазину, стан, ціна. Посилання — на позицію в «Асортимент і ціни».
"""
from django.urls import reverse
from django.utils.html import format_html, format_html_join

STYLE = ("display:inline-block;margin:1px 2px 1px 0;padding:1px 7px;border-radius:9px;font-size:11px;"
         "line-height:16px;text-decoration:none;white-space:nowrap;color:#fff;")


def title(listing) -> str:
    shop = listing.shop
    price = listing.effective_price
    parts = [shop.name, "показується" if listing.is_visible else "сховано",
             f"{price:.2f} {shop.currency}" if price is not None else "ціна за запитом"]
    if listing.price_source == "base":
        parts.append("базові ціни")
    if not shop.is_active:
        parts.append("магазин вимкнено")
    return " · ".join(parts)


def style(listing) -> str:
    color = "#2e7d32" if listing.is_visible else "#757575"
    if listing.shop.is_active:
        return f"{STYLE}background:{color};border:1px solid {color}"
    return f"{STYLE}background:transparent;border:1px dashed {color};color:{color};opacity:.85"


def badges(listings, link: bool = True, empty: str = "—"):
    rows = sorted(listings, key=lambda l: (not l.shop.is_active, not l.shop.is_default, l.shop.name))
    if not rows:
        return format_html('<span style="opacity:.45">{}</span>', empty)
    if link:
        return format_html_join("", '<a href="{}" title="{}" style="{}">{}</a>', (
            (reverse("admin:shop_shoplisting_change", args=[l.pk]), title(l), style(l), l.shop.slug) for l in rows))
    return format_html_join("", '<span title="{}" style="{}">{}</span>',
                            ((title(l), style(l), l.shop.slug) for l in rows))
