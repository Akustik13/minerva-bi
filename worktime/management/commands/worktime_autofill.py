"""Автозаповнення робочого часу в askDANTE: порожні робочі дні працівників з галочкою «Автозаповнення».

    python manage.py worktime_autofill --if-due    # з cron: раз на добу після «Запуск о годині», лише якщо увімкнено
    python manage.py worktime_autofill --dry-run   # що записалось би, без запису

З --if-due (кожен цикл cron) ще й «Пішов» автоматично після стандартного відходу (services.auto_clock_out).
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from worktime import services
from worktime.models import DanteSettings


class Command(BaseCommand):
    help = "Заповнити порожні робочі дні в askDANTE (режим і період — у налаштуваннях askDANTE)"

    def add_arguments(self, parser):
        parser.add_argument("--if-due", action="store_true", help="лише якщо сьогодні ще не запускалось")
        parser.add_argument("--dry-run", action="store_true", help="лише показати, нічого не записувати")

    def handle(self, *args, **opts):
        st = DanteSettings.get()
        if opts["if_due"]:   # виклик із cron — відмітка «cron живий» для сторінки налаштувань
            DanteSettings.objects.filter(pk=st.pk).update(cron_seen_at=timezone.now())
            # забули «Пішов» — після стандартного відходу (кожен цикл cron, не раз на добу)
            for b in services.auto_clock_out(st):
                self.stdout.write(f"worktime_autofill: «Пішов» автоматично {b.dante_user} {b.date} "
                                  f"{services.hhmm(b.start)}–{services.hhmm(b.stop)} · {b.status} {b.error}".rstrip())
        if opts["if_due"] and not services.autofill_due(st):
            return
        if st.autofill_mode == DanteSettings.MODE_OFF:
            self.stdout.write("worktime_autofill: автозаповнення вимкнено в налаштуваннях")
            return
        self.stdout.write(f"worktime_autofill: {services.autofill(st, dry_run=opts['dry_run'])}")
