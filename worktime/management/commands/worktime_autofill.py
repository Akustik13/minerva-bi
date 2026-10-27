"""Автозаповнення робочого часу в askDANTE: порожні робочі дні працівників з галочкою «Автозаповнення».

    python manage.py worktime_autofill --if-due    # з cron: раз на добу після «Запуск о годині», лише якщо увімкнено
    python manage.py worktime_autofill --dry-run   # що записалось би, без запису
"""
from django.core.management.base import BaseCommand

from worktime import services
from worktime.models import DanteSettings


class Command(BaseCommand):
    help = "Заповнити порожні робочі дні в askDANTE (режим і період — у налаштуваннях askDANTE)"

    def add_arguments(self, parser):
        parser.add_argument("--if-due", action="store_true", help="лише якщо сьогодні ще не запускалось")
        parser.add_argument("--dry-run", action="store_true", help="лише показати, нічого не записувати")

    def handle(self, *args, **opts):
        st = DanteSettings.get()
        if opts["if_due"] and not services.autofill_due(st):
            return
        if st.autofill_mode == DanteSettings.MODE_OFF:
            self.stdout.write("worktime_autofill: автозаповнення вимкнено в налаштуваннях")
            return
        self.stdout.write(f"worktime_autofill: {services.autofill(st, dry_run=opts['dry_run'])}")
