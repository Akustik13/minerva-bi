"""
ai_assistant/management/commands/send_reminders.py
Надіслати прострочені нагадування по CustomerTimeline через Telegram.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Надіслати нагадування юзерам по CustomerTimeline'

    def handle(self, *args, **options):
        from django.utils import timezone
        from crm.models import CustomerTimeline

        now = timezone.now()
        due = (CustomerTimeline.objects
               .filter(
                   remind_at__lte=now,
                   remind_sent=False,
                   event_type='reminder',
               )
               .select_related('customer', 'user__profile'))

        if not due.exists():
            self.stdout.write('Нагадувань не знайдено')
            return

        try:
            import requests
            from strategy.models import AISettings
            token = AISettings.get().telegram_bot_token
        except Exception:
            token = None

        sent = skipped = 0
        for event in due:
            telegram_id = None
            if event.user:
                try:
                    telegram_id = event.user.profile.telegram_id
                except Exception:
                    pass

            if telegram_id and token:
                text = (
                    f'🔔 Нагадування\n'
                    f'Клієнт: {event.customer.name}\n'
                    f'{event.title}'
                )
                try:
                    requests.post(
                        f'https://api.telegram.org/bot{token}/sendMessage',
                        json={'chat_id': telegram_id, 'text': text},
                        timeout=5,
                    )
                    sent += 1
                except Exception as e:
                    self.stderr.write(f'Telegram error: {e}')
                    skipped += 1
            else:
                skipped += 1

            event.remind_sent = True
            event.save(update_fields=['remind_sent'])

        self.stdout.write(f'Надіслано: {sent}, пропущено (немає Telegram): {skipped}')
