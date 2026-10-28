"""
email_assistant/crm_link.py — прив'язка листів до CRM-клієнта і замовлення.

Одне місце для всіх шляхів появи листа (sync_email, синхронізація окремих папок,
імпорт .eml, відправка з Minerva, legacy fetch_emails):

- напрям визначається за відправником: лист з власної адреси (або з адреси
  власного домену) — 📤 відправлений, навіть якщо копія прийшла у Вхідні через CC;
- клієнт шукається серед співрозмовників (для відправлених — усі To/Cc без власних адрес);
- номер замовлення шукається в темі, потім у тексті листа — події групуються в CRM по замовленню;
- один лист (копія у Вхідних + у Надісланих) = одна подія, ключ — Message-ID.
"""
import logging
import re
from email.utils import parseaddr

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from email_assistant.imap_client import looks_like_html

logger = logging.getLogger('email_assistant')

# Домени публічних поштовиків — не вважаються «власним доменом» компанії
FREE_MAIL_DOMAINS = {
    'gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com', 'live.com', 'msn.com',
    'yahoo.com', 'icloud.com', 'me.com', 'aol.com', 'gmx.de', 'gmx.net', 'gmx.com',
    'web.de', 't-online.de', 'freenet.de', 'mail.de', 'online.de', 'ukr.net',
    'i.ua', 'meta.ua', 'proton.me', 'protonmail.com', 'yandex.ru', 'mail.ru',
}

# Кандидати в номери замовлень: «99823894», «SO-2026-0012» (мін. 5 цифр — не роки й не кількості)
_TOKEN_RE = re.compile(r'(?<![\w-])([A-Za-z0-9][\w/-]{3,63})(?![\w-])')
_MIN_ORDER_LEN = 4


def _addr(value) -> str:
    _, addr = parseaddr(str(value or ''))
    addr = addr.strip().lower()
    return addr if '@' in addr else ''


def own_addresses():
    """(адреси, домени) власних скриньок: email-акаунти, SMTP/IMAP профілів і глобальні налаштування."""
    raw = []
    from email_assistant.models import EmailAccount
    for row in EmailAccount.objects.values_list('email_address', 'imap_username', 'smtp_username'):
        raw.extend(row)
    try:
        from core.models import UserProfile
        for row in UserProfile.objects.values_list('imap_user', 'smtp_user', 'smtp_from'):
            raw.extend(row)
    except Exception:
        pass
    try:
        from config.models import NotificationSettings
        ns = NotificationSettings.objects.filter(pk=1).values(
            'email_host_user', 'email_from', 'imap_user').first()
        if ns:
            raw.extend(ns.values())
    except Exception:
        pass
    addrs = {a for a in (_addr(x) for x in raw) if a}
    domains = {a.split('@', 1)[1] for a in addrs} - FREE_MAIL_DOMAINS
    return addrs, domains


def is_own(address: str, own) -> bool:
    addrs, domains = own
    address = _addr(address)
    if not address:
        return False
    return address in addrs or address.split('@', 1)[1] in domains


def find_customer(addresses):
    from crm.models import Customer
    for a in addresses:
        customer = Customer.objects.filter(email__iexact=a).first()
        if customer:
            return customer
    return None


def customer_orders(customer) -> list:
    """Замовлення клієнта (за ключем CRM або email) — для пошуку номерів у листах."""
    from sales.models import SalesOrder
    cond = Q(customer_key=customer.external_key) if customer.external_key else Q(pk__in=[])
    if customer.email:
        cond |= Q(email__iexact=customer.email)
    return list(SalesOrder.objects.filter(cond).only('pk', 'order_number', 'source'))


def _first_mentioned(orders, text):
    """Замовлення, номер якого згадано в тексті найраніше."""
    best, best_pos = None, None
    for order in orders:
        num = (order.order_number or '').strip()
        if len(num) < _MIN_ORDER_LEN:
            continue
        m = re.search(r'(?<![\w-])' + re.escape(num) + r'(?![\w-])', text, re.IGNORECASE)
        if m and (best_pos is None or m.start() < best_pos):
            best, best_pos = order, m.start()
    return best


def find_order(subject, body, customer=None, orders=None, global_search=True):
    """
    Номер замовлення в темі (пріоритет) або тексті листа.
    Спершу серед замовлень клієнта, потім (global_search) — серед усіх замовлень.
    """
    from sales.models import SalesOrder
    texts = [t for t in (subject or '', (body or '')[:20000]) if t]
    if orders is None and customer is not None:
        orders = customer_orders(customer)
    for text in texts:
        hit = _first_mentioned(orders or [], text)
        if hit:
            return hit
    if not global_search:
        return None
    for text in texts:
        tokens = {t for t in _TOKEN_RE.findall(text) if sum(ch.isdigit() for ch in t) >= 5}
        if not tokens:
            continue
        found = list(SalesOrder.objects.filter(order_number__in=tokens)
                     .only('pk', 'order_number', 'source', 'customer_key', 'email'))
        hit = _first_mentioned(found, text)
        if hit:
            return hit
    # Трек-номер замовлення чи відправлення (листи перевізника: UPS 1Z…, DHL…)
    from email_assistant.links import find_order_by_tracking, tokens_in
    return find_order_by_tracking(tokens_in(texts))


def resolve(*, from_email, to_emails, cc_emails=(), subject='', body='',
            is_sent_folder=False, own=None):
    """
    Повертає (customer, direction, order) або (None, direction, None).
    direction: 'out' — лист від нас клієнту, 'in' — від клієнта.
    """
    own = own or own_addresses()
    outgoing = is_sent_folder or is_own(from_email, own)
    candidates = list(to_emails or []) + list(cc_emails or []) if outgoing else [from_email]
    counterparts = []
    for a in candidates:
        a = _addr(a)
        if a and a not in counterparts and not is_own(a, own):
            counterparts.append(a)
    direction = 'out' if outgoing else 'in'
    if not counterparts:  # внутрішнє листування — не в CRM
        return None, direction, None

    customer = find_customer(counterparts)
    order = find_order(subject, body, customer)
    if customer is None and order is not None:
        customer = order.crm_customer
    return customer, direction, order


def link_to_crm(*, user, subject, body, from_email, to_emails, cc_emails=(),
                message_id='', sent_at=None, is_sent_folder=False, imap_uid=None,
                email_message=None, own=None):
    """Створити/оновити подію хронології клієнта для листа. Повертає CustomerTimeline або None."""
    from crm.models import CustomerTimeline

    customer, direction, order = resolve(
        from_email=from_email, to_emails=to_emails, cc_emails=cc_emails,
        subject=subject, body=body, is_sent_folder=is_sent_folder, own=own)
    if customer is None:
        return None

    event_type = 'email_out' if direction == 'out' else 'email_in'
    message_id = (message_id or '').strip()[:500]

    title = (subject or '(без теми)')[:300]
    event = None
    if email_message is not None:
        event = CustomerTimeline.objects.filter(customer=customer, email_message=email_message).first()
    if event is None and message_id:
        event = CustomerTimeline.objects.filter(customer=customer, message_id=message_id).first()
    if event is None and imap_uid:
        # Події, створені до появи Message-ID у хронології: той самий UID і тема
        event = CustomerTimeline.objects.filter(
            customer=customer, email_message__isnull=True, message_id='',
            related_email_id=imap_uid, title=title,
            event_type__in=('email_in', 'email_out'),
        ).first()

    if event is not None:
        changed = []
        if email_message is not None and event.email_message_id is None:
            event.email_message = email_message
            changed.append('email_message')
        if message_id and not event.message_id:
            event.message_id = message_id
            changed.append('message_id')
        if order is not None and not event.related_order_id:
            event.related_order_id = order.pk
            changed.append('related_order_id')
        if event.event_type in ('email_in', 'email_out') and event.event_type != event_type:
            event.event_type = event_type
            changed.append('event_type')
        if body and looks_like_html(event.body):  # сирий HTML зі старих HTML-only листів
            event.body = body[:2000]
            changed.append('body')
        if changed:
            event.save(update_fields=changed)
        return event

    return CustomerTimeline.objects.create(
        customer=customer,
        user=user,
        event_type=event_type,
        title=title,
        body=(body or '')[:2000],
        related_email_id=imap_uid or None,
        related_order_id=order.pk if order is not None else None,
        email_message=email_message,
        message_id=message_id,
        created_at=sent_at or timezone.now(),
    )


def set_thread_customer(thread, customer, user=None) -> int:
    """Клієнт гілки вручну («🔍 Чому лист тут»): гілка → клієнт, листи гілки — у його хронології (подія листа
    іншого клієнта переноситься, а якщо в нього вже є — зайва видаляється; бракує — створюється, як для нового
    листа). customer=None — лише відв'язати гілку (події хронології лишаються). → листів у хронології клієнта."""
    from crm.models import CustomerTimeline
    from email_assistant.models import EmailMessage, EmailThread
    EmailThread.objects.filter(pk=thread.pk).update(crm_customer=customer)
    if customer is None:
        return 0
    own, n = own_addresses(), 0
    for em in (EmailMessage.objects.filter(thread=thread, is_deleted=False, is_spam=False)
               .exclude(folder__in=[EmailMessage.FOLDER_SPAM, EmailMessage.FOLDER_DRAFT])):
        events = CustomerTimeline.objects.filter(email_message=em)
        if events.filter(customer=customer).exists():
            events.exclude(customer=customer).delete()
        elif events.exists():
            first = events.first()
            events.exclude(pk=first.pk).delete()
            CustomerTimeline.objects.filter(pk=first.pk).update(customer=customer)
        else:
            outgoing = em.folder == EmailMessage.FOLDER_SENT or is_own(em.from_email, own)
            CustomerTimeline.objects.create(
                customer=customer, user=user, event_type='email_out' if outgoing else 'email_in',
                title=(em.subject or '(без теми)')[:300], body=(em.body_text or '')[:2000],
                related_email_id=em.imap_uid or None, email_message=em,
                message_id=(em.message_id or '').strip()[:500], created_at=em.sent_at or timezone.now())
        n += 1
    return n


def link_message_to_crm(em, own=None):
    """Прив'язати збережений EmailMessage до клієнта (і його гілку листування теж)."""
    from email_assistant.models import EmailMessage, EmailThread

    if em.is_spam or em.folder in (EmailMessage.FOLDER_SPAM, EmailMessage.FOLDER_DRAFT):
        return None
    try:
        # savepoint: помилка БД тут не ламає зовнішню транзакцію (relink_email_crm, sync)
        with transaction.atomic():
            event = link_to_crm(
                user=em.account.user,
                subject=em.subject,
                body=em.body_text,
                from_email=em.from_email,
                to_emails=em.to_emails,
                cc_emails=em.cc_emails,
                message_id=em.message_id,
                sent_at=em.sent_at,
                is_sent_folder=em.folder == EmailMessage.FOLDER_SENT,
                imap_uid=em.imap_uid,
                email_message=em,
                own=own,
            )
    except Exception as e:
        logger.error('CRM link email pk=%s: %s', em.pk, e)
        return None
    if event is not None and em.thread_id:
        EmailThread.objects.filter(pk=em.thread_id, crm_customer__isnull=True).update(
            crm_customer=event.customer)
    return event
