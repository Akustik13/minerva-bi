"""🎙 Прибирання нарад (meetings/services.py).

  python manage.py meetings_cleanup --if-due   # cron, щоциклу: завислі записи (sweep_stale) — завжди; повне
                                               # прибирання (cleanup) — не частіше, ніж раз на 20 год
  python manage.py meetings_cleanup            # усе зараз

Повне прибирання: розшифровки після строку зберігання, протоколи після protocol_retention_days, ні разу не записані
підготовки, вимкнені власники, старі сліди видалених нарад. Повідомлення «не вдалося» надсилаються тут же, у цьому
процесі (не у фоновому потоці: команда одразу завершується, і фоновий потік зник би разом із нею).
"""
from django.core.management.base import BaseCommand

from meetings import notify, services


class Command(BaseCommand):
    help = "Наради: завислі записи й прибирання за строками зберігання"

    def add_arguments(self, parser):
        parser.add_argument("--if-due", action="store_true",
                            help="повне прибирання — лише якщо за 20 год ще не прибирали (завислі записи — завжди)")

    def handle(self, *args, **opts):
        background = notify.BACKGROUND
        notify.BACKGROUND = False
        try:
            if services.claim_cleanup() or not opts["if_due"]:
                out = services.cleanup()
            else:
                out = {"stale": services.sweep_stale()}
        finally:
            notify.BACKGROUND = background
        if opts["verbosity"] > 1 or (opts["verbosity"] and not opts["if_due"]):
            self.stdout.write(", ".join(f"{k}: {v}" for k, v in out.items()))
