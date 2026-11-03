"""Новий лист → чи є в ньому запрошення на зустріч (iCalendar). Після прив'язки до клієнта CRM (email_assistant).
Дедлайн у календарі ↔ замовлення (calendar_app/order_deadlines.py): нова подія-дедлайн дізнається своє замовлення,
збережене замовлення (відправили / скасували / повернули в роботу) закриває чи відкриває свої дедлайни."""
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


@receiver(post_save, sender="calendar_app.CalendarEvent", dispatch_uid="calendar_order_deadline_link")
def link_new_deadline(sender, instance, created, **kwargs):
    if not created or kwargs.get("raw") or instance.event_type != instance.TYPE_DEADLINE or instance.order_state:
        return
    from .order_deadlines import link
    try:
        with transaction.atomic():   # збій пошуку замовлення не ламає створення події
            link(instance)
    except Exception as e:  # noqa: BLE001
        logger.warning("order of calendar event %s: %s", instance.pk, e)


@receiver(post_save, sender="sales.SalesOrder", dispatch_uid="calendar_order_deadlines")
def order_deadlines_follow_order(sender, instance, created, **kwargs):
    if created or kwargs.get("raw"):
        return
    from .order_deadlines import order_changed
    try:
        with transaction.atomic():
            order_changed(instance)
    except Exception as e:  # noqa: BLE001 — календар не заважає зберегти замовлення
        logger.warning("calendar deadlines of order %s: %s", instance.pk, e)
