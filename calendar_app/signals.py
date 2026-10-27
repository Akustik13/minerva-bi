"""Новий лист → чи є в ньому запрошення на зустріч (iCalendar). Після прив'язки до клієнта CRM (email_assistant)."""
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from email_assistant.models import EmailMessage

logger = logging.getLogger(__name__)


@receiver(post_save, sender=EmailMessage, dispatch_uid="calendar_meeting_invites")
def scan_new_mail_for_invites(sender, instance, created, **kwargs):
    if not created or kwargs.get("raw"):
        return
    from .invites import detect
    try:
        with transaction.atomic():   # збій розпізнавання не повинен зламати збереження листа
            detect(instance)
    except Exception as e:  # noqa: BLE001
        logger.warning("meeting invite scan for message %s: %s", instance.pk, e)
