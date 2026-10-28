"""
email_assistant/spam.py — «🚫 Спам» раз і назавжди.

Позначили лист спамом → Minerva запам'ятовує відправника (правило пошти «Позначити спамом»):
  - для поштовиків (gmail.com, gmx.de…), власного домену й доменів клієнтів / постачальників — лише адресу,
    інакше весь @домен (спамери міняють адреси, але не домен);
  - інші листи цього відправника з Вхідних і папок теж ідуть у Спам (у Minerva і на сервері);
  - нові листи від нього одразу потрапляють у Спам ще ДО збереження (`before_save`), тож запрошення,
    терміни, автовідповідь і сповіщення їх уже не бачать;
  - з календаря прибирається все, що прийшло від нього (calendar_app/blocks.py, причина «спам»).
Старі автоправила на весь домен поштовика («Спам: @gmail.com») надто широкі — вони не діють (`too_broad`).
"""
import logging

from email_assistant.crm_link import FREE_MAIL_DOMAINS, _addr, is_own, own_addresses

logger = logging.getLogger('email_assistant')

NAME_PREFIX = 'Спам: '


def sender_of(em) -> str:
    """Відправник листа (адреса)."""
    return _addr(em.from_email)


def _known_domain(domain: str) -> bool:
    """Домен клієнта CRM або постачальника — блокувати лише адресу, не весь домен."""
    from crm.models import Customer
    from inventory.models import Supplier
    return (Customer.objects.filter(email__iendswith='@' + domain).exists()
            or Supplier.objects.filter(email__iendswith='@' + domain).exists())


def sender_key(address: str, own=None) -> str:
    """Ключ правила: адреса або @домен (див. опис модуля). '' — свою адресу спамом не позначаємо."""
    address = _addr(address)
    if not address:
        return ''
    own = own or own_addresses()
    if is_own(address, own):
        return ''
    domain = address.split('@', 1)[1]
    if domain in FREE_MAIL_DOMAINS or _known_domain(domain):
        return address
    return '@' + domain


def matches(address: str, key: str) -> bool:
    address = _addr(address)
    if not address or not key:
        return False
    key = key.lower()
    if key.startswith('@'):
        return address.endswith(key) or address.split('@', 1)[1].endswith('.' + key[1:])
    return address == key


def too_broad(rule) -> bool:
    """Автоправило «Спам: @gmail.com» (старе: на весь домен поштовика) — не діє, щоб не ховати чужі листи."""
    value = (rule.condition_value or '').strip().lower()
    return (rule.name or '').startswith(NAME_PREFIX) and value.startswith('@') and value[1:] in FREE_MAIL_DOMAINS


def spam_keys(account) -> list:
    """Активні правила «Позначити спамом» за відправником (адреса / @домен / частина адреси)."""
    from email_assistant.models import EmailRule
    keys = []
    for rule in EmailRule.objects.filter(account=account, is_active=True, action=EmailRule.ACTION_MARK_SPAM,
                                         condition_field=EmailRule.FIELD_FROM_EMAIL):
        if too_broad(rule):
            continue
        keys.append(rule)
    return keys


def is_spam_sender(account, address: str, rules=None) -> bool:
    """Чи підходить відправник до правила «спам» скриньки."""
    from email_assistant.models import EmailMessage
    if not _addr(address):
        return False
    probe = EmailMessage(from_email=_addr(address), from_name='', subject='', body_text='', to_emails=[])
    return any(rule.matches(probe) for rule in (spam_keys(account) if rules is None else rules))


def before_save(em) -> bool:
    """Новий лист від відомого спамера → одразу в Спам (до збереження, щоб сигнали його вже не обробляли)."""
    from email_assistant.models import EmailMessage
    if em.is_spam or em.folder in (EmailMessage.FOLDER_SENT, EmailMessage.FOLDER_DRAFT, EmailMessage.FOLDER_SPAM):
        return False
    try:
        if not is_spam_sender(em.account, em.from_email):
            return False
    except Exception as e:  # noqa: BLE001 — правила не повинні зламати синхронізацію
        logger.warning('spam check %s: %s', em.from_email, e)
        return False
    em.is_spam, em.folder = True, EmailMessage.FOLDER_SPAM
    em._move_to_spam = True
    return True


def after_save(em):
    """Лист став спамом під час синхронізації — перенести і на сервері (у фоні)."""
    if getattr(em, '_move_to_spam', False) and em.imap_uid:
        from email_assistant.sync import default_imap_folder
        move_on_server(em.account, [(default_imap_folder(em.account, em), em.imap_uid)])


def move_on_server(account, items):
    """[(папка, uid)] → папка «Спам» на сервері, одне підключення, у фоні."""
    items = [(f, u) for f, u in items if f and u]
    if not items:
        return

    def _run():
        from django.db import close_old_connections
        from email_assistant.imap_client import IMAPClient
        close_old_connections()
        try:
            with IMAPClient(account) as imap:
                for folder, uid in items:
                    imap.move_to_spam(folder, uid)
        except Exception as ex:  # noqa: BLE001
            logger.warning('spam move %s: %s', items[:3], ex)

    from email_assistant.sync import _start_background
    _start_background(_run)


def remember(account, messages) -> list:
    """Запам'ятати відправників листів як спам (правило пошти на кожного). Повертає ключі нових і наявних правил."""
    from email_assistant.models import EmailRule
    own = own_addresses()
    keys = []
    for em in messages:
        key = sender_key(em.from_email, own)
        if key and key not in keys:
            keys.append(key)
    for key in keys:
        EmailRule.objects.get_or_create(
            account=account, condition_field=EmailRule.FIELD_FROM_EMAIL, condition_op=EmailRule.OP_CONTAINS,
            condition_value=key, action=EmailRule.ACTION_MARK_SPAM,
            defaults={'name': NAME_PREFIX + key})
        EmailRule.objects.filter(account=account, condition_field=EmailRule.FIELD_FROM_EMAIL,
                                 condition_value=key, action=EmailRule.ACTION_MARK_SPAM).update(is_active=True)
    return keys


def forget(account, messages) -> int:
    """«Не спам»: прибрати автоправила для відправників цих листів."""
    from email_assistant.models import EmailRule
    own = own_addresses()
    keys = set()
    for em in messages:
        address = _addr(em.from_email)
        if address:
            keys |= {address, '@' + address.split('@', 1)[1], sender_key(address, own)}
    deleted, _ = (EmailRule.objects.filter(account=account, action=EmailRule.ACTION_MARK_SPAM,
                                           name__startswith=NAME_PREFIX, condition_value__in=keys - {''})
                  .delete())
    return deleted


def sweep(account, keys) -> int:
    """Інші листи цих відправників (не надіслані, не в Спамі) → Спам у Minerva і на сервері."""
    from email_assistant.models import EmailMessage
    from email_assistant.sync import default_imap_folder
    if not keys:
        return 0
    from django.db.models import Q
    cond = Q()
    for key in keys:
        cond |= Q(from_email__icontains=key)
    candidates = (EmailMessage.objects.filter(account=account, is_spam=False, is_deleted=False)
                  .exclude(folder__in=[EmailMessage.FOLDER_SENT, EmailMessage.FOLDER_DRAFT]).filter(cond))
    moved = [m for m in candidates if any(matches(m.from_email, k) for k in keys)]
    if not moved:
        return 0
    move_on_server(account, [(default_imap_folder(account, m), m.imap_uid) for m in moved if m.imap_uid])
    pks = [m.pk for m in moved]
    EmailMessage.objects.filter(pk__in=pks).update(is_spam=True, folder=EmailMessage.FOLDER_SPAM)
    from email_assistant.knowledge_hook import touch
    touch(pks)
    return len(pks)


def mark(account, messages) -> dict:
    """Позначити листи спамом: самі листи, правила на відправників, решту їхніх листів, календар."""
    from email_assistant.models import EmailMessage
    from email_assistant.sync import default_imap_folder
    messages = list(messages)
    new = [m for m in messages if not m.is_spam]
    move_on_server(account, [(default_imap_folder(account, m), m.imap_uid) for m in new if m.imap_uid])
    EmailMessage.objects.filter(pk__in=[m.pk for m in messages]).update(is_spam=True,
                                                                        folder=EmailMessage.FOLDER_SPAM)
    keys = remember(account, messages)
    more = sweep(account, keys)
    removed = 0
    try:
        from calendar_app import blocks
        for key in keys:
            removed += blocks.block(account.user, key, blocks.SPAM)['removed']
    except Exception as e:  # noqa: BLE001 — календар не повинен зламати позначку спаму
        logger.warning('spam → calendar %s: %s', keys, e)
    from email_assistant.knowledge_hook import touch
    touch([m.pk for m in messages])
    return {'keys': keys, 'more': more, 'calendar': removed}


def unmark(account, messages) -> int:
    """«Не спам»: назад у Вхідні, автоправила й блок календаря для відправника знімаються."""
    from email_assistant.models import EmailMessage
    messages = list(messages)
    EmailMessage.objects.filter(pk__in=[m.pk for m in messages], is_spam=True).update(
        is_spam=False, folder=EmailMessage.FOLDER_INBOX)
    forget(account, messages)
    try:
        from calendar_app import blocks
        for em in messages:
            blocks.unblock_spam(account.user, em.from_email)
    except Exception as e:  # noqa: BLE001
        logger.warning('unspam → calendar: %s', e)
    from email_assistant.knowledge_hook import touch
    touch([m.pk for m in messages])
    return len(messages)
