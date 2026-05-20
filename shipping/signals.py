from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from .models import Shipment


@receiver(pre_save, sender=Shipment)
def _capture_old_shipment_status(sender, instance, **kwargs):
    if instance.pk:
        try:
            instance._old_ship_status = sender.objects.filter(
                pk=instance.pk).values_list('status', flat=True).first()
        except Exception:
            instance._old_ship_status = None
    else:
        instance._old_ship_status = None


@receiver(post_save, sender=Shipment)
def _notify_shipment_events(sender, instance, created, **kwargs):
    try:
        old_status = getattr(instance, '_old_ship_status', None)
        new_status = instance.status
        if old_status and old_status != new_status:
            from dashboard.notifications import notify_shipment_status
            notify_shipment_status(instance, old_status, new_status)
    except Exception:
        pass
