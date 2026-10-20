"""Новий лист → підказка, до якого проєкту він може належати (після прив'язки до CRM у email_assistant)."""
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from email_assistant.models import EmailMessage

logger = logging.getLogger(__name__)


@receiver(post_save, sender=EmailMessage)
def suggest_projects_for_new_mail(sender, instance, created, **kwargs):
    if not created or kwargs.get("raw"):
        return
    from projects.services import suggest_for_message
    try:
        with transaction.atomic():   # збій підказки не повинен зламати збереження листа
            suggest_for_message(instance)
    except Exception as e:
        logger.warning("project suggestion for message %s: %s", instance.pk, e)
