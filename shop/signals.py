"""Сигнали магазину: позиції й ступені цін → вебхук stock.changed; оплачене замовлення → статус (paid_status)."""
from django.db.models.signals import post_delete, post_save, pre_save
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


@receiver(pre_save, sender="sales.SalesOrder", dispatch_uid="shop_paid_status_pre")
def _remember_paid(sender, instance, raw=False, update_fields=None, **kwargs):
    """Чи було замовлення вже оплачене до цього збереження (лише коли зберігається «Статус оплати»)."""
    instance._shop_paid_before = None
    if raw or not instance.pk or (update_fields is not None and "payment_status" not in update_fields):
        return
    instance._shop_paid_before = (
        type(instance).objects.filter(pk=instance.pk).values_list("payment_status", flat=True).first() == "paid")


@receiver(post_save, sender="sales.SalesOrder", dispatch_uid="shop_paid_status_post")
def _order_paid(sender, instance, created=False, raw=False, **kwargs):
    """Замовлення стало «Оплачено» → після commit правило магазину «Після оплати — статус» (shop/paid_status.py)."""
    if raw or instance.payment_status != "paid" or instance.status != "received":
        return
    if created or getattr(instance, "_shop_paid_before", None) is False:
        from django.db import transaction
        from .paid_status import safe_apply
        pk = instance.pk
        transaction.on_commit(lambda: safe_apply(pk))
