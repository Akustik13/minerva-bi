"""
send_scheduled — відправити заплановані листи.
Запускається кожні 2 хвилини через cron_runner.sh.

Один лист — не більше одного разу:
- перед відправкою лист «бронюється» атомарним UPDATE (sent_at ставиться лише якщо він ще порожній) —
  паралельний запуск (ручний поверх cron, другий контейнер) такий лист пропускає;
- якщо процес упав посеред відправки, бронь лишається: через CLAIM_TIMEOUT лист позначається помилкою
  «невідомо, чи пішов» — краще попросити перевірити «Надіслані», ніж надіслати клієнту двічі.

Вкладення (ScheduledEmail.attachments) читаються вже після броні: файли з комп'ютера — з MEDIA, документи Minerva
(order:/msg:/offer:) — як під час звичайної відправки (attachments.load_items). Якогось файлу вже немає — лист
не йде без нього, а отримує помилку «Вкладення недоступне: …». Надіслано — збережені файли прибираються з диска,
документ Angebot (offer:) стає «надісланим».
"""
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger('email_assistant')

CLAIM_TIMEOUT = timedelta(minutes=15)
UNKNOWN_RESULT = ('Відправку перервано — невідомо, чи лист пішов. Перевірте «Надіслані» і, якщо треба, '
                  'поставте лист у чергу знову (статус «Очікує»).')


class Command(BaseCommand):
    help = 'Відправити заплановані листи (status=pending, scheduled_at <= now)'

    def handle(self, *args, **options):
        from email_assistant import attachments
        from email_assistant.models import ScheduledEmail
        from email_assistant.smtp_client import SMTPClient

        now = timezone.now()
        pending = ScheduledEmail.objects.filter(status=ScheduledEmail.STATUS_PENDING)
        stuck = pending.filter(sent_at__lt=now - CLAIM_TIMEOUT).update(
            status=ScheduledEmail.STATUS_FAILED, error_msg=UNKNOWN_RESULT)
        if stuck:
            logger.warning('send_scheduled: %s лист(ів) з перерваною відправкою позначено помилкою', stuck)

        due = list(pending.filter(sent_at__isnull=True, scheduled_at__lte=now)
                   .select_related('account', 'account__user', 'reply_to').order_by('scheduled_at', 'pk'))
        sent = errors = 0
        for scheduled in due:
            claimed = ScheduledEmail.objects.filter(
                pk=scheduled.pk, status=ScheduledEmail.STATUS_PENDING, sent_at__isnull=True,
            ).update(sent_at=timezone.now())
            if not claimed:
                continue   # уже відправляє інший запуск
            try:
                files, missing = attachments.load_items(scheduled.account, scheduled.attachments or [])
                if missing:   # файл зник — без нього лист не йде
                    result = {'ok': False, 'error': f'Вкладення недоступне: {missing}'}
                else:
                    result = SMTPClient(scheduled.account).send(
                        to_emails=scheduled.to_emails,
                        subject=scheduled.subject,
                        body_text=scheduled.body,
                        body_html=scheduled.body_html or '',
                        cc_emails=scheduled.cc_emails or [],
                        reply_to_message=scheduled.reply_to,
                        attachments=files,
                    )
            except Exception as e:
                result = {'ok': False, 'error': str(e)}
                logger.error('send_scheduled pk=%s: %s', scheduled.pk, e)
            if result['ok']:
                scheduled.status, scheduled.sent_at, scheduled.error_msg = \
                    ScheduledEmail.STATUS_SENT, timezone.now(), ''
                sent += 1
                self.stdout.write(f'✓ {scheduled.subject[:40]}')
            else:   # SMTP відмовив — лист не пішов, бронь знімаємо
                scheduled.status, scheduled.sent_at, scheduled.error_msg = \
                    ScheduledEmail.STATUS_FAILED, None, result.get('error', '')
                errors += 1
                self.stdout.write(f'✗ {result.get("error")}')
            scheduled.save(update_fields=['status', 'sent_at', 'error_msg'])
            if result['ok']:
                self._after_sent(scheduled, result)

        if sent or errors or stuck:
            self.stdout.write(f'Заплановані: {sent} надіслано, {errors} помилок, перервано раніше: {stuck}')

    @staticmethod
    def _after_sent(scheduled, result):
        """Надіслано: Angebot у вкладенні → версія «надіслана»; збережені файли листа — з диска."""
        from email_assistant import attachments
        items = [i for i in scheduled.attachments or [] if isinstance(i, dict)]
        offer_refs = [i['ref'] for i in items if str(i.get('ref', '')).startswith('offer:')]
        try:
            if offer_refs:
                from offers.services import mark_sent_refs
                mark_sent_refs(offer_refs, result.get('message_id', ''))
        except Exception as e:
            logger.warning('send_scheduled pk=%s: Angebot не позначено надісланим: %s', scheduled.pk, e)
        attachments.remove_stored(items)
