"""Нарада видалена (кнопкою, прибиранням або каскадом — напр. разом із користувачем) → її документи бази знань
прибираються (tombstone у стрічці для RAG), а MeetingTombstone каже RAG-ПК видалити запис, а не «невідома нарада».
Збій тут не ламає видалення (кожен крок — у своїй точці збереження).
Нарада збережена з головним проєктом → він і серед «📁 Проєктів наради» (meetings/parts.py)."""
import logging

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Meeting, MeetingAgendaFile, MeetingTombstone

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


@receiver(post_save, sender=Meeting)
def meeting_main_project(sender, instance, raw=False, **kwargs):
    """Головний проєкт — завжди серед проєктів наради (create, ✏️ Змінити, ⚡ швидкий запис, будь-який шлях)."""
    if raw or not instance.project_id:
        return
    fields = kwargs.get("update_fields")
    if fields is not None and "project" not in fields:     # стан від RAG-ПК, розшифровка… — проєкт не змінювався
        return
    from . import parts
    parts.ensure_main_link(instance)


@receiver(post_delete, sender=MeetingAgendaFile)
def agenda_file_deleted(sender, instance, **kwargs):
    """📎 Файл агенди прибрано (✕ або разом з нарадою) — і з диска."""
    try:
        if instance.file:
            instance.file.delete(save=False)
    except Exception as e:  # noqa: BLE001
        logger.warning("agenda file %s: %s", instance.pk, e)
