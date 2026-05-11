"""
send_scheduled — відправити заплановані листи.
Запускається кожні 2 хвилини через cron_runner.sh.
"""
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger('email_assistant')


class Command(BaseCommand):
    help = 'Відправити заплановані листи (status=pending, scheduled_at <= now)'

    def handle(self, *args, **options):
        from email_assistant.models import ScheduledEmail
        from email_assistant.smtp_client import SMTPClient

        now = timezone.now()
        due = (ScheduledEmail.objects
               .filter(status=ScheduledEmail.STATUS_PENDING, scheduled_at__lte=now)
               .select_related('account', 'account__user'))

        sent = errors = 0
        for scheduled in due:
            try:
                result = SMTPClient(scheduled.account).send(
                    to_emails=scheduled.to_emails,
                    subject=scheduled.subject,
                    body_text=scheduled.body,
                    body_html=scheduled.body_html or '',
                    cc_emails=scheduled.cc_emails or [],
                )
                if result['ok']:
                    scheduled.status  = ScheduledEmail.STATUS_SENT
                    scheduled.sent_at = now
                    sent += 1
                    self.stdout.write(f'✓ {scheduled.subject[:40]}')
                else:
                    scheduled.status    = ScheduledEmail.STATUS_FAILED
                    scheduled.error_msg = result.get('error', '')
                    errors += 1
                    self.stdout.write(f'✗ {result.get("error")}')
                scheduled.save()
            except Exception as e:
                scheduled.status    = ScheduledEmail.STATUS_FAILED
                scheduled.error_msg = str(e)
                scheduled.save()
                errors += 1
                logger.error('send_scheduled pk=%s: %s', scheduled.pk, e)

        if sent or errors:
            self.stdout.write(f'Заплановані: {sent} надіслано, {errors} помилок')
