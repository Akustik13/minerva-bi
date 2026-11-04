"""
python manage.py check_digikey_messages [--force]

Нові повідомлення DigiKey Marketplace → Telegram і пошта. cron_runner.sh викликає щоцикл, перевірка сама стежить
за «Інтервалом перевірки» в налаштуваннях DigiKey; --force — зараз, навіть якщо вимкнено чи ще рано.
Логіка — bots/services/dk_message_watch.py.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Check DigiKey Marketplace for new buyer messages and notify"

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Перевірити зараз, навіть якщо авто-перевірку вимкнено чи інтервал не минув")

    def handle(self, *args, **options):
        from bots.services import dk_message_watch
        dk_message_watch.run(force=options["force"], log=self.stdout.write)
