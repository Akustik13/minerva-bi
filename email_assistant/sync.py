"""
email_assistant/sync.py — спільна логіка синхронізації пошти.

- save_message: один шлях збереження листа (гілка, вкладення, лічильники гілки);
- sync_folder: нові листи будь-якої папки сервера за UID — без вікна «останні N днів»;
- pull_read_flags: прочитане на сервері (телефон, веб-пошта) → прочитане в Minerva;
- set_read: прочитане в Minerva → прапорець \\Seen на сервері (у ту папку, де лежить лист).
"""
import logging
import threading
from collections import defaultdict

from django.db.models import Max, Q

logger = logging.getLogger('email_assistant')

# Службові папки — їх показує сам інтерфейс (Вхідні, Надіслані, Спам, Кошик) або вони не потрібні
SKIP_FOLDER_NAMES = {
    'inbox', 'gesendete objekte', 'sent', 'sent items', 'sent messages', '[gmail]/sent mail',
    'trash', 'inbox.trash', '[gmail]/trash', 'deleted items', 'deleted messages', 'papierkorb',
    'spam', 'junk', '[gmail]/spam', 'inbox.spam', 'junk email',
    'drafts', 'inbox.drafts', '[gmail]/drafts', 'entwürfe', 'entw&apw-rfe',
}
SKIP_SPECIAL_USE = {'\\Sent', '\\Trash', '\\Junk', '\\Drafts', '\\All'}


def default_imap_folder(account, em) -> str:
    if em.imap_folder_name:
        return em.imap_folder_name
    return account.imap_folder_sent if em.folder == 'sent' else account.imap_folder_inbox


def folder_type_for(account, imap_folder: str) -> str:
    from email_assistant.models import EmailMessage
    sent = (account.imap_folder_sent or '').lower()
    return EmailMessage.FOLDER_SENT if sent and imap_folder.lower() == sent else EmailMessage.FOLDER_INBOX


def folder_messages(account, imap_folder: str):
    """Листи папки сервера в БД (старі листи Вхідних мають порожню imap_folder_name)."""
    from email_assistant.models import EmailMessage
    qs = EmailMessage.objects.filter(account=account)
    if imap_folder.lower() == (account.imap_folder_inbox or 'INBOX').lower():
        return qs.filter(Q(imap_folder_name='') | Q(imap_folder_name__iexact=imap_folder), folder='inbox')
    return qs.filter(imap_folder_name=imap_folder)


def folders_to_sync(folders) -> list:
    """Папки сервера, які синхронізуються окремо від Вхідних/Надісланих."""
    return [f for f in folders
            if f.get('selectable')
            and f.get('special') not in SKIP_SPECIAL_USE
            and f['name'].lower() not in SKIP_FOLDER_NAMES]


def get_or_create_thread(account, msg_data):
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


def save_message(account, msg_data, imap_folder, folder_type=None, persist_files=False):
    """Зберегти лист з IMAP (якщо його ще немає). Повертає EmailMessage або None."""
    from email_assistant.imap_client import persist_attachments
    from email_assistant.models import EmailMessage

    if EmailMessage.objects.filter(account=account, imap_uid=msg_data['uid'],
                                   imap_folder_name=imap_folder).exists():
        return None

    attachments = msg_data.get('attachments', [])
    if persist_files:
        attachments = persist_attachments(account.pk, msg_data['uid'], imap_folder, attachments)
    else:
        for att in attachments:
            att.pop('_data', None)

    thread = get_or_create_thread(account, msg_data)
    em = EmailMessage(
        account=account,
        thread=thread,
        imap_uid=msg_data['uid'],
        imap_folder_name=imap_folder,
        message_id=msg_data['message_id'],
        in_reply_to=msg_data['in_reply_to'],
        folder=folder_type or folder_type_for(account, imap_folder),
        subject=msg_data['subject'],
        from_email=msg_data['from_email'],
        from_name=msg_data['from_name'],
        to_emails=msg_data['to_emails'],
        cc_emails=msg_data['cc_emails'],
        body_text=msg_data['body_text'],
        body_html=msg_data['body_html'],
        attachments=attachments,
        is_read=msg_data['is_read'],
        sent_at=msg_data['sent_at'],
    )
    em._list_unsubscribe = msg_data.get('list_unsubscribe', False)  # підказка для категорії (сигнал)
    em.save()
    thread.message_count = thread.messages.count()
    if not thread.last_message_at or (msg_data['sent_at'] and msg_data['sent_at'] > thread.last_message_at):
        thread.last_message_at = msg_data['sent_at']
    if not msg_data['is_read']:
        thread.has_unread = True
    thread.save(update_fields=['message_count', 'last_message_at', 'has_unread'])
    return em


def sync_folder(client, account, imap_folder, *, full=False, limit=None, persist_files=True):
    """
    Завантажити листи папки сервера.
    full=False — лише новіші за останній збережений UID (перший раз — останні `limit` листів);
    full=True  — усі листи папки, яких ще немає в БД (в межах `limit`).
    Повертає список нових EmailMessage або None, якщо папку не вдалось відкрити.
    """
    stored = folder_messages(account, imap_folder).exclude(imap_uid=0)
    after = 0 if full else (stored.aggregate(m=Max('imap_uid'))['m'] or 0)
    uids = client.search_uids_after(imap_folder, after)
    if uids is None:
        return None
    if full:
        have = set(stored.values_list('imap_uid', flat=True))
        uids = [u for u in uids if int(u) not in have]
    if limit:
        uids = uids[-limit:]

    created = []
    for msg_data in client.fetch_by_uids_iter(uids):
        try:
            em = save_message(account, msg_data, imap_folder, persist_files=persist_files)
        except Exception as e:
            logger.error('sync_folder %s uid=%s: %s', imap_folder, msg_data.get('uid'), e)
            continue
        if em:
            created.append(em)
    return created


def _refresh_threads(thread_ids):
    from email_assistant.models import EmailThread
    for thread in EmailThread.objects.filter(pk__in={t for t in thread_ids if t}):
        has_unread = thread.messages.filter(is_read=False, is_deleted=False).exists()
        if thread.has_unread != has_unread:
            thread.has_unread = has_unread
            thread.save(update_fields=['has_unread'])


def pull_read_flags(client, account, imap_folder, two_way=True) -> int:
    """
    Прочитане на сервері → прочитане в Minerva.
    two_way — ще й «непрочитане на сервері» → непрочитане (коли Minerva сама пише \\Seen на сервер,
    сервер — джерело правди; інакше локальні позначки Minerva не перезаписуються).
    """
    unseen = client.search_unseen(imap_folder)
    if unseen is None:
        return 0
    stored = folder_messages(account, imap_folder).exclude(imap_uid=0).filter(is_deleted=False)

    to_read = stored.filter(is_read=False).exclude(imap_uid__in=unseen)
    to_unread = stored.filter(is_read=True, imap_uid__in=unseen) if two_way and unseen else stored.none()
    thread_ids = set(to_read.values_list('thread_id', flat=True)) | set(to_unread.values_list('thread_id', flat=True))
    changed = to_read.update(is_read=True) + to_unread.update(is_read=False)
    if changed:
        _refresh_threads(thread_ids)
    return changed


def _start_background(fn):
    threading.Thread(target=fn, daemon=True).start()


def push_seen(account, uids_by_folder, seen: bool):
    """Записати \\Seen на сервер у фоні (одне підключення на всі папки)."""
    uids_by_folder = {f: u for f, u in uids_by_folder.items() if f and u}
    if not uids_by_folder:
        return

    def _run():
        from email_assistant.imap_client import IMAPClient
        try:
            with IMAPClient(account) as imap:
                for folder, uids in uids_by_folder.items():
                    imap.set_seen(folder, uids, seen)
        except Exception as e:
            logger.warning('push_seen %s: %s', list(uids_by_folder), e)

    _start_background(_run)


def set_read(account, messages, read: bool = True) -> int:
    """
    Позначити листи прочитаними/непрочитаними в Minerva і, якщо увімкнено
    «Позначати прочитані на IMAP-сервері», — на сервері (у їхній папці).
    """
    from email_assistant.models import EmailMessage, EmailSettings

    changed = [m for m in messages if m.is_read != read]
    if not changed:
        return 0
    EmailMessage.objects.filter(pk__in=[m.pk for m in changed]).update(is_read=read)
    for m in changed:
        m.is_read = read
    _refresh_threads({m.thread_id for m in changed})

    if EmailSettings.get_for_user(account.user).mark_read_on_server:
        by_folder = defaultdict(list)
        for m in changed:
            if m.imap_uid:
                by_folder[default_imap_folder(account, m)].append(m.imap_uid)
        push_seen(account, by_folder, read)
    return len(changed)
