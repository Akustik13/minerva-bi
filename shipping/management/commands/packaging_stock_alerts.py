"""📦 Коробок досі мало — нагадати знову (shipping/services/packaging_stock.remind_due).

Cron щоцикл (cron_runner.sh); без «Нагадувати кожні N днів» у «Налаштуваннях доставки» нічого не робить.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Нагадати про коробки, яких досі мало (Налаштування доставки → 📦 Запас коробок)."

    def handle(self, *args, **options):
        from shipping.services.packaging_stock import remind_due
        due = remind_due()
        if due and options.get("verbosity", 1) > 0:
            self.stdout.write(f"📦 Нагадали про коробки: {', '.join(str(m) for m in due)}")
