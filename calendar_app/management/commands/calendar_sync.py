"""Календарі: підписки iCal (оновлення), синхронізація Google / Outlook / Minerva за правилами й дедлайни
замовлень (order_deadlines.sweep: зв'язати нові з замовленням, закрити відправлені / скасовані).

    python manage.py calendar_sync --if-due   # з cron: те, чий час настав (підписки — за своїм інтервалом,
                                              # правила — кожні «Синхронізувати кожні, хв»)
"""
from django.core.management.base import BaseCommand

from calendar_app import feeds, order_deadlines, sync


class Command(BaseCommand):
    help = "Оновити підписані календарі й виконати правила синхронізації"

    def add_arguments(self, parser):
        parser.add_argument("--if-due", action="store_true", help="лише те, чий час настав")

    def handle(self, *args, **opts):
        n = feeds.refresh_due()
        if n:
            self.stdout.write(f"calendar_sync: підписок оновлено {n}")
        n = sync.refresh_layers()
        if n:
            self.stdout.write(f"calendar_sync: календарів Google / Outlook у Minerva оновлено {n}")
        for line in sync.run_due():
            self.stdout.write(f"calendar_sync: {line}")
        if opts.get("if_due") and not order_deadlines.sweep_due():
            return
        r = order_deadlines.sweep()
        if r["linked"] or r["closed"] or r["reopened"]:
            self.stdout.write(f"calendar_sync: дедлайни замовлень — зв'язано {r['linked']}, закрито {r['closed']}, "
                              f"відкрито знову {r['reopened']}")
