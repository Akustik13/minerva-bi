"""Надіслані Angebote з минулим строком дії → «Прострочено» (offers.services.expire_overdue).

    python manage.py expire_offers   # з cron раз на добу (cron_runner.sh); повтор нічого не змінює

Раніше статус мінявся лише коли хтось відкривав список Angebote в адмінці.
"""
from django.core.management.base import BaseCommand

from offers import services


class Command(BaseCommand):
    help = "Позначити прострочені надіслані Angebote (строк дії минув)"

    def handle(self, *args, **opts):
        n = services.expire_overdue()
        if n or opts.get("verbosity", 1) > 1:
            self.stdout.write(f"expire_offers: прострочено {n}")
