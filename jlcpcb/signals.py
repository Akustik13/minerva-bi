"""
jlcpcb/signals.py — JLCOrder ↔ Shipment sync signals.
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


@receiver(post_save, sender='shipping.Shipment')
def sync_jlc_order_from_shipment(sender, instance, **kwargs):
    """
    When Shipment tracking updates — push ETA and delivered date back to JLCOrder.
    Runs after every track_shipments cycle.
    """
    if not instance.jlc_order_id:
        return
    try:
        order = instance.jlc_order
        changed = []

        # ETA: prefer eta_to (UPS/DHL real window), fallback to carrier_eta (tariff estimate)
        eta = instance.eta_to or instance.carrier_eta
        if eta and order.expected_date != eta:
            order.expected_date = eta
            changed.append('expected_date')

        # Delivered: when Shipment is delivered, set JLCOrder.delivered_date
        if (instance.status == 'delivered'
                and instance.delivered_at
                and not order.delivered_date):
            order.delivered_date = instance.delivered_at.date()
            changed.append('delivered_date')

        if changed:
            order.save(update_fields=changed)
            logger.info(
                "JLC signal: updated JLCOrder %s fields=%s",
                order.jlc_order_number, changed,
            )
    except Exception as e:
        logger.warning("JLC signal: date sync failed for shipment #%s: %s", instance.pk, e)
