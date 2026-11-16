"""suppliers/signals.py — новий лист у пошті → чи це відповідь постачальника на запит (RFQ), suppliers/services/rfq.py."""
import logging

from django.db import transaction
from django.db.models.signals import post_delete, post_save
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


@receiver([post_save, post_delete], sender="inventory.Supplier", dispatch_uid="suppliers_mail_link_reset")
@receiver([post_save, post_delete], sender="suppliers.SupplierContact", dispatch_uid="suppliers_mail_link_reset_c")
def mail_link_reset(sender, **kwargs):
    """Змінили постачальника / контакт — довідник адрес для 🏭 у пошті зібрати заново (suppliers/services/mail_link.py)."""
    from suppliers.services.mail_link import reset
    reset()
