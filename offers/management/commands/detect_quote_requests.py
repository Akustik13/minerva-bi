"""Знайти запити пропозицій серед уже отриманої пошти: python manage.py detect_quote_requests [--days 30 | --all]
Те саме є в «⚙️ Налаштування Angebote» → «Знайти запити в уже отриманій пошті»."""
from django.core.management.base import BaseCommand

from offers.detection import scan_recent


class Command(BaseCommand):
    help = "Позначити листи, схожі на запит пропозиції (нова пошта позначається автоматично)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--all", action="store_true", help="уся пошта, без обмеження датою")

    def handle(self, *args, **opts):
        days = None if opts["all"] else opts["days"]
        seen, found = scan_recent(days=days)
        period = "уся пошта" if days is None else f"за {days} дн."
        self.stdout.write(self.style.SUCCESS(f"Знайдено запитів пропозиції: {found} ({period}; перевірено листів {seen})"))
