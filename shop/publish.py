"""Опублікувати товари складу в інтернет-магазині й одразу передати сайту фото, даташит і дані.

Звідки викликається: масова дія товарів «🏪 Опублікувати в інтернет-магазині…» (inventory ProductAdmin), дія
«➕ Додати в магазин…» каталогу магазину, галочка «додати й застосувати» в блоці «🏪 Інтернет-магазини» картки товару.

- `publish` — нові позиції (ціна: базові ціни товару / «Ціна продажу» / закупівля + націнка, ступені), сховані
  позиції цього магазину знову показуються; кожна зміна позиції шле сайту вебхук stock.changed (shop/signals.py) —
  сайт скидає кеш каталогу й бере свіжі дані (назва, ціни, лот, наявність, технічні параметри).
- `push` — фото й даташити: індексація саме цих товарів (shop/media.py, у фоні) і запит до сайту забрати копії
  (media_sync.php); вимкнено «🖼 Фото й даташити» або сайт магазину — пояснення, без помилки.
"""
from __future__ import annotations

from .models import Shop, ShopListing, ShopSettings

PRICE_SOURCES = ("base", "sale", "markup")


def publish(shop: Shop, products, visible: bool = True, price_source: str = "base", with_tiers: bool = True,
            by: str = "") -> dict:
    """→ {added, existed, shown, base_missing, media}: додано, уже були, знову показано, без базових цін (взято
    «Ціну продажу»), що з фото (`push`)."""
    from . import services
    products = list(products)
    ids = [p.pk for p in products]
    markup = ShopSettings.get().default_markup if price_source == "markup" else None
    base = price_source == "base"
    added, existed = services.add_products(shop, products, visible, markup, with_tiers and not base, base=base)
    base_missing = []
    if base:
        base_missing = [l.product.sku for l in ShopListing.objects.filter(shop=shop, product_id__in=ids)
                        .exclude(price_source=ShopListing.PRICE_BASE).select_related("product")
                        if not services.base_tiers(l.product)]
    shown = 0
    if visible:
        hidden = ShopListing.objects.filter(shop=shop, product_id__in=ids, is_visible=False)
        shown = services.set_visibility(hidden, True)
    return {"added": added, "existed": existed, "shown": shown, "base_missing": base_missing,
            "media": push(shop, ids, by=by)}


def push(shop: Shop, product_ids, by: str = "") -> str:
    """Фото й даташити цих товарів — на сайт магазину зараз. Повертає пояснення людською мовою."""
    from . import media
    ids = [int(i) for i in product_ids]
    if not ids:
        return ""
    if not media.settings_obj().enabled:
        return "фото й даташити для сайту вимкнено («🖼 Фото й даташити» → «Увімкнено») — сайт показує свої файли"
    if not shop.media_enabled:
        return f"фото й даташити для сайту «{shop}» вимкнено на сторінці «🖼 Фото й даташити»"
    if not shop.url:
        return f"у картці магазину «{shop}» немає адреси сайту — фото сайт забере під час наступної синхронізації"
    if media.start(mode="changed", product_ids=ids, by=by, shop_id=shop.pk):
        return "фото й даташити перевіряються, потім сайт забере їх (кілька хвилин)"
    media.ping_sites(only=shop)        # уже йде інша індексація — сайт забере те, що готово
    return "уже йде перевірка фото — сайту надіслано запит забрати готові файли"


def message(shop: Shop, result: dict) -> str:
    """Повідомлення для людини після публікації."""
    parts = [f"«{shop}»: додано {result['added']}"]
    if result["existed"]:
        parts.append(f"уже були {result['existed']}")
    if result["shown"]:
        parts.append(f"знову показано {result['shown']}")
    text = ", ".join(parts) + "."
    if result.get("base_missing"):
        text += " Без базових цін (ціна — «Ціна продажу»): " + ", ".join(result["base_missing"][:15]) + "."
    if result.get("media"):
        text += " Фото: " + result["media"] + "."
    return text
