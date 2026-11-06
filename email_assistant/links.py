"""
email_assistant/links.py — зв'язки листа з даними Minerva.

Шукає в темі й тексті листа номери замовлень і трек-номери (UPS 1Z…, DHL, FedEx — будь-які,
що збережені в замовленні чи відправленні) і повертає замовлення з клієнтом і відправленнями.
Так лист від UPS «REF: 102037400 … 1Z0R6B300426098447» показує замовлення digikey:102037400,
клієнта AEVEX і статус відправлення, навіть якщо його переслав колега.

Номер буває лише у вкладенні (ABD митниці «Referenznummer/UCR 102497269», інвойс перевізника): тоді —
текст PDF / DOCX / TXT / CSV листа (`attachment_tokens`, `order_in_attachments`).
"""
import contextlib
import contextvars
import copy
import logging

from django.urls import reverse

from email_assistant.crm_link import _TOKEN_RE

logger = logging.getLogger('email_assistant')

MAX_ORDERS = 6
ATTACHMENT_FILES = 4          # вкладень одного листа
ATTACHMENT_CHARS = 30000      # тексту з одного вкладення
ATTACHMENT_TOKENS = 300       # номерів з одного вкладення (кеш у attachments[i]['tokens'])
ATTACHMENT_MIN_DIGITS = 6     # у вкладеннях — лише довші номери: індекси, телефони, коди товарів (5 цифр) не плутати
PREVIEW_FILES = 6             # прев'ю гілки читає за раз не більше стількох ще не прочитаних вкладень
PREVIEW_FETCH = 2             # …і бере з IMAP файли не більше ніж стількох листів (синхронізація зберегла не все)

# Файлу документа немає на диску (лист збережено без вкладень) — брати з IMAP лише там, де можна чекати:
# прев'ю листа, relink_email_crm, «📥 Зберегти вкладення за 30 днів». Сигнал нового листа (синхронізація) — ні.
_FETCH = contextvars.ContextVar('links_fetch', default=None)


@contextlib.contextmanager
def allow_fetch(limit=None):
    """Дозволити attachment_tokens брати файли з IMAP: limit — скільки листів (None — без обмеження)."""
    token = _FETCH.set([limit])
    try:
        yield
    finally:
        _FETCH.reset(token)


def _take_fetch() -> bool:
    box = _FETCH.get()
    if box is None:
        return False
    if box[0] is None:
        return True
    if box[0] <= 0:
        return False
    box[0] -= 1
    return True


TOKENS_VERSION = 2   # tokens_v: кеш, де невдале читання не записується як «номерів немає»
READ_TRIES = 3       # стільки разів пробувати прочитати файл, що не читається (пошкоджений PDF), потім — ні


def _unread(att) -> bool:
    """Номери вкладення ще не прочитано. Порожній кеш без tokens_v — теж: раніше так запам'ятовувалось і невдале
    читання (PDF ще не збережено; у контейнері синхронізації немає бібліотеки PDF) — номер не знаходився ніколи."""
    tokens = att.get('tokens')
    if tokens:
        return False
    if tokens is None:
        return int(att.get('tokens_fail') or 0) < READ_TRIES
    return att.get('tokens_v') != TOKENS_VERSION


def tokens_in(texts) -> set:
    """Кандидати в номери: слова з літер/цифр, де щонайменше 5 цифр (+ варіант у верхньому регістрі)."""
    tokens = set()
    for text in texts:
        for tok in _TOKEN_RE.findall(text or ''):
            if sum(ch.isdigit() for ch in tok) >= 5:
                tokens.add(tok)
                tokens.add(tok.upper())
    return tokens


def orders_for_tokens(tokens):
    """{order_pk: (SalesOrder, причина)} — за номером замовлення, трек-номером замовлення або відправлення."""
    from sales.models import SalesOrder
    from shipping.models import Shipment

    found = {}
    if not tokens:
        return found
    for order in SalesOrder.objects.filter(order_number__in=tokens)[:MAX_ORDERS]:
        found.setdefault(order.pk, (order, 'номер замовлення'))
    for order in SalesOrder.objects.filter(tracking_number__in=tokens).exclude(tracking_number='')[:MAX_ORDERS]:
        found.setdefault(order.pk, (order, 'трек-номер'))
    for shipment in (Shipment.objects.filter(tracking_number__in=tokens).exclude(tracking_number='')
                     .exclude(order=None).select_related('order')[:MAX_ORDERS]):
        found.setdefault(shipment.order_id, (shipment.order, 'трек-номер'))
    return found


def attachment_tokens(msg, budget=None) -> dict:
    """{номер: назва вкладення} — кандидати з тексту PDF / DOCX / TXT / CSV листа (≥ ATTACHMENT_MIN_DIGITS цифр).
    Кожне вкладення читається раз: знайдене пишеться в attachments[i]['tokens'] (UPDATE без сигналу).
    Файлу на диску немає — кеш не пишеться (прочитається, коли файл з'явиться); у allow_fetch — файли з IMAP.
    budget — [скільки ще вкладень можна прочитати] на весь запит (прев'ю гілки не читає десятки PDF)."""
    from email_assistant.models import EmailMessage
    from email_assistant.reply_brief import ATTACHMENT_TYPES, _attachment_path, read_attachment_text

    raw = list(getattr(msg, 'attachments', None) or [])
    if not raw:
        return {}
    raw = copy.deepcopy(raw)
    docs = []
    for i, att in enumerate(raw):
        if not isinstance(att, dict) or att.get('is_inline'):
            continue
        name = att.get('name') or ''
        if name.rsplit('.', 1)[-1].lower() not in ATTACHMENT_TYPES:
            continue
        docs.append((i, att))
        if len(docs) >= ATTACHMENT_FILES:
            break
    out, changed = {}, False
    missing = [i for i, att in docs if _unread(att) and not _attachment_path(att)]
    if missing and getattr(msg, 'imap_uid', None) and (budget is None or budget[0] > 0) and _take_fetch():
        from email_assistant.attachments import fetch_documents
        try:
            for i, path in fetch_documents(msg, missing).items():
                raw[i]['file_path'] = path
                changed = True
        except Exception as e:  # noqa: BLE001 — IMAP недоступний: спробуємо наступного разу
            logger.info('attachment files of message %s from IMAP: %s', getattr(msg, 'pk', None), e)
    for _i, att in docs:
        tokens = att.get('tokens')
        if _unread(att):
            if not _attachment_path(att):
                continue      # файлу ще немає — не запам'ятовувати «нічого не знайдено»
            if budget is not None:
                if budget[0] <= 0:
                    continue
                budget[0] -= 1
            text = read_attachment_text(att, limit=ATTACHMENT_CHARS)
            changed = True
            if text is None:   # не прочиталось тут (пошкоджений файл, немає бібліотеки PDF) — спробувати пізніше
                att.pop('tokens', None)
                att.pop('tokens_read', None)
                att['tokens_fail'] = int(att.get('tokens_fail') or 0) + 1
                continue
            tokens = sorted(t for t in tokens_in([text])
                            if sum(ch.isdigit() for ch in t) >= ATTACHMENT_MIN_DIGITS)[:ATTACHMENT_TOKENS]
            att.pop('tokens_fail', None)
            # прочитано з файлу (і коли нічого не знайдено)
            att['tokens'], att['tokens_read'], att['tokens_v'] = tokens, True, TOKENS_VERSION
        for tok in tokens or []:
            out.setdefault(tok, att.get('name') or '')
    if changed and getattr(msg, 'pk', None):
        try:
            EmailMessage.objects.filter(pk=msg.pk).update(attachments=raw)
            msg.attachments = raw
        except Exception as e:  # noqa: BLE001 — кеш не обов'язковий
            logger.info('attachment tokens cache for message %s: %s', msg.pk, e)
    return out


def order_in_attachments(msg, customer=None):
    """Замовлення, номер (або трек-номер) якого є у вкладенні листа; кілька різних — лише замовлення клієнта,
    інакше нічого (не вгадувати). Для CRM-прив'язки нового листа (crm_link.find_order)."""
    tokens = attachment_tokens(msg)
    if not tokens:
        return None
    found = [order for order, _reason in orders_for_tokens(set(tokens)).values()]
    if len(found) == 1:
        return found[0]
    if found and customer is not None:
        from email_assistant.crm_link import customer_orders
        own = {o.pk for o in customer_orders(customer)}
        mine = [o for o in found if o.pk in own]
        if len(mine) == 1:
            return mine[0]
    return None


def find_links(texts, messages=()) -> list:
    """Зв'язки для прев'ю листа: замовлення → клієнт, відправлення (статус, трек). messages — листи гілки:
    номери з їхніх вкладень теж (причина «… у вкладенні»)."""
    from shipping.models import Shipment

    found = orders_for_tokens(tokens_in(texts))
    if messages and len(found) < MAX_ORDERS:
        budget, extra, per_message = [PREVIEW_FILES], {}, []
        for m in messages:
            try:
                tokens = attachment_tokens(m, budget)
            except Exception as e:  # noqa: BLE001 — пошкоджене вкладення не ламає прев'ю
                logger.info('attachment tokens of message %s: %s', getattr(m, 'pk', None), e)
                continue
            extra.update(tokens)
            if tokens:
                per_message.append((m, tokens))
        in_files = orders_for_tokens(set(extra))
        for pk, (order, reason) in in_files.items():
            found.setdefault(pk, (order, f'{reason} у вкладенні'))
        if in_files:
            link_from_attachments(per_message)
    if not found:
        return []
    shipments = {}
    for s in (Shipment.objects.filter(order_id__in=found.keys())
              .select_related('carrier').order_by('-pk')):
        shipments.setdefault(s.order_id, []).append(s)

    links = []
    for order, reason in list(found.values())[:MAX_ORDERS]:
        customer = order.crm_customer
        links.append({
            'order':    order,
            'label':    f'{order.source}:{order.order_number}' if order.source else order.order_number,
            'url':      reverse('admin:sales_salesorder_change', args=[order.pk]),
            'reason':   reason,
            'customer': customer,
            'customer_url': reverse('admin:crm_customer_change', args=[customer.pk]) if customer else '',
            'shipments': [{
                'tracking': s.tracking_number,
                'status':   s.get_status_display(),
                'carrier':  str(s.carrier) if s.carrier_id else '',
                'url':      reverse('admin:shipping_shipment_change', args=[s.pk]),
            } for s in shipments.get(order.pk, [])[:3]],
        })
    return links


def link_from_attachments(per_message) -> int:
    """Лист, у вкладенні якого номер замовлення, ще не в хронології цього замовлення (номер прочитано вже після
    збереження листа — синхронізація тоді не зберегла PDF) → crm_link.link_message_to_crm, як для нового листа.
    per_message — [(лист, {номер: файл})]. → скільки листів прив'язано."""
    from crm.models import CustomerTimeline
    from email_assistant.crm_link import link_message_to_crm
    n = 0
    for m, tokens in per_message:
        if not getattr(m, 'pk', None) or not orders_for_tokens(set(tokens)):
            continue
        if CustomerTimeline.objects.filter(email_message=m, related_order_id__isnull=False).exists():
            continue
        try:
            event = link_message_to_crm(m)
        except Exception as e:  # noqa: BLE001 — прев'ю важливіше
            logger.info('link message %s to order from attachment: %s', m.pk, e)
            continue
        n += bool(event is not None and event.related_order_id)
    return n


def find_order_by_tracking(tokens):
    """Замовлення за трек-номером (для прив'язки листа до клієнта в CRM)."""
    for order, reason in orders_for_tokens(tokens).values():
        if reason == 'трек-номер':
            return order
    return None

