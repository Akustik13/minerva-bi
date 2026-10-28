"""Час на пошту → askDANTE (worktime/mail_time.py): за останні дні до вчора дописати різницю там, де своя категорія
листів (або проєкти Minerva) записується автоматично.

    python manage.py worktime_mail_time --if-due    # з cron: раз на добу після «Запуск о годині», лише якщо увімкнено
    python manage.py worktime_mail_time --dry-run   # що записалось би, без запису
"""
from django.core.management.base import BaseCommand

from worktime import mail_time
from worktime.models import DanteSettings


class Command(BaseCommand):
    help = "Записати в askDANTE час на пошту (автоматичні категорії листів і проєкти)"

    def add_arguments(self, parser):
        parser.add_argument("--if-due", action="store_true", help="лише раз на добу після години запуску")
        parser.add_argument("--dry-run", action="store_true", help="нічого не записувати")

    def handle(self, *args, **opts):
        st = DanteSettings.get()
        if opts["if_due"] and not mail_time.due(st):
            return
        self.stdout.write("worktime_mail_time: " + mail_time.auto(st, dry_run=opts["dry_run"]))
