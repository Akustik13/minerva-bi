"""sync_email — завантажити нові листи для всіх активних EmailAccount."""
import logging
from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger('email_assistant')


class Command(BaseCommand):
    help = 'Синхронізувати пошту для всіх активних акаунтів'

    def add_arguments(self, parser):
        parser.add_argument('--user',    metavar='USERNAME', help='Тільки для цього юзера')
        parser.add_argument('--account', type=int,           help='Тільки для цього EmailAccount pk')
        parser.add_argument('--dry-run', action='store_true', help='Показати без збереження')
        parser.add_argument('--all',     action='store_true', help='Повна синхронізація (ігнорує last_seen_uid, days_back=3650)')

    def handle(self, *args, **options):
        from email_assistant.models import EmailAccount

        qs = EmailAccount.objects.filter(is_active=True).select_related('user')
        if options['user']:
            qs = qs.filter(user__username=options['user'])
        if options['account']:
            qs = qs.filter(pk=options['account'])

        if not qs.exists():
            self.stdout.write('Немає активних email акаунтів')
            return

        sync_all = options.get('all', False)
        total_new = total_err = 0
        for account in qs:
            self.stdout.write(f'--- {account.user.username} / {account.email_address} ---')
            try:
                new, err = self._sync_account(account, options['dry_run'], sync_all=sync_all)
                total_new += new
                total_err += err
                self.stdout.write(f'  +{new} нових, {err} помилок')
            except Exception as e:
                total_err += 1
                logger.error('sync_email account %s: %s', account.pk, e)
                self.stdout.write(f'  ERROR: {e}')

        self.stdout.write(f'\nРезультат: +{total_new} нових листів, {total_err} помилок')

    def _sync_account(self, account, dry_run: bool, sync_all: bool = False):
        from email_assistant.models import EmailMessage, EmailThread
        from email_assistant.imap_client import IMAPClient

        created = errors = 0
        days_back = 3650 if sync_all else account.sync_days_back
        limit = None if account.sync_no_limit else account.sync_limit

        with IMAPClient(account) as client:
            for folder, folder_type in [
                (account.imap_folder_inbox, EmailMessage.FOLDER_INBOX),
                (account.imap_folder_sent,  EmailMessage.FOLDER_SENT),
            ]:
                if not folder:
                    continue

                since_uid = 0 if sync_all else (
                    account.last_seen_uid if folder_type == EmailMessage.FOLDER_INBOX else 0
                )
                messages  = client.fetch_messages(
                    folder=folder,
                    days_back=days_back,
                    since_uid=since_uid,
                    limit=limit,
                )

                for msg_data in messages:
                    try:
                        if EmailMessage.objects.filter(
                                account=account,
                                imap_uid=msg_data['uid'],
                                imap_folder_name=folder).exists():
                            continue

                        if dry_run:
                            self.stdout.write(
                                f'  [DRY] {folder_type} uid={msg_data["uid"]} | {msg_data["subject"][:50]}')
                            created += 1
                            continue

                        thread       = self._get_or_create_thread(account, msg_data)
                        crm_customer = self._find_crm_customer(msg_data)
                        if crm_customer and thread and not thread.crm_customer_id:
                            thread.crm_customer = crm_customer
                            thread.save(update_fields=['crm_customer'])

                        em = EmailMessage.objects.create(
                            account=account,
                            thread=thread,
                            imap_uid=msg_data['uid'],
                            imap_folder_name=folder,
                            message_id=msg_data['message_id'],
                            in_reply_to=msg_data['in_reply_to'],
                            folder=folder_type,
                            subject=msg_data['subject'],
                            from_email=msg_data['from_email'],
                            from_name=msg_data['from_name'],
                            to_emails=msg_data['to_emails'],
                            cc_emails=msg_data['cc_emails'],
                            body_text=msg_data['body_text'],
                            body_html=msg_data['body_html'],
                            attachments=msg_data['attachments'],
                            is_read=msg_data['is_read'],
                            sent_at=msg_data['sent_at'],
                        )

                        if thread:
                            thread.message_count  = thread.messages.count()
                            thread.last_message_at = msg_data['sent_at']
                            if not msg_data['is_read']:
                                thread.has_unread = True
                            thread.save(update_fields=['message_count', 'last_message_at', 'has_unread'])

                        if crm_customer:
                            self._sync_to_crm(em, crm_customer, account)

                        if folder_type == EmailMessage.FOLDER_INBOX and not msg_data['is_read']:
                            self._notify_new_email(em, account)

                        if folder_type == EmailMessage.FOLDER_INBOX:
                            self._post_process_inbox(em, account, thread)

                        created += 1

                    except Exception as e:
                        errors += 1
                        logger.error('save message uid=%s: %s', msg_data.get('uid'), e)

                if not dry_run and messages and folder_type == EmailMessage.FOLDER_INBOX:
                    max_uid = max(m['uid'] for m in messages)
                    if max_uid > account.last_seen_uid:
                        account.last_seen_uid = max_uid
                        account.last_sync_at  = timezone.now()
                        account.save(update_fields=['last_seen_uid', 'last_sync_at'])

        return created, errors

    def _get_or_create_thread(self, account, msg_data):
        from email_assistant.models import EmailThread

        thread_key = (msg_data.get('in_reply_to') or
                      msg_data.get('message_id') or
                      msg_data['subject'])

        thread = EmailThread.objects.filter(account=account, thread_id=thread_key[:500]).first()
        if not thread:
            clean = msg_data['subject']
            for prefix in ('Re: ', 'Fwd: ', 'FW: ', 'RE: ', 'AW: ', 'WG: '):
                clean = clean.replace(prefix, '')
            thread = EmailThread.objects.filter(account=account, subject=clean.strip()).first()

        if not thread:
            thread = EmailThread.objects.create(
                account=account,
                thread_id=thread_key[:500] if thread_key else '',
                subject=msg_data['subject'][:500],
                participants=[msg_data['from_email']] + msg_data['to_emails'],
            )
        return thread

    def _find_crm_customer(self, msg_data):
        try:
            from crm.models import Customer
            for e in [msg_data['from_email']] + msg_data['to_emails']:
                if not e:
                    continue
                c = Customer.objects.filter(email__iexact=e).first()
                if c:
                    return c
        except Exception:
            pass
        return None

    def _notify_new_email(self, email_msg, account):
        """Send Telegram notification for a new unread inbox email."""
        try:
            from email_assistant.models import EmailSettings
            es = EmailSettings.get_for_user(account.user)
            if not es.telegram_notify_new:
                return

            # Quiet hours check
            if es.telegram_quiet_from and es.telegram_quiet_to:
                from django.utils import timezone as tz
                now_time = tz.localtime().time()
                qf, qt = es.telegram_quiet_from, es.telegram_quiet_to
                if qf < qt:
                    if qf <= now_time < qt:
                        return
                else:  # wraps midnight
                    if now_time >= qf or now_time < qt:
                        return

            from dashboard.notifications import _get_ns
            ns = _get_ns()
            if not ns or not getattr(ns, 'telegram_bot_token', None):
                return

            sender = email_msg.from_name or email_msg.from_email
            subj   = email_msg.subject or '(без теми)'
            text = (
                f'📧 <b>Новий лист</b>\n'
                f'Від: {sender}\n'
                f'Тема: {subj}\n'
                f'Акаунт: {account.email_address}'
            )

            # Prefer private user Telegram over general channel
            try:
                profile = account.user.profile
                tid = getattr(profile, 'telegram_id', None)
                if tid:
                    self._send_telegram_private(ns.telegram_bot_token, tid, text)
                    return
            except Exception:
                pass

            # Fallback: general channel
            if getattr(ns, 'telegram_chat_id', None):
                from dashboard.notifications import _send_telegram
                _send_telegram(ns, text)

        except Exception as e:
            logger.warning('Telegram email notify failed: %s', e)

    def _send_telegram_private(self, token, chat_id, text):
        import urllib.request, urllib.parse
        data = urllib.parse.urlencode({
            'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML',
        }).encode()
        urllib.request.urlopen(
            urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage', data=data),
            timeout=5,
        )

    def _post_process_inbox(self, em, account, thread):
        """Run rules + deadline detection + auto-reply for a freshly saved inbox message."""
        from email_assistant.models import EmailSettings
        es = EmailSettings.get_for_user(account.user)

        self._apply_rules(em, account)

        if es.deadline_detection:
            self._detect_deadlines(em, account)

        if es.auto_reply_enabled:
            self._auto_reply(em, account, es, thread)

    def _apply_rules(self, em, account):
        """Check active rules and apply the first matching one."""
        from email_assistant.models import EmailRule
        try:
            for rule in account.rules.filter(is_active=True):
                if rule.matches(em):
                    rule.apply_to(em)
                    logger.info('Rule "%s" applied to uid=%s', rule.name, em.imap_uid)
                    break
        except Exception as e:
            logger.warning('apply_rules uid=%s: %s', em.imap_uid, e)

    def _detect_deadlines(self, em, account):
        """Extract deadlines from email body and create CalendarEvent records."""
        from email_assistant import ai_helper
        from calendar_app.models import CalendarEvent
        from django.utils.dateparse import parse_datetime
        from django.utils import timezone

        try:
            profile = account.user.profile
        except Exception:
            profile = None

        deadlines = ai_helper.extract_deadlines(em.body_text or '', profile)
        for d in deadlines:
            try:
                dt = parse_datetime(d.get('date', ''))
                if dt is None:
                    continue
                if timezone.is_naive(dt):
                    dt = timezone.make_aware(dt)
                if CalendarEvent.objects.filter(
                        user=account.user, email_message=em,
                        start_at=dt).exists():
                    continue
                CalendarEvent.objects.create(
                    user=account.user,
                    title=d.get('title', 'Дедлайн з листа')[:300],
                    description=d.get('description', '')[:500],
                    event_type=CalendarEvent.TYPE_DEADLINE,
                    start_at=dt,
                    email_message=em,
                    remind_minutes_before=60,
                )
                logger.info('Calendar event created from email uid=%s: %s', em.imap_uid, d.get('title'))
            except Exception as exc:
                logger.error('deadline create error: %s', exc)

    def _auto_reply(self, em, account, es, thread):
        """Generate AI reply for inbox message; save as draft or schedule to send."""
        from email_assistant import ai_helper
        from email_assistant.models import EmailDraft, ScheduledEmail
        from django.utils import timezone

        try:
            profile = account.user.profile
        except Exception:
            profile = None

        msgs = list(thread.messages.order_by('sent_at')) if thread else [em]
        reply_text = ai_helper.generate_reply(msgs, account, profile)
        if not reply_text:
            return

        subject = em.subject or ''
        if not subject.lower().startswith('re:'):
            subject = f'Re: {subject}'

        to_emails = [em.from_email] if em.from_email else []

        if es.auto_reply_mode == 'draft':
            EmailDraft.objects.create(
                account=account,
                reply_to=em,
                subject=subject,
                to_emails=to_emails,
                body=reply_text,
            )
            logger.info('Auto-reply draft created for uid=%s', em.imap_uid)
        else:
            ScheduledEmail.objects.create(
                account=account,
                subject=subject,
                to_emails=to_emails,
                body=reply_text,
                scheduled_at=timezone.now(),
                trigger='auto_reply',
            )
            logger.info('Auto-reply scheduled for uid=%s', em.imap_uid)

    def _sync_to_crm(self, email_msg, customer, account):
        try:
            from crm.models import CustomerTimeline
            event_type = 'email_out' if email_msg.folder == 'sent' else 'email_in'
            if CustomerTimeline.objects.filter(
                    customer=customer,
                    related_email_id=email_msg.imap_uid,
                    event_type=event_type).exists():
                return
            CustomerTimeline.objects.create(
                customer=customer,
                user=account.user,
                event_type=event_type,
                title=email_msg.subject or '(без теми)',
                body=(email_msg.body_text or '')[:1000],
                related_email_id=email_msg.imap_uid,
                created_at=email_msg.sent_at or timezone.now(),
            )
        except Exception as e:
            logger.error('CRM sync error: %s', e)
