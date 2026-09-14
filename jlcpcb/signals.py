"""
jlcpcb/signals.py — auto-create Shipment when tracking_number is set on JLCOrder.
"""
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender='jlcpcb.JLCOrder')
def sync_jlc_shipment(sender, instance, **kwargs):
    """When tracking_number is added/changed on JLCOrder — create or update Shipment."""
    if not instance.tracking_number:
        return
    try:
        from shipping.services.import_tracking import ensure_shipment_for_jlc_order
        shipment, created = ensure_shipment_for_jlc_order(
            jlc_order=instance,
            tracking_number=instance.tracking_number,
            carrier_name=instance.tracking_carrier,
        )
        if created:
            logger.info(
                "JLC signal: created Shipment #%s for %s",
                shipment.pk, instance.jlc_order_number,
            )
    except Exception as e:
        logger.warning("JLC signal: shipment sync failed for %s: %s", instance.jlc_order_number, e)
