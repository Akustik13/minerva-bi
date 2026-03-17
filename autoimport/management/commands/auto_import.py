"""
Management command: python manage.py auto_import

Usage:
    python manage.py auto_import --all                # run all due profiles
    python manage.py auto_import --profile 1          # run specific profile
    python manage.py auto_import --all --force        # ignore schedule, run now
    python manage.py auto_import --all --dry-run      # preview without DB writes
"""
from django.core.management.base import BaseCommand

from autoimport.models import AutoImportProfile
from autoimport.runner import run_profile


class Command(BaseCommand):
    help = 'Run auto-import profiles (scheduled CSV/Excel import from folder or URL)'

    def add_arguments(self, parser):
        parser.add_argument('--profile', type=int, metavar='ID',
                            help='Run a specific profile by ID')
        parser.add_argument('--all', action='store_true',
                            help='Run all enabled profiles that are due')
        parser.add_argument('--force', action='store_true',
                            help='Ignore schedule and run even if not due yet')
        parser.add_argument('--dry-run', action='store_true',
                            help='Parse and validate without writing to DB')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        force   = options['force']

        if options['profile']:
            profile_ids = [options['profile']]
        elif options['all']:
            profile_ids = list(
                AutoImportProfile.objects.filter(enabled=True).values_list('pk', flat=True)
            )
        else:
            self.stderr.write(self.style.ERROR('Вкажіть --all або --profile <ID>'))
            return

        if not profile_ids:
            self.stdout.write('Немає активних профілів для запуску.')
            return

        for pid in profile_ids:
            try:
                profile = AutoImportProfile.objects.get(pk=pid)
            except AutoImportProfile.DoesNotExist:
                self.stderr.write(self.style.ERROR(f'Профіль #{pid} не знайдено'))
                continue

            if not force and not profile.is_due():
                self.stdout.write(f'[SKIP] {profile.name} — ще не час (next: {profile.next_run_at})')
                continue

            mode = 'DRY-RUN' if (dry_run or profile.dry_run_mode) else 'RUN'
            self.stdout.write(f'[{mode}] {profile.name} ({profile.get_import_type_display()}) ...')

            logs = run_profile(pid, dry_run=dry_run, force=force)

            if not logs:
                self.stdout.write('  → Немає файлів для обробки')
                continue

            for log in logs:
                if log.status == 'skipped':
                    self.stdout.write(f'  ⏭  {log.source_name} — дублікат, пропущено')
                elif log.status == 'error':
                    self.stdout.write(self.style.ERROR(
                        f'  ❌ {log.source_name} — помилка: {log.error_detail[:120]}'
                    ))
                elif log.status == 'dry_run':
                    self.stdout.write(self.style.WARNING(
                        f'  🧪 {log.source_name} — dry-run: +{log.records_created} /{log.records_updated}u /{log.records_skipped}s'
                    ))
                else:
                    self.stdout.write(self.style.SUCCESS(
                        f'  ✅ {log.source_name} — +{log.records_created} /{log.records_updated}u /{log.records_skipped}s [{log.duration_ms}ms]'
                    ))
                    if log.errors_count:
                        self.stdout.write(self.style.WARNING(
                            f'     ⚠ {log.errors_count} помилок рядків: {log.error_detail[:200]}'
                        ))
