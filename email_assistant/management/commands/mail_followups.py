"""⏰ Нагадування «немає реакції на лист»: строк минув, а ні підтвердження прочитання, ні відповіді — лист і Telegram
власнику скриньки (email_assistant/followups.py run_due). Cron щоцикл; без нагадувань — один порожній запит."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Нагадати про листи без реакції (⏰ «Нагадати, якщо немає реакції»)'

    def handle(self, *args, **options):
        from email_assistant import followups
        out = followups.run_due()
        if out['notified'] or out['closed']:
            self.stdout.write(f"Без реакції: повідомлено {out['notified']}, уже з реакцією {out['closed']}")
