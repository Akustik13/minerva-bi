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


@receiver(post_save, sender='email_assistant.EmailMessage')
def jlc_tracking_from_mail(sender, instance, created, **kwargs):
    """📨 Лист JLCPCB «відправлено» з номером замовлення W… і трек-номером → трек-номер у замовлення JLC."""
    if not created:
        return
    try:
        from django.db import transaction
        from jlcpcb.services import tracking
        if tracking.looks_like_jlc_mail(instance):
            transaction.on_commit(lambda pk=instance.pk: tracking.on_mail(pk))
    except Exception as e:  # noqa: BLE001
        logger.warning("JLC signal: mail tracking failed for message #%s: %s", instance.pk, e)


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

        # 📦 посилку доставлено → замовлення JLC «Доставлено», закупівля «прибула», авто-оприбуткування
        # (раніше переносилась лише дата, і замовлення лишалось «Відправлено»)
        if instance.status == 'delivered' and order.local_status in ('ordered', 'reviewed', 'in_production',
                                                                       'manufactured', 'shipped'):
            from django.db import transaction
            from jlcpcb.services import purchase

            def _deliver(pk=order.pk, sh=instance.pk):
                from jlcpcb.models import JLCOrder
                fresh = JLCOrder.objects.filter(pk=pk).first()
                if fresh is not None:
                    purchase.deliver(fresh, source=f'посилка #{sh} доставлена')
            transaction.on_commit(_deliver)
    except Exception as e:
        logger.warning("JLC signal: date sync failed for shipment #%s: %s", instance.pk, e)


@receiver(post_save, sender='inventory.PurchaseOrderLine')
def jlc_line_received_from_purchase(sender, instance, **kwargs):
    """«Отримано» позиції закупівлі JLCPCB змінили (огляд складу, картка закупівлі, API) — те саме в позиції JLC,
    щоб картка замовлення JLC бачила, що прийнято (jlcpcb/services/purchase.mirror_received)."""
    if not (instance.external_line_id or '').startswith('jlc:'):
        return
    try:
        from jlcpcb.services import purchase
        purchase.mirror_received(po_line=instance)
    except Exception as e:  # noqa: BLE001 — прийом на склад не має падати через позначку в JLC
        logger.warning("JLC signal: mirror received failed for PO line #%s: %s", instance.pk, e)
