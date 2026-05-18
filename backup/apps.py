import logging
import os
import threading
import time

from django.apps import AppConfig

logger = logging.getLogger(__name__)

_SCHEDULER_GUARD = "MINERVA_BACKUP_SCHEDULER_STARTED"
_CHECK_INTERVAL  = 3600   # перевіряємо кожну годину


def _backup_scheduler():
    """Daemon thread: checks every hour if an auto-backup is due and runs it."""
    # Small initial delay so Django is fully loaded before first DB access
    time.sleep(60)

    while True:
        try:
            _maybe_run_backup()
        except Exception:
            logger.exception("backup scheduler: unexpected error")
        time.sleep(_CHECK_INTERVAL)


def _maybe_run_backup():
    from django.utils import timezone
    from datetime import timedelta
    from backup.models import BackupLog, BackupSettings
    from backup import utils

    cfg = BackupSettings.get_settings()
    if not cfg.auto_enabled:
        return

    # Determine interval
    if cfg.schedule == "weekly":
        interval = timedelta(days=7)
    else:
        interval = timedelta(days=1)

    # Find last successful backup (any type counts as a run)
    last = (
        BackupLog.objects
        .filter(status=BackupLog.STATUS_OK)
        .exclude(backup_type=BackupLog.TYPE_SETTINGS)
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )

    if last and (timezone.now() - last) < interval:
        return  # not yet due

    logger.info("backup scheduler: starting scheduled %s backup", cfg.schedule)
    if cfg.include_media:
        log = utils.run_full_backup()
    else:
        log = utils.run_db_backup()

    if log.status == BackupLog.STATUS_OK:
        logger.info(
            "backup scheduler: done — %s, %.1fs", log.file_path, log.duration
        )
    else:
        logger.error("backup scheduler: failed — %s", log.error_msg)


class BackupConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "backup"
    verbose_name = "💾 Резервне копіювання"

    def ready(self):
        # Guard against double-start (Django dev autoreloader forks twice)
        if os.environ.get(_SCHEDULER_GUARD):
            return
        os.environ[_SCHEDULER_GUARD] = "1"

        t = threading.Thread(target=_backup_scheduler, daemon=True, name="BackupScheduler")
        t.start()
        logger.info("backup scheduler thread started")
