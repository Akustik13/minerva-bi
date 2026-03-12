"""Management command: send email alerts for critical stock and overdue deadlines.

Usage:
    python manage.py send_alerts            # skip if sent recently
    python manage.py send_alerts --force    # always send (ignores interval)
    python manage.py send_alerts --test     # test SMTP only (no real alerts)

Docker / cron example (every 12 hours):
    0 */12 * * * docker-compose exec -T web python manage.py send_alerts
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

    def handle(self, *args, **options):
        from dashboard.notifications import run_alerts

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
