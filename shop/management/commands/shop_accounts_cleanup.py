"""Прибирання кабінету покупців: прострочені сесії й посилання з листів, непідтверджені реєстрації (30 днів)."""
from django.core.management.base import BaseCommand

from shop.accounts import cleanup


class Command(BaseCommand):
    help = "Видаляє прострочені сесії покупців, старі посилання з листів і непідтверджені реєстрації старші 30 днів"

    def handle(self, *args, **options):
        stats = cleanup()
        self.stdout.write(f"shop_accounts_cleanup: сесій {stats['sessions']}, посилань {stats['tokens']}, "
                          f"непідтверджених акаунтів {stats['unverified']}")
