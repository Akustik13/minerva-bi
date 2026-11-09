"""Відправка запланованого листа (ScheduledEmail) — один шлях для cron (`manage.py send_scheduled`) і кнопки
«📤 Надіслати зараз» у «⏳ Запланованих».

Один лист — не більше одного разу:
- перед відправкою лист «бронюється» атомарним UPDATE (sent_at ставиться лише якщо він ще порожній) —
  паралельний запуск (кнопка поверх cron, другий контейнер) такий лист пропускає;
- якщо процес упав посеред відправки, бронь лишається: через CLAIM_TIMEOUT лист позначається помилкою
  «невідомо, чи пішов» — краще попросити перевірити «Надіслані», ніж надіслати клієнту двічі.

Вкладення (ScheduledEmail.attachments) читаються вже після броні: файли з комп'ютера — з MEDIA, документи Minerva
(order:/msg:/offer:) — як під час звичайної відправки (attachments.load_items). Якогось файлу вже немає — лист
не йде без нього, а отримує помилку «Вкладення недоступне: …». Надіслано — збережені файли прибираються з диска,
документ Angebot (offer:) стає «надісланим», лист агента отримує мітку «🤖 агент».
"""
import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger('email_assistant')

CLAIM_TIMEOUT = timedelta(minutes=15)
UNKNOWN_RESULT = ('Відправку перервано — невідомо, чи лист пішов. Перевірте «Надіслані» і, якщо треба, '
                  'поставте лист у чергу знову (статус «Очікує»).')


def release_stuck(now=None) -> int:
    """Броні без результату довше CLAIM_TIMEOUT → помилка «невідомо, чи пішов» (повтору наосліп немає)."""
    from email_assistant.models import ScheduledEmail
    now = now or timezone.now()
    stuck = ScheduledEmail.objects.filter(status=ScheduledEmail.STATUS_PENDING,
                                          sent_at__lt=now - CLAIM_TIMEOUT).update(
        status=ScheduledEmail.STATUS_FAILED, error_msg=UNKNOWN_RESULT)
    if stuck:
        logger.warning('send_scheduled: %s лист(ів) з перерваною відправкою позначено помилкою', stuck)
    return stuck


def send_one(scheduled) -> dict:
    """Надіслати лист, що чекає в черзі, зараз → {'ok': True | False | None, 'error': …}.
    None — лист уже не в черзі (надіслано, скасовано, помилка) або його саме відправляє інший запуск."""
    from email_assistant import attachments
    from email_assistant.models import ScheduledEmail
    from email_assistant.smtp_client import SMTPClient

    claimed = ScheduledEmail.objects.filter(
        pk=scheduled.pk, status=ScheduledEmail.STATUS_PENDING, sent_at__isnull=True,
    ).update(sent_at=timezone.now())
    if not claimed:
        return {'ok': None, 'error': ''}
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
                importance=(scheduled.send_options or {}).get('importance', ''),
                read_receipt=bool((scheduled.send_options or {}).get('read_receipt')),
            )
    except Exception as e:  # noqa: BLE001 — лист не пішов, бронь знімаємо нижче
        result = {'ok': False, 'error': str(e)}
        logger.error('send_scheduled pk=%s: %s', scheduled.pk, e)
    if result['ok']:
        scheduled.status, scheduled.sent_at, scheduled.error_msg = ScheduledEmail.STATUS_SENT, timezone.now(), ''
    else:   # SMTP відмовив — лист не пішов, бронь знімаємо
        scheduled.status, scheduled.sent_at, scheduled.error_msg = \
            ScheduledEmail.STATUS_FAILED, None, result.get('error', '')
    scheduled.save(update_fields=['status', 'sent_at', 'error_msg'])
    if result['ok']:
        after_sent(scheduled, result)
    return {'ok': bool(result['ok']), 'error': result.get('error', '') or ''}


def due(now=None):
    """Листи, час яких настав і які ще ніхто не бронював, — за часом."""
    from email_assistant.models import ScheduledEmail
    return (ScheduledEmail.objects.filter(status=ScheduledEmail.STATUS_PENDING, sent_at__isnull=True,
                                          scheduled_at__lte=now or timezone.now())
            .select_related('account', 'account__user', 'reply_to').order_by('scheduled_at', 'pk'))


def after_sent(scheduled, result):
    """Надіслано: Angebot у вкладенні → версія «надіслана»; збережені файли листа — з диска; лист агента —
    мітка «🤖 агент» у пошті (agent/letters.py); 📬 / ⏰ — стеження за реакцією."""
    from email_assistant import attachments
    try:
        from agent import letters
        letters.scheduled_sent(scheduled, result.get('message_id', ''))
    except Exception as e:  # noqa: BLE001 — лист уже пішов
        logger.warning('send_scheduled pk=%s: мітку листа агента не записано: %s', scheduled.pk, e)
    items = [i for i in scheduled.attachments or [] if isinstance(i, dict)]
    offer_refs = [i['ref'] for i in items if str(i.get('ref', '')).startswith('offer:')]
    try:
        if offer_refs:
            from offers.services import mark_sent_refs   # «надіслано» — лише якщо власник скриньки може змінювати
            mark_sent_refs(offer_refs, result.get('message_id', ''), user=scheduled.account.user)
    except Exception as e:  # noqa: BLE001
        logger.warning('send_scheduled pk=%s: Angebot не позначено надісланим: %s', scheduled.pk, e)
    attachments.remove_stored(items)
    opts = scheduled.send_options or {}
    if opts.get('read_receipt') or opts.get('remind_days'):   # 📬 / ⏰ — копію з «Надісланих» прив'яже sync
        try:
            from email_assistant import followups
            followups.track(scheduled.account, msg_id=result.get('message_id', ''), subject=scheduled.subject,
                            recipients=list(scheduled.to_emails or []) + list(scheduled.cc_emails or []),
                            read_receipt=bool(opts.get('read_receipt')),
                            remind_days=int(opts.get('remind_days') or 0),
                            until=opts.get('until') or 'reaction', user=scheduled.account.user)
        except Exception as e:  # noqa: BLE001 — лист уже пішов
            logger.warning('send_scheduled pk=%s: нагадування не створено: %s', scheduled.pk, e)
