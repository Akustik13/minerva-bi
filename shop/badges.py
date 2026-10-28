"""Бейджі «у яких магазинах товар» — однакові в списку товарів, каталозі магазину й огляді складу.

Зелений — показується, сірий — сховано, з пунктирною рамкою — сам магазин вимкнений (напр. свіжа копія).
Підказка: назва магазину, стан, ціна. Посилання — на позицію в «Асортимент і ціни».
Останнім — «DigiKey», якщо в товару є лістинг DigiKey Marketplace (bots.DigiKeyListing): червоний — виставлено,
сірий — вимкнено на DigiKey, пунктир — чернетка / очікує затвердження, ⚠ — помилка синхронізації.
"""
from django.core.exceptions import ObjectDoesNotExist
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


DK_RED, DK_GREY = "#c8102e", "#757575"


def dk_listing(product):
    """Лістинг DigiKey Marketplace товару або None (для запиту — select_related("dk_listing"))."""
    try:
        return product.dk_listing
    except (ObjectDoesNotExist, AttributeError):
        return None


def dk_badge(listing, link: bool = True):
    """Бейдж «DigiKey»: стан лістингу, кількість і ціна на DigiKey у підказці; '' — лістингу немає."""
    if listing is None:
        return ""
    status = listing.sync_status
    if status == listing.SYNC_PUBLISHED and listing.dk_is_active:
        text, state, color, solid = "DigiKey", "виставлено", DK_RED, True
    elif status == listing.SYNC_PUBLISHED:
        text, state, color, solid = "DigiKey", "вимкнено на DigiKey", DK_GREY, True
    elif status == listing.SYNC_STAGED:
        text, state, color, solid = "DigiKey ⏳", "очікує затвердження DigiKey", DK_RED, False
    elif status == listing.SYNC_ERROR:
        text, state, color, solid = "DigiKey ⚠", "помилка синхронізації: " + (listing.last_error or "")[:120], DK_RED, False
    else:
        text, state, color, solid = "DigiKey", "чернетка — ще не виставлено", DK_GREY, False
    parts = ["DigiKey Marketplace", state]
    if listing.dk_quantity_available is not None:
        parts.append(f"на DigiKey {listing.dk_quantity_available} шт.")
    if listing.dk_price_min is not None:
        parts.append(f"ціна за 1 шт. {listing.dk_price_min:.2f}")
    tint = "#fdecee" if color == DK_RED else "#f1f1f1"   # світла плашка — читається на світлій і темній темі
    css = (f"{STYLE}background:{color};border:1px solid {color}" if solid else
           f"{STYLE}background:{tint};border:1px dashed {color};color:{color}")
    if link:
        return format_html('<a href="{}" title="{}" style="{}">{}</a>',
                           reverse("admin:bots_digikeylisting_change", args=[listing.pk]), " · ".join(parts), css, text)
    return format_html('<span title="{}" style="{}">{}</span>', " · ".join(parts), css, text)


def badges(listings, link: bool = True, empty: str = "—", dk=None):
    """Магазини товару, а за ним — DigiKey (dk — лістинг з dk_listing(product))."""
    rows = sorted(listings, key=lambda l: (not l.shop.is_active, not l.shop.is_default, l.shop.name))
    digikey = dk_badge(dk, link)
    if not rows:
        return digikey or format_html('<span style="opacity:.45">{}</span>', empty)
    if link:
        shops = format_html_join("", '<a href="{}" title="{}" style="{}">{}</a>', (
            (reverse("admin:shop_shoplisting_change", args=[l.pk]), title(l), style(l), l.shop.slug) for l in rows))
    else:
        shops = format_html_join("", '<span title="{}" style="{}">{}</span>',
                                 ((title(l), style(l), l.shop.slug) for l in rows))
    return format_html("{}{}", shops, digikey)
