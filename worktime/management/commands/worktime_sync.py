"""Записи часу askDANTE (години на проєктах і клієнтах) і місячні підсумки — копія в Minerva для звітів.

    python manage.py worktime_sync --if-due   # з cron: раз на добу після «Запуск о годині», лише якщо увімкнено
    python manage.py worktime_sync            # зараз
"""
import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from worktime import services
from worktime.client import DanteError
from worktime.models import DanteSettings, DanteUser


class Command(BaseCommand):
    help = "Оновити копію записів часу й місячних підсумків askDANTE"

    def add_arguments(self, parser):
        parser.add_argument("--if-due", action="store_true", help="лише раз на добу після години запуску")

    def handle(self, *args, **opts):
        st = DanteSettings.get()
        if not st.enabled or not st.configured:
            return
        now = timezone.localtime()
        if opts["if_due"] and (now.hour < st.autofill_hour or (
                st.records_synced_at and timezone.localtime(st.records_synced_at).date() >= now.date())):
            return
        try:
            if not st.last_sync_at or timezone.now() - st.last_sync_at > datetime.timedelta(hours=20):
                services.sync_directory(st)
            r = services.sync_records(st)
        except DanteError as e:
            self.stdout.write(f"worktime_sync: {e.message}")
            return
        first = now.date().replace(day=1)
        prev = first - datetime.timedelta(days=1)
        du = DanteUser.objects.filter(user__isnull=False, archived=False).first()
        if du:   # askDANTE віддає підсумок усіх працівників одним запитом
            for day in (prev, first):
                services.balance(du, day.year, day.month, max_age_minutes=0)
        self.stdout.write(f"worktime_sync: записів часу {r['records']} ({r['from']:%d.%m}–{r['to']:%d.%m}), "
                          f"прибрано {r['removed']}")
