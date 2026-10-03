"""Зміна ступенів цін → вебхук stock.changed для сайту (той самий механізм, що й у товарах)."""
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import ShopPriceTier


@receiver([post_save, post_delete], sender=ShopPriceTier, dispatch_uid="shop_tier_webhook")
def _tier_changed(sender, instance, **kwargs):
    try:
        from inventory.models import Product
        if Product.objects.filter(pk=instance.product_id, shop_visible=True).exists():
            from api.webhooks import _queue_stock
            _queue_stock(instance.product_id)
    except Exception:
        pass
