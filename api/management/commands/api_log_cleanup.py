"""Журнал API: видалити записи, старші за API_LOG_DAYS (60 днів). cron_runner.sh — раз на добу."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Видаляє з «📜 Журналу API» записи, старші за API_LOG_DAYS днів"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None, help="Скільки днів зберігати (замість API_LOG_DAYS)")

    def handle(self, *args, **options):
        from api.key_limits import LOG_DAYS, cleanup
        n = cleanup(options["days"])
        self.stdout.write(f"Журнал API: видалено {n} записів старших за {options['days'] or LOG_DAYS} днів")
