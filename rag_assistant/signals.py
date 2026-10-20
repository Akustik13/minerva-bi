"""Пошта → журнал бази знань. Підключається ПІСЛЯ email_assistant і projects (порядок INSTALLED_APPS):
на момент виклику лист уже має категорію, клієнта CRM і пропозиції проєктів."""
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from email_assistant.models import EmailMessage, EmailThread

from .knowledge import IRRELEVANT_FIELDS, safe_touch

THREAD_FIELDS = frozenset({"subject", "crm_customer", "knowledge_excluded"})


@receiver(post_save, sender=EmailMessage, dispatch_uid="knowledge_message_saved")
def knowledge_message_saved(sender, instance, created, update_fields=None, **kwargs):
    if kwargs.get("raw"):
        return
    if created:
        # новий лист міг змінити клієнта всієї гілки (crm_link оновлює гілку запитом) — перевірити гілку
        safe_touch(pks=[instance.pk], thread_id=instance.thread_id)
    elif update_fields is None or not set(update_fields) <= IRRELEVANT_FIELDS:
        safe_touch(pks=[instance.pk])


@receiver(post_delete, sender=EmailMessage, dispatch_uid="knowledge_message_deleted")
def knowledge_message_deleted(sender, instance, **kwargs):
    safe_touch(pks=[instance.pk])


@receiver(post_save, sender=EmailThread, dispatch_uid="knowledge_thread_saved")
def knowledge_thread_saved(sender, instance, created, update_fields=None, **kwargs):
    if created or kwargs.get("raw"):
        return
    if update_fields is None or set(update_fields) & THREAD_FIELDS:
        safe_touch(thread_id=instance.pk)
