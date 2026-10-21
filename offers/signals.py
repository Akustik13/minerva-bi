"""Новий лист → чи це запит пропозиції (після категорії й CRM з email_assistant і проєктів)."""
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from email_assistant.models import EmailMessage

logger = logging.getLogger(__name__)


@receiver(post_save, sender=EmailMessage, dispatch_uid="offers_quote_request_scan")
def scan_new_mail_for_quote_request(sender, instance, created, **kwargs):
    if not created or kwargs.get("raw"):
        return
    from .detection import scan
    try:
        with transaction.atomic():   # збій розпізнавання не повинен зламати збереження листа
            scan(instance)
    except Exception as e:  # noqa: BLE001
        logger.warning("quote request scan for message %s: %s", instance.pk, e)
