"""Зміна позиції магазину або ступенів цін → вебхук stock.changed для сайтів."""
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import ShopListing, ShopPriceTier


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


@receiver(post_save, sender="bots.DigiKeyListing", dispatch_uid="shop_digikey_prices")
def _digikey_prices_changed(sender, instance, update_fields=None, **kwargs):
    """Нові ціни офера DigiKey → позиції магазинів з джерелом ціни «DigiKey»."""
    if not instance.product_id or (update_fields is not None and "dk_prices" not in update_fields):
        return
    from .services import apply_digikey_prices
    for listing in ShopListing.objects.filter(product_id=instance.product_id,
                                              price_source=ShopListing.PRICE_DIGIKEY).select_related("product"):
        apply_digikey_prices(listing)
