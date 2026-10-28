"""Пошта → журнал бази знань. Підключається ПІСЛЯ email_assistant і projects (порядок INSTALLED_APPS):
на момент виклику лист уже має категорію, клієнта CRM і пропозиції проєктів."""
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from email_assistant.models import EmailAccount, EmailMessage, EmailThread

from .knowledge import IRRELEVANT_FIELDS, safe_touch, start_account_touch

THREAD_FIELDS = frozenset({"subject", "crm_customer", "knowledge_excluded"})
# Поля скриньки, від яких залежать права (access.user_ids, допуск) і mailbox.address у документах
ACCOUNT_FIELDS = ("user_id", "is_active", "email_address")


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


@receiver(pre_save, sender=EmailAccount, dispatch_uid="knowledge_account_before")
def knowledge_account_before(sender, instance, **kwargs):
    if kwargs.get("raw") or not instance.pk:
        return
    instance._knowledge_before = EmailAccount.objects.filter(pk=instance.pk).values_list(*ACCOUNT_FIELDS).first()


@receiver(post_save, sender=EmailAccount, dispatch_uid="knowledge_account_saved")
def knowledge_account_saved(sender, instance, created, **kwargs):
    """Новий власник / вимкнена скринька → документи її листів оновлюються одразу, а не при нічній звірці."""
    if created or kwargs.get("raw"):
        return
    before = getattr(instance, "_knowledge_before", None)
    if before is not None and before != tuple(getattr(instance, f) for f in ACCOUNT_FIELDS):
        account_id = instance.pk
        transaction.on_commit(lambda: start_account_touch(account_id))   # фоновий потік бачить уже збережене


# ── Документи й посилання (KnowledgeItem, тип document) ──────────────────────

@receiver(post_save, sender="rag_assistant.KnowledgeItem", dispatch_uid="knowledge_item_saved")
def knowledge_item_saved(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    from .knowledge_items import touch
    touch([instance.pk])


@receiver(post_delete, sender="rag_assistant.KnowledgeItem", dispatch_uid="knowledge_item_deleted")
def knowledge_item_deleted(sender, instance, **kwargs):
    from .knowledge_items import touch
    touch([instance.pk])
