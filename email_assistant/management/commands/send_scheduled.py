"""
send_scheduled — відправити заплановані листи, час яких настав.
Запускається кожні 2 хвилини через cron_runner.sh; уся відправка — `email_assistant/scheduled_send.py`
(ним же — «📤 Надіслати зараз» у «⏳ Запланованих»): бронь атомарним UPDATE, кожен лист не більше одного разу,
перервана відправка через 15 хв → помилка «невідомо, чи пішов».
"""
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Відправити заплановані листи (status=pending, scheduled_at <= now)'

    def handle(self, *args, **options):
        from email_assistant import scheduled_send

        now = timezone.now()
        stuck = scheduled_send.release_stuck(now)
        sent = errors = 0
        for scheduled in list(scheduled_send.due(now)):
            result = scheduled_send.send_one(scheduled)
            if result['ok'] is None:
                continue   # уже відправляє інший запуск
            if result['ok']:
                sent += 1
                self.stdout.write(f'✓ {scheduled.subject[:40]}')
            else:
                errors += 1
                self.stdout.write(f'✗ {result["error"]}')
        if sent or errors or stuck:
            self.stdout.write(f'Заплановані: {sent} надіслано, {errors} помилок, перервано раніше: {stuck}')
