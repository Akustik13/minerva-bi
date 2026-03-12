"""
Management command: python manage.py run_backup [--type db|media|full]

Can be scheduled via Windows Task Scheduler or cron for auto-backup.
Example cron (Linux):
  0 3 * * * cd /app && python manage.py run_backup --type full
Example Task Scheduler (Windows, daily at 03:00):
  Program: python
  Arguments: manage.py run_backup --type full
  Start in: C:\\tabele_mvp
"""
from django.core.management.base import BaseCommand

from backup.models import BackupLog
from backup import utils


class Command(BaseCommand):
    help = "Виконати резервне копіювання бази даних та/або медіа файлів"

    def add_arguments(self, parser):
        parser.add_argument(
            "--type",
            choices=["db", "media", "full"],
            default="full",
            help="Тип бекапу: db, media, full (default: full)",
        )

    def handle(self, *args, **options):
        btype = options["type"]
        self.stdout.write(f"Starting {btype} backup...")

        if btype == "db":
            log = utils.run_db_backup()
        elif btype == "media":
            log = utils.run_media_backup()
        else:
            log = utils.run_full_backup()

        if log.status == BackupLog.STATUS_OK:
            self.stdout.write(
                self.style.SUCCESS(
                    f"✅ Backup OK: {log.file_path} "
                    f"({log.file_size} bytes, {log.duration}s)"
                )
            )
        else:
            self.stderr.write(
                self.style.ERROR(f"❌ Backup failed: {log.error_msg}")
            )
