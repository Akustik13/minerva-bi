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

        total_new = total_err = 0
        for account in qs:
            self.stdout.write(f'--- {account.user.username} / {account.email_address} ---')
            try:
                new, err = self._sync_account(account, options['dry_run'])
                total_new += new
                total_err += err
                self.stdout.write(f'  +{new} нових, {err} помилок')
            except Exception as e:
                total_err += 1
                logger.error('sync_email account %s: %s', account.pk, e)
                self.stdout.write(f'  ERROR: {e}')

        self.stdout.write(f'\nРезультат: +{total_new} нових листів, {total_err} помилок')

    def _sync_account(self, account, dry_run: bool):
        from email_assistant.models import EmailMessage, EmailThread
        from email_assistant.imap_client import IMAPClient

        created = errors = 0

        with IMAPClient(account) as client:
            for folder, folder_type in [
                (account.imap_folder_inbox, EmailMessage.FOLDER_INBOX),
                (account.imap_folder_sent,  EmailMessage.FOLDER_SENT),
            ]:
                if not folder:
                    continue

                since_uid = account.last_seen_uid if folder_type == EmailMessage.FOLDER_INBOX else 0
                messages  = client.fetch_messages(
                    folder=folder,
                    days_back=account.sync_days_back,
                    since_uid=since_uid,
                )

                for msg_data in messages:
                    try:
                        if EmailMessage.objects.filter(
                                account=account,
                                imap_uid=msg_data['uid'],
                                folder=folder_type).exists():
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
