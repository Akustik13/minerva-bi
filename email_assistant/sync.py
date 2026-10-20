"""
email_assistant/sync.py — спільна логіка синхронізації пошти.

- save_message: один шлях збереження листа (гілка, вкладення, лічильники гілки);
- sync_folder: нові листи будь-якої папки сервера за UID — без вікна «останні N днів»;
- pull_read_flags: прочитане на сервері (телефон, веб-пошта) → прочитане в Minerva;
- set_read: прочитане в Minerva → прапорець \\Seen на сервері (у ту папку, де лежить лист).
"""
import logging
import re
import threading
from collections import defaultdict
from datetime import timedelta

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


# Префікси відповідей/пересилань різними мовами: Re:, AW:, Aw:, WG:, Fwd:, FW:, SV:, VS:, TR:, Antw:, Re[2]:
_REPLY_PREFIX_RE = re.compile(r'^\s*(re|aw|antw|wg|fw|fwd|sv|vs|tr)\s*(\[\d+\])?\s*:\s*', re.IGNORECASE)
SUBJECT_THREAD_DAYS = 180


def normalize_subject(subject: str) -> str:
    """Тема без «Re: AW: Aw: WG: …» і зайвих пробілів, у нижньому регістрі — ключ гілки за темою."""
    s = subject or ''
    while True:
        stripped = _REPLY_PREFIX_RE.sub('', s, count=1)
        if stripped == s:
            break
        s = stripped
    return ' '.join(s.split()).lower()


def _addresses(msg_data) -> set:
    return {a.lower() for a in [msg_data.get('from_email')] + list(msg_data.get('to_emails') or [])
            + list(msg_data.get('cc_emails') or []) if a}


def get_or_create_thread(account, msg_data):
    """
    Гілка листа:
    1) ланцюжок Message-ID — лист, на який відповідають (In-Reply-To) або будь-який із References,
       уже є в Minerva → його гілка; так само інша копія цього ж листа (Вхідні/Надіслані);
    2) запасний варіант — та сама тема без Re:/AW:/WG: і спільний зовнішній співрозмовник
       (однакові «Anfrage» від різних людей не зливаються);
    3) інакше — нова гілка.
    """
    from email_assistant.crm_link import is_own, own_addresses
    from email_assistant.models import EmailMessage, EmailThread

    refs = [r for r in [msg_data.get('in_reply_to')] + list(msg_data.get('references') or []) if r]
    mid = msg_data.get('message_id') or ''

    ids = [r[:500] for r in refs] + ([mid[:500]] if mid else [])
    if ids:
        known = (EmailMessage.objects.filter(account=account, message_id__in=ids[-60:])
                 .exclude(thread=None).order_by('-sent_at').values_list('thread_id', flat=True).first())
        if known:
            return EmailThread.objects.get(pk=known)
        thread = EmailThread.objects.filter(account=account, thread_id__in=ids[-60:]).first()
        if thread:
            return thread

    core = normalize_subject(msg_data.get('subject'))
    own = own_addresses()
    external = {a for a in _addresses(msg_data) if not is_own(a, own)}
    if len(core) >= 3 and external:
        since = (msg_data.get('sent_at') or timezone_now()) - timedelta(days=SUBJECT_THREAD_DAYS)
        candidates = (EmailThread.objects.filter(account=account, subject__icontains=core[:120])
                      .filter(last_message_at__gte=since).order_by('-last_message_at')[:30])
        for t in candidates:
            if normalize_subject(t.subject) == core and external & {p.lower() for p in (t.participants or [])}:
                return t

    return EmailThread.objects.create(
        account=account,
        thread_id=(refs[0] if refs else mid or msg_data['subject'] or '')[:500],
        subject=msg_data['subject'][:500],
        participants=sorted(_addresses(msg_data))[:50],
    )


def timezone_now():
    from django.utils import timezone
    return timezone.now()


def save_message(account, msg_data, imap_folder, folder_type=None, persist_files=False):
    """Зберегти лист з IMAP (якщо його ще немає). Повертає EmailMessage або None."""
    from email_assistant.imap_client import persist_attachments
    from email_assistant.models import EmailMessage

    if EmailMessage.objects.filter(account=account, imap_uid=msg_data['uid'],
                                   imap_folder_name=imap_folder).exists():
        return None

    # Лист, надісланий із Minerva, уже збережений локально (без UID) — це його копія з «Надісланих»:
    # дописуємо UID/папку замість дубліката
    if msg_data.get('message_id'):
        local = EmailMessage.objects.filter(account=account, imap_uid=0,
                                            message_id=msg_data['message_id']).first()
        if local is not None:
            local.imap_uid, local.imap_folder_name = msg_data['uid'], imap_folder
            fields = ['imap_uid', 'imap_folder_name']
            if not local.body_html and msg_data.get('body_html'):
                local.body_html = msg_data['body_html']
                fields.append('body_html')
            local.save(update_fields=fields)
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
    # учасники гілки (для запасного зв'язування за темою)
    participants = sorted({p.lower() for p in (thread.participants or [])} | _addresses(msg_data))[:50]
    thread.participants = participants
    thread.save(update_fields=['message_count', 'last_message_at', 'has_unread', 'participants'])
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
