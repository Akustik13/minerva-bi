"""📨 Щотижневий звіт відповідальним за проєкти (projects/weekly.py).

  python manage.py project_weekly --if-due   # cron: лише в день і після години з «⚙️ Налаштування проєктів», раз на день
  python manage.py project_weekly            # зараз, незалежно від розкладу (вимкнений звіт теж)
"""
from django.core.management.base import BaseCommand

from projects import weekly


class Command(BaseCommand):
    help = "Щотижневий звіт відповідальним за проєкти: колір, прогноз, ризики, кроки тижня"

    def add_arguments(self, parser):
        parser.add_argument("--if-due", action="store_true", help="лише коли настав час за налаштуваннями")

    def handle(self, *args, **opts):
        if opts["if_due"] and not (weekly.due() and weekly.claim()):
            return
        if not opts["if_due"]:
            weekly.claim()
        out = weekly.run()
        if opts["verbosity"]:
            self.stdout.write(out["result"])
