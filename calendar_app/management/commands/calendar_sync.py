"""Календарі: підписки iCal (оновлення) і синхронізація Google / Outlook / Minerva за правилами.

    python manage.py calendar_sync --if-due   # з cron: те, чий час настав (підписки — за своїм інтервалом,
                                              # правила — кожні «Синхронізувати кожні, хв»)
"""
from django.core.management.base import BaseCommand

from calendar_app import feeds, sync


class Command(BaseCommand):
    help = "Оновити підписані календарі й виконати правила синхронізації"

    def add_arguments(self, parser):
        parser.add_argument("--if-due", action="store_true", help="лише те, чий час настав")

    def handle(self, *args, **opts):
        n = feeds.refresh_due()
        if n:
            self.stdout.write(f"calendar_sync: підписок оновлено {n}")
        for line in sync.run_due():
            self.stdout.write(f"calendar_sync: {line}")
