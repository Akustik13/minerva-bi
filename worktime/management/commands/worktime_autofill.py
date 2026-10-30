"""Автозаповнення робочого часу в askDANTE: порожні робочі дні працівників з галочкою «Автозаповнення».

    python manage.py worktime_autofill --if-due    # з cron: раз на добу після «Запуск о годині», лише якщо увімкнено
    python manage.py worktime_autofill --dry-run   # що записалось би, без запису

З --if-due (кожен цикл cron) ще й «Робочий день сам» (services.auto_day — відкрити о приході), «Пішов» автоматично
після стандартного відходу (services.auto_clock_out) і нагадування про відмітку askDANTE без «Gehen» (services.day_end).
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
            # «Робочий день сам»: о стандартному приході відкрити день (кожен цикл cron, не раз на добу)
            for line in services.auto_day(st):
                self.stdout.write(f"worktime_autofill: робочий день сам — {line}")
            # забули «Пішов» / день відкрила Minerva — після стандартного відходу
            for b in services.auto_clock_out(st):
                self.stdout.write(f"worktime_autofill: «Пішов» автоматично {b.dante_user} {b.date} "
                                  f"{services.hhmm(b.start)}–{services.hhmm(b.stop)} · {b.status} {b.error}".rstrip())
            # кінець дня: відмітка «Kommen» askDANTE без «Gehen» — нагадування (закрити її Minerva не може)
            for line in services.day_end(st):
                self.stdout.write(f"worktime_autofill: кінець дня — {line}")
        if opts["if_due"] and not services.autofill_due(st):
            return
        if st.autofill_mode == DanteSettings.MODE_OFF:
            self.stdout.write("worktime_autofill: автозаповнення вимкнено в налаштуваннях")
            return
        self.stdout.write(f"worktime_autofill: {services.autofill(st, dry_run=opts['dry_run'])}")
