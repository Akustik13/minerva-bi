"""Автоперевірка онлайн-оплат Stripe (cron щохвилини): поки вебхук не працює, раз на «Автоперевірка оплат, хв»
Minerva питає Stripe про незавершені оплати. --now — перевірити всі незавершені зараз (як кнопка в адмінці)."""
from django.core.management.base import BaseCommand

from shop import payments


class Command(BaseCommand):
    help = "Перевіряє в Stripe незавершені онлайн-оплати (автоперевірка за налаштуваннями магазину)"

    def add_arguments(self, parser):
        parser.add_argument("--now", action="store_true", help="Перевірити всі незавершені оплати зараз")

    def handle(self, *args, **options):
        if options["now"]:
            self.stdout.write(payments.stats_text(payments.check_payments(payments.unfinished())))
            return
        for shop, stats in payments.run_poll():
            if stats["checked"]:
                self.stdout.write(f"check_stripe_payments {shop.slug}: {payments.stats_text(stats)}")
