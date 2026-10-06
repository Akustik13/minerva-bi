"""Зміна позиції магазину або ступенів цін → вебхук stock.changed для сайтів."""
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import ShopListing, ShopPriceTier, ShopSettings


def _queue(product_id):
    if not product_id:
        return
    try:
        from api.webhooks import _queue_stock
        _queue_stock(product_id)
    except Exception:
        pass


@receiver([post_save, post_delete], sender=ShopListing, dispatch_uid="shop_listing_webhook")
def _listing_changed(sender, instance, **kwargs):
    _queue(instance.product_id)


@receiver([post_save, post_delete], sender=ShopPriceTier, dispatch_uid="shop_tier_webhook")
def _tier_changed(sender, instance, **kwargs):
    try:
        _queue(ShopListing.objects.filter(pk=instance.listing_id).values_list("product_id", flat=True).first())
    except Exception:
        pass


_BASE_FIELDS = {"base_prices", "base_price_currency"}


@receiver(post_save, sender="inventory.Product", dispatch_uid="shop_base_prices")
def _base_prices_changed(sender, instance, update_fields=None, created=False, **kwargs):
    """Змінено базові ціни товару → позиції магазинів з джерелом «Базові ціни»."""
    if created or (update_fields is not None and not (_BASE_FIELDS & set(update_fields))):
        return
    from .services import reapply_base_prices
    reapply_base_prices(instance.pk)


@receiver(post_save, sender=ShopSettings, dispatch_uid="shop_fx_rates")
def _settings_changed(sender, instance, update_fields=None, **kwargs):
    """Змінено курси / округлення → перерахувати всі позиції з базовими цінами."""
    if update_fields is not None and not ({"fx_rates", "rounding"} & set(update_fields)):
        return
    from .services import reapply_base_prices
    reapply_base_prices()
