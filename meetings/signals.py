"""Нарада видалена (кнопкою, прибиранням або каскадом — напр. разом із користувачем) → її документи бази знань
прибираються (tombstone у стрічці для RAG), а MeetingTombstone каже RAG-ПК видалити запис, а не «невідома нарада».
Збій тут не ламає видалення (кожен крок — у своїй точці збереження)."""
import logging

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import Meeting, MeetingTombstone

logger = logging.getLogger(__name__)


@receiver(post_delete, sender=Meeting)
def meeting_deleted(sender, instance, **kwargs):
    try:
        with transaction.atomic():
            MeetingTombstone.objects.create(
                meeting_id=instance.pk, uid=instance.uid, bench_session=instance.bench_session or "",
                old_sessions=list(instance.old_sessions or [])[-20:],
                reason=(getattr(instance, "_delete_reason", "") or "deleted")[:20])
    except Exception as e:  # noqa: BLE001
        logger.warning("meeting %s tombstone: %s", instance.pk, e)
    try:
        from . import knowledge
        with transaction.atomic():
            knowledge.unpublish(instance)
    except Exception as e:  # noqa: BLE001
        logger.warning("meeting %s unpublish: %s", instance.pk, e)
