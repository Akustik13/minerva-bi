"""suppliers/signals.py — новий лист у пошті → чи це відповідь постачальника на запит (RFQ), suppliers/services/rfq.py."""
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender="email_assistant.EmailMessage", dispatch_uid="suppliers_rfq_reply")
def rfq_reply(sender, instance, created, **kwargs):
    if not created or kwargs.get("raw"):
        return

    def run():
        try:
            from suppliers.models import SupplierRequestRecipient
            if not SupplierRequestRecipient.objects.exclude(state=SupplierRequestRecipient.PLANNED).exists():
                return
            from suppliers.services.rfq import on_incoming
            on_incoming(instance)
        except Exception:  # noqa: BLE001 — збій тут не має зупиняти синхронізацію пошти
            logger.warning("suppliers: rfq reply check failed for message %s", instance.pk, exc_info=True)

    transaction.on_commit(run)
