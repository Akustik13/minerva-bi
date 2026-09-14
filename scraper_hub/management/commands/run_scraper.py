from django.core.management.base import BaseCommand

from scraper_hub.models import ScraperSiteConfig
from scraper_hub.services import run_site_sync


class Command(BaseCommand):
    help = 'Запустити scraper для завантаження рахунків постачальників'

    def add_arguments(self, parser):
        parser.add_argument('--site', help='Назва сайту: jlcpcb, ups, email, custom')
        parser.add_argument('--all',  action='store_true', help='Всі активні сайти')
        parser.add_argument(
            '--cron', action='store_true',
            help='Запустити тільки ті сайти, розклад яких збігається з поточним часом. '
                 'Додайте в системний cron: "* * * * * python manage.py run_scraper --cron"',
        )
        parser.add_argument(
            '--list', action='store_true',
            help='Показати розклад всіх активних сайтів і вийти.',
        )

    def handle(self, *args, **options):
        # ── --list: show schedule and exit ───────────────────────────────────
        if options['list']:
            configs = ScraperSiteConfig.objects.filter(enabled=True)
            if not configs.exists():
                self.stdout.write('Активних конфігурацій немає.')
                return
            self.stdout.write(self.style.SUCCESS('Розклад активних scraper-ів:'))
            self.stdout.write(f'{"Сайт":<20} {"Розклад":<30} {"Cron-вираз":<20} {"Статус"}')
            self.stdout.write('─' * 80)
            for cfg in configs:
                cron = cfg.cron_expression or '—'
                due  = '● зараз' if cfg.is_due_now() else ''
                self.stdout.write(
                    f'{cfg.get_site_name_display():<20} '
                    f'{cfg.schedule_description:<30} '
                    f'{cron:<20} {due}'
                )
            return

        # ── Choose configs ────────────────────────────────────────────────────
        if options['cron']:
            configs = ScraperSiteConfig.objects.filter(enabled=True)
            configs = [c for c in configs if c.is_due_now()]
            if not configs:
                # Silence — cron runs every minute, most runs have nothing to do
                return
        elif options['all']:
            configs = list(ScraperSiteConfig.objects.filter(enabled=True))
        elif options['site']:
            configs = list(ScraperSiteConfig.objects.filter(
                site_name=options['site'], enabled=True,
            ))
        else:
            self.stderr.write(
                'Вкажіть один з аргументів: --site=<назва>, --all, --cron, або --list'
            )
            return

        if not configs:
            self.stderr.write('Активних конфігурацій не знайдено.')
            return

        for cfg in configs:
            self.stdout.write(f'▶ Запускаємо {cfg.get_site_name_display()}…')
            run = run_site_sync(cfg, triggered_by='cron' if options['cron'] else 'manual')
            icon = '✅' if run.status == 'ok' else ('⚠️' if run.status == 'partial' else '❌')
            self.stdout.write(
                f'  {icon} Статус: {run.get_status_display()}, '
                f'Файлів: {run.files_downloaded}'
            )
            if run.error_message:
                self.stderr.write(f'  Помилка: {run.error_message[:200]}')
