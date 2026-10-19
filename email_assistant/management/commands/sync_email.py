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
        parser.add_argument('--folders', action='store_true',
                            help='Також усі інші папки сервера (нові листи за UID) — для крону')

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
                new, err = self._sync_account(account, options['dry_run'], sync_all=sync_all,
                                              with_folders=options['folders'])
                total_new += new
                total_err += err
                self.stdout.write(f'  +{new} нових, {err} помилок')
            except Exception as e:
                total_err += 1
                logger.error('sync_email account %s: %s', account.pk, e)
                self.stdout.write(f'  ERROR: {e}')

        self.stdout.write(f'\nРезультат: +{total_new} нових листів, {total_err} помилок')

    def _sync_account(self, account, dry_run: bool, sync_all: bool = False, with_folders: bool = False):
        from email_assistant.imap_client import IMAPClient
        from email_assistant.models import EmailMessage, EmailSettings
        from email_assistant.sync import folders_to_sync, pull_read_flags, save_message, sync_folder

        created = errors = 0
        days_back = 3650 if sync_all else account.sync_days_back
        limit = None if account.sync_no_limit else account.sync_limit
        # Minerva сама пише \Seen на сервер → сервер джерело правди і для «непрочитано»
        two_way = EmailSettings.get_for_user(account.user).mark_read_on_server

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
                        if dry_run:
                            if not EmailMessage.objects.filter(
                                    account=account, imap_uid=msg_data['uid'],
                                    imap_folder_name=folder).exists():
                                self.stdout.write(
                                    f'  [DRY] {folder_type} uid={msg_data["uid"]} | {msg_data["subject"][:50]}')
                                created += 1
                            continue

                        # CRM-хронологія і клієнт гілки — сигнал email_assistant.signals.link_email_to_crm
                        em = save_message(account, msg_data, folder, folder_type)
                        if em is None:
                            continue

                        if folder_type == EmailMessage.FOLDER_INBOX:
                            self._post_process_inbox(em, account, em.thread)
                            em.refresh_from_db(fields=['is_spam', 'folder'])
                            if not em.is_spam and not msg_data['is_read']:
                                self._notify_new_email(em, account)

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

                if not dry_run:
                    try:
                        pull_read_flags(client, account, folder, two_way=two_way)
                    except Exception as e:
                        logger.warning('pull_read_flags %s: %s', folder, e)

            if with_folders and not dry_run:
                for f in folders_to_sync(client.list_folders()):
                    try:
                        new = sync_folder(client, account, f['name'], limit=limit)
                        created += len(new or [])
                        pull_read_flags(client, account, f['name'], two_way=two_way)
                    except Exception as e:
                        errors += 1
                        logger.warning('sync folder %s: %s', f['name'], e)

        if not dry_run:
            # Листи без категорії (історія до появи категорій) — порціями при кожній синхронізації
            try:
                from email_assistant.categorize import categorize_pending
                categorize_pending(account, limit=1000)
            except Exception as e:
                logger.warning('categorize_pending %s: %s', account.pk, e)

        return created, errors

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
