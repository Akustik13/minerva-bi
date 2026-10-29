"""
Management command: dk_price_sync — ціни DigiKey ← базові ціни Minerva (bots/services/dk_prices.py).

Кожен запуск:
  1. дослати оновлення, що зависли (процес сайту перезапустився посеред надсилання);
  2. перевірити надіслані ціни на DigiKey (✅ діє / ⚠ інші ціни) → сповіщення;
  3. автооновлення — якщо увімкнене і настав час (інтервал у «💲 Налаштуваннях цін DigiKey»).

Usage:
  python manage.py dk_price_sync --if-due     # cron_runner.sh, кожен цикл
  python manage.py dk_price_sync --auto       # автоперевірка зараз, без огляду на інтервал і режим
"""
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Ціни DigiKey ← Minerva: перевірка надісланих і автооновлення"

    def add_arguments(self, parser):
        parser.add_argument("--if-due", action="store_true",
                            help="Автооновлення лише коли настав час (так і без прапорця; для однаковості з cron)")
        parser.add_argument("--auto", action="store_true", help="Автоперевірка зараз (як «▶ Перевірити зараз»)")

    def handle(self, *args, **options):
        from bots.models import DigiKeyPriceSettings
        from bots.services import dk_prices

        DigiKeyPriceSettings.get()
        DigiKeyPriceSettings.objects.filter(pk=1).update(cron_seen_at=timezone.now())
        resumed = dk_prices.resume_stuck()
        if resumed:
            self.stdout.write(f"Дослано завислих оновлень: {resumed}")
        try:
            res = dk_prices.verify()
            if res["checked"]:
                self.stdout.write(f"Перевірено на DigiKey: {res['checked']}, діє {res['verified']}, "
                                  f"інші ціни {res['mismatch']}")
        except Exception as exc:
            self.stderr.write(f"Перевірка цін DigiKey: {exc}")
        if options["auto"] or dk_prices.due():
            self.stdout.write("Автооновлення цін DigiKey: " + dk_prices.auto(force=options["auto"]))
