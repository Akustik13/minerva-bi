"""Management command: send email alerts for critical stock and overdue deadlines.

Usage:
    python manage.py send_alerts            # skip if sent recently
    python manage.py send_alerts --if-due   # cron: as above + pause after a failed attempt, quiet if nothing to do
    python manage.py send_alerts --force    # always send (ignores interval)
    python manage.py send_alerts --test     # test SMTP only (no real alerts)

Розклад сервера (cron_runner.sh) запускає `send_alerts --if-due` кожні 15 хв: попередження йдуть не частіше,
ніж «Мінімальний інтервал між надсиланнями (год)» з «Налаштувань сповіщень» (last_alert_sent), лише коли
є критичний склад / прострочені замовлення і ввімкнено відповідні галочки. Невдала спроба — повтор не раніше
ніж за 30 хв і не більше 3 разів на день (dashboard/schedule_guard.py).
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Send email alerts: critical stock + overdue shipping deadlines'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force', action='store_true',
            help='Send even if recently sent (ignore interval check)',
        )
        parser.add_argument(
            '--test', action='store_true',
            help='Send a test email to verify SMTP settings (no real alerts)',
        )
        parser.add_argument(
            '--if-due', action='store_true',
            help='For cron: respect the interval, pause after a failed attempt, print only sent / errors',
        )

    def handle(self, *args, **options):
        from dashboard import schedule_guard
        from dashboard.notifications import run_alerts

        if options['if_due']:
            if not schedule_guard.may_try('alerts'):
                return
            result = run_alerts()
            if result.get('sent'):
                schedule_guard.clear('alerts')
                self.stdout.write(self.style.SUCCESS(
                    f"✅ Alert sent: {result.get('critical', 0)} critical stock, "
                    f"{result.get('overdue', 0)} overdue orders"
                ))
            elif schedule_guard.channel_errors(result):
                schedule_guard.note_failure('alerts')
                self.stdout.write(self.style.WARNING(
                    f"⚠️  Not sent: {'; '.join(schedule_guard.channel_errors(result))}"))
            return

        result = run_alerts(force=options['force'], is_test=options['test'])

        if result.get('sent'):
            if result.get('is_test'):
                em = result.get('email', {})
                tg = result.get('telegram', {})
                if em.get('sent'):
                    self.stdout.write(self.style.SUCCESS('✅ Test email sent successfully'))
                elif em.get('error'):
                    self.stdout.write(self.style.WARNING(f'📧 Email error: {em["error"]}'))
                if tg.get('sent'):
                    self.stdout.write(self.style.SUCCESS('📱 Telegram: test message sent'))
                elif tg.get('error'):
                    self.stdout.write(self.style.WARNING(f'📱 Telegram error: {tg["error"]}'))
            else:
                self.stdout.write(self.style.SUCCESS(
                    f"✅ Alert sent: {result.get('critical', 0)} critical stock, "
                    f"{result.get('overdue', 0)} overdue orders"
                ))
                tg = result.get('telegram', {})
                if tg.get('sent'):
                    self.stdout.write('📱 Telegram: надіслано')
                elif tg.get('error'):
                    self.stdout.write(self.style.WARNING(f'📱 Telegram помилка: {tg["error"]}'))
        else:
            reason = result.get('reason') or result.get('error') or '?'
            if result.get('ok'):
                self.stdout.write(f'ℹ️  No alerts to send: {reason}')
            else:
                self.stdout.write(self.style.WARNING(f'⚠️  Not sent: {reason}'))
