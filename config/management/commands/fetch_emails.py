"""
config/management/commands/fetch_emails.py

Читає листи з IMAP (inbox + sent) і зберігає в CustomerTimeline.
Кожен користувач має власні IMAP налаштування (UserProfile).
Клієнт і напрям — email_assistant.crm_link (To/Cc, власні адреси, номер замовлення).
Дублікати відфільтровуються по IMAP UID + user і по Message-ID.
Запускається автоматично кожні 15 хв через cron_runner.sh.
"""
import email
import imaplib
import logging
from datetime import datetime, timedelta, timezone as dt_tz
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger('config')


def _decode_str(raw) -> str:
    """Декодуємо encoded-word заголовки (=?utf-8?...?=)."""
    if not raw:
        return ''
    parts = decode_header(raw)
    result = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            result.append(chunk.decode(enc or 'utf-8', errors='replace'))
        else:
            result.append(chunk)
    return ' '.join(result).strip()


def _get_body(msg) -> str:
    """Текст листа; для листів лише з HTML — текст, витягнутий з розмітки."""
    from email_assistant.imap_client import _get_body as _bodies
    text_body, _ = _bodies(msg)
    return text_body[:2000]


class Command(BaseCommand):
    help = 'Завантажити листи з IMAP і додати в хронологію клієнтів (per-user)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Показати знайдені листи без збереження',
        )
        parser.add_argument(
            '--days', type=int, default=None,
            help='Завантажити за N днів (default: 30)',
        )
        parser.add_argument(
            '--customer', metavar='EMAIL', default=None,
            help='Фільтрувати листи тільки від/до цього email (напр. ivan@example.com)',
        )
        parser.add_argument(
            '--user', metavar='USERNAME', default=None,
            help='Синхронізувати тільки для цього користувача',
        )
        parser.add_argument(
            '--list-folders', action='store_true',
            help='Показати список IMAP папок для кожного юзера і вийти (діагностика)',
        )

    def handle(self, *args, **options):
        from core.models import UserProfile

        if options.get('list_folders'):
            self._list_folders(options)
            return

        dry_run         = options['dry_run']
        lookback        = options['days'] or 30
        customer_filter = options['customer'].lower().strip() if options['customer'] else None
        username_filter = options['user']

        qs = UserProfile.objects.filter(imap_enabled=True).select_related('user')
        if username_filter:
            qs = qs.filter(user__username=username_filter)

        if not qs.exists():
            self.stdout.write('Немає користувачів з увімкненим IMAP — пропускаємо')
            return

        total_created = total_skipped = total_errors = 0

        for profile in qs:
            if not profile.imap_host or not profile.imap_user or not profile.imap_password:
                self.stderr.write(
                    f'[{profile.user.username}] IMAP host/user/password не заповнені — пропускаємо'
                )
                continue

            self.stdout.write(f'--- {profile.user.username} ({profile.imap_user}) ---')

            for folder, event_type in [
                ('INBOX',                               'email_in'),
                (profile.imap_sent_folder or '',        'email_out'),
            ]:
                if not folder:
                    continue
                c, sk, er = self._process_folder(
                    profile, folder, event_type, lookback, dry_run, customer_filter)
                total_created += c
                total_skipped += sk
                total_errors  += er

        self.stdout.write(
            f'fetch_emails: +{total_created} нових, {total_skipped} пропущено, {total_errors} помилок'
        )
        # Update last-fetch timestamp in NotificationSettings
        try:
            from config.models import NotificationSettings
            ns = NotificationSettings.objects.filter(pk=1).first()
            if ns:
                ns.imap_last_fetched = timezone.now()
                ns.save(update_fields=['imap_last_fetched'])
        except Exception:
            pass

    def _connect(self, profile) -> imaplib.IMAP4_SSL | imaplib.IMAP4:
        if profile.imap_use_ssl:
            conn = imaplib.IMAP4_SSL(profile.imap_host, profile.imap_port)
        else:
            conn = imaplib.IMAP4(profile.imap_host, profile.imap_port)
            conn.starttls()
        conn.login(profile.imap_user, profile.imap_password)
        return conn

    def _select_folder(self, conn, folder_name: str) -> bool:
        """
        Спробувати вибрати IMAP папку.
        Перевіряє кілька варіантів назви (з/без лапок, альтернативні sent-назви).
        Повертає True якщо успішно обрано.
        """
        candidates = [folder_name]

        # Додати варіант з/без лапок
        if folder_name.startswith('"') and folder_name.endswith('"'):
            candidates.append(folder_name[1:-1])
        else:
            candidates.append(f'"{folder_name}"')

        # Для sent-папок — спробувати поширені альтернативні назви
        if 'sent' in folder_name.lower():
            candidates += [
                'INBOX.Sent',
                '"INBOX.Sent"',
                'Sent',
                '"Sent"',
                'Sent Items',
                '"Sent Items"',
                '[Gmail]/Sent Mail',
                'Sent Messages',
            ]

        seen = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                status, _ = conn.select(candidate, readonly=True)
                if status == 'OK':
                    return True
            except Exception:
                continue
        return False

    def _list_folders(self, options):
        """Показати список IMAP папок для кожного юзера — для діагностики."""
        from core.models import UserProfile

        username_filter = options.get('user')
        qs = UserProfile.objects.filter(imap_enabled=True).select_related('user')
        if username_filter:
            qs = qs.filter(user__username=username_filter)

        if not qs.exists():
            self.stdout.write('Немає користувачів з увімкненим IMAP')
            return

        for profile in qs:
            if not profile.imap_host or not profile.imap_user or not profile.imap_password:
                self.stdout.write(f'[{profile.user.username}] IMAP не налаштований — пропускаємо')
                continue
            try:
                conn = self._connect(profile)
                _, folders = conn.list()
                self.stdout.write(f'\n=== {profile.user.username} ({profile.imap_user}) ===')
                for f in (folders or []):
                    line = f.decode() if isinstance(f, bytes) else str(f)
                    self.stdout.write(f'  {line}')
                conn.logout()
            except Exception as e:
                self.stdout.write(f'[{profile.user.username}] Помилка підключення: {e}')

    def _process_folder(self, profile, folder, event_type, lookback_days, dry_run, customer_filter):
        from crm.models import Customer, CustomerTimeline
        from email_assistant.crm_link import link_to_crm, own_addresses, resolve
        from email_assistant.imap_client import parse_addresses

        customer_qs = Customer.objects.exclude(email='')
        if customer_filter:
            customer_qs = customer_qs.filter(email__iexact=customer_filter)
        if not customer_qs.exists():
            return 0, 0, 0
        own = own_addresses()

        # UIDs вже збережених листів: скоуп по user + event_type (кожен юзер — свій mailbox)
        existing_uids = set(
            CustomerTimeline.objects
            .filter(event_type=event_type, related_email_id__isnull=False, user=profile.user)
            .values_list('related_email_id', flat=True)
        )

        since_date = (datetime.now() - timedelta(days=lookback_days)).strftime('%d-%b-%Y')

        created = skipped = errors = 0
        try:
            conn = self._connect(profile)
        except Exception as e:
            self.stderr.write(f'[{profile.user.username}] IMAP connect error ({folder}): {e}')
            return 0, 0, 1

        try:
            if not self._select_folder(conn, folder):
                self.stderr.write(
                    f'[{profile.user.username}] Cannot select folder: {folder} '
                    f'— пропускаємо (не критично для sent папки)'
                )
                # Не рахуємо як помилку — sent папка може називатись інакше
                return 0, 0, 0

            _, data = conn.uid('search', None, f'SINCE {since_date}')
            uids = data[0].split() if data[0] else []

            for uid_bytes in uids:
                uid = int(uid_bytes)
                if uid in existing_uids:
                    skipped += 1
                    continue

                try:
                    _, msg_data = conn.uid('fetch', uid_bytes, '(RFC822)')
                    raw = msg_data[0][1]
                    msg = email.message_from_bytes(raw)

                    subject = _decode_str(msg.get('Subject', ''))
                    _, from_email = parseaddr(msg.get('From', ''))
                    body = _get_body(msg)
                    # Клієнт і напрям — спільна логіка email_assistant.crm_link
                    # (копія власного листа у Вхідних через CC = відправлений, а не отриманий)
                    params = dict(
                        from_email=from_email,
                        to_emails=parse_addresses(msg.get('To', '')),
                        cc_emails=parse_addresses(msg.get('Cc', '')),
                        subject=subject,
                        body=body,
                        is_sent_folder=event_type == 'email_out',
                        own=own,
                    )
                    customer, direction, _ = resolve(**params)
                    if not customer or (customer_filter and customer.email.lower() != customer_filter):
                        skipped += 1
                        continue

                    # Дата листа
                    try:
                        date_str = msg.get('Date', '')
                        msg_date = parsedate_to_datetime(date_str)
                        if msg_date.tzinfo is None:
                            msg_date = msg_date.replace(tzinfo=dt_tz.utc)
                    except Exception:
                        msg_date = timezone.now()

                    if dry_run:
                        self.stdout.write(
                            f'[DRY] email_{direction} uid={uid} | '
                            f'{customer.name} | {subject[:60]}'
                        )
                        created += 1
                        continue

                    # Дублікат того ж листа (інша копія / sync_email) знаходиться за Message-ID
                    link_to_crm(
                        user=profile.user,
                        message_id=msg.get('Message-ID', ''),
                        sent_at=msg_date,
                        imap_uid=uid,
                        **params,
                    )
                    existing_uids.add(uid)
                    created += 1

                except Exception as e:
                    logger.error(f'[{profile.user.username}] fetch_emails uid={uid}: {e}')
                    errors += 1

        finally:
            try:
                conn.logout()
            except Exception:
                pass

        return created, skipped, errors
