from django.core.management.base import BaseCommand

from scraper_hub.models import ScraperSiteConfig
from scraper_hub.services import run_site


class Command(BaseCommand):
    help = 'Запустити scraper для завантаження рахунків постачальників'

    def add_arguments(self, parser):
        parser.add_argument('--site', help='Назва сайту: jlcpcb, ups')
        parser.add_argument('--all', action='store_true', help='Всі активні сайти')

    def handle(self, *args, **options):
        if options['all']:
            configs = ScraperSiteConfig.objects.filter(enabled=True)
        elif options['site']:
            configs = ScraperSiteConfig.objects.filter(
                site_name=options['site'], enabled=True,
            )
        else:
            self.stderr.write('Вкажіть --site=jlcpcb або --all')
            return

        if not configs.exists():
            self.stderr.write('Активних конфігурацій не знайдено')
            return

        for cfg in configs:
            self.stdout.write(f'▶ Запускаємо {cfg.get_site_name_display()}...')
            run = run_site(cfg, triggered_by='cron')
            icon = '✅' if run.status == 'ok' else ('⚠️' if run.status == 'partial' else '❌')
            self.stdout.write(
                f'  {icon} Статус: {run.get_status_display()}, '
                f'Файлів: {run.files_downloaded}'
            )
            if run.error_message:
                self.stderr.write(f'  Помилка: {run.error_message[:200]}')
