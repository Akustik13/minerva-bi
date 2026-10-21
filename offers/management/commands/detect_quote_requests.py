"""Знайти запити пропозицій серед уже отриманої пошти: python manage.py detect_quote_requests [--days 30]"""
from django.core.management.base import BaseCommand

from offers.detection import scan_recent


class Command(BaseCommand):
    help = "Позначити листи, схожі на запит пропозиції (нова пошта позначається автоматично)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)

    def handle(self, *args, **opts):
        n = scan_recent(days=opts["days"])
        self.stdout.write(self.style.SUCCESS(f"Знайдено запитів пропозиції: {n} (за {opts['days']} дн.)"))
