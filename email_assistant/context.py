"""
email_assistant/context.py — «досьє» листа: що Minerva знає про цю переписку.

Факти беруться з бази, а не вигадуються AI:
  - замовлення, згадані в гілці (номер / трек-номер, див. links.py): позиції, сума, статус, дати;
  - відправлення: перевізник, трек, статус перевізника, затримка, очікувана/фактична доставка;
  - клієнт: картка CRM і нотатки/дзвінки з хронології;
  - інші гілки листування з цим клієнтом (останній лист кожної).

Використовується:
  - AI-відповіддю (context_text → у промпт як достовірні факти);
  - кнопкою «📜 Історія» (history_events + state_lines; AI-підсумок за бажанням).
"""
from datetime import date, datetime

from django.utils import timezone

MAX_OTHER_THREADS = 4
MAX_NOTES = 8


def _day(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    return value


def _fmt(value) -> str:
    d = _day(value)
    return d.strftime('%d.%m.%Y') if d else '?'


def _money(amount, currency) -> str:
    if amount in (None, ''):
        return ''
    return f'{float(amount):.2f} {currency or ""}'.strip()


def build_context(msg) -> dict:
    """Зібрати досьє для листа (і всієї його гілки)."""
    from crm.models import CustomerTimeline
    from email_assistant.links import find_links
    from email_assistant.models import EmailThread

    if msg.thread_id:
        thread_msgs = list(msg.thread.messages.filter(is_deleted=False).order_by('sent_at'))
    else:
        thread_msgs = [msg]
    texts = []
    for m in thread_msgs:
        texts += [m.subject or '', (m.body_text or '')[:50000]]
    links = find_links(texts)

    customer = None
    if msg.thread_id and msg.thread.crm_customer_id:
        customer = msg.thread.crm_customer
    if customer is None:
        customer = next((l['customer'] for l in links if l['customer']), None)
    if customer is None:
        ev = CustomerTimeline.objects.filter(email_message__in=thread_msgs).select_related('customer').first()
        customer = ev.customer if ev else None

    other_threads, notes = [], []
    if customer is not None:
        for t in (EmailThread.objects.filter(account=msg.account, crm_customer=customer)
                  .exclude(pk=msg.thread_id).order_by('-last_message_at')[:MAX_OTHER_THREADS]):
            last = t.messages.filter(is_deleted=False).order_by('-sent_at').first()
            if last:
                other_threads.append({'thread': t, 'last': last, 'count': t.messages.filter(is_deleted=False).count()})
        notes = list(CustomerTimeline.objects.filter(customer=customer)
                     .exclude(event_type__in=['email_in', 'email_out', 'order'])
                     .order_by('-created_at')[:MAX_NOTES])

    return {
        'message':       msg,
        'thread_msgs':   thread_msgs,
        'links':         links,
        'orders':        [l['order'] for l in links],
        'customer':      customer,
        'other_threads': other_threads,
        'notes':         notes,
    }


def _shipments(order):
    return list(order.shipments.select_related('carrier').order_by('-pk')[:3])


def state_lines(ctx) -> list:
    """Поточний стан: клієнт, замовлення, відправлення — одним рядком кожне."""
    lines = []
    c = ctx['customer']
    if c is not None:
        extra = ', '.join(x for x in [c.company if c.company and c.company != c.name else '',
                                      c.email, c.country] if x)
        lines.append(f'👤 {c.name}' + (f' ({extra})' if extra else ''))
    for order in ctx['orders']:
        total = _money(order.total_price, order.currency)
        lines.append(f'🛒 {order.source}:{order.order_number} від {_fmt(order.order_date)} — '
                     f'{order.get_status_display()}' + (f', {total}' if total else ''))
        for s in _shipments(order):
            parts = [f'🚚 {s.carrier.name} {s.tracking_number or "без трек-номера"} — {s.get_status_display()}']
            if s.carrier_status_label:
                parts.append(f'«{s.carrier_status_label}»')
            if s.carrier_delayed:
                parts.append('⚠️ затримка')
            if s.delivered_at:
                parts.append(f'доставлено {_fmt(s.delivered_at)}')
            elif s.eta_to or s.eta_from:
                parts.append(f'очікується {_fmt(s.eta_from or s.eta_to)}'
                             + (f'–{_fmt(s.eta_to)}' if s.eta_from and s.eta_to and s.eta_to != s.eta_from else ''))
            lines.append(' · '.join(parts))
    return lines


def history_events(ctx) -> list:
    """Хронологія фактів: [(дата, іконка, текст)] від старих до нових."""
    events = []

    def add(when, icon, text):
        if when:
            events.append((_day(when), icon, text))

    for order in ctx['orders']:
        label = f'{order.source}:{order.order_number}'
        lines = list(order.lines.select_related('product')[:6])
        items = ', '.join(f'{(l.sku_raw or l.product.sku)} × {int(l.qty) if l.qty == int(l.qty) else l.qty}'
                          for l in lines)
        total = _money(order.total_price, order.currency)
        add(order.order_date, '🛒', f'Замовлення {label}' + (f': {items}' if items else '')
            + (f' ({total})' if total else ''))
        add(order.order_confirm_sent_at, '📥', f'Клієнту надіслано підтвердження замовлення {label}')
        add(order.shipped_at, '📦', f'Замовлення {label} відправлено')
        add(order.ship_notify_sent_at, '📧', f'Клієнту надіслано повідомлення про відправку {label}')
        add(order.delivered_at, '✅', f'Замовлення {label} доставлено')
        for s in _shipments(order):
            add(s.created_at, '🚚', f'Створено відправлення {s.carrier.name} {s.tracking_number}'.rstrip())
            add(s.delivered_at, '✅', f'Перевізник: доставлено ({s.tracking_number})')

    for m in ctx['thread_msgs']:
        icon = '📤' if m.folder == 'sent' or m.from_email == ctx['message'].account.email_address else '📨'
        who = m.from_name or m.from_email
        add(m.sent_at, icon, f'{who}: {m.subject or "(без теми)"}')

    for t in ctx['other_threads']:
        add(t['last'].sent_at, '✉️', f'Інша переписка: «{t["thread"].subject}» ({t["count"]} лист.)')

    for n in ctx['notes']:
        add(n.created_at, '📝', f'{n.get_event_type_display()}: {n.title}')

    events.sort(key=lambda e: e[0] or date.min)
    return events


def used_summary(ctx) -> list:
    """Що саме взято з Minerva — показується користувачу біля AI-відповіді."""
    used = []
    if ctx['customer'] is not None:
        used.append(f'клієнт {ctx["customer"].name}')
    for o in ctx['orders']:
        used.append(f'замовлення {o.source}:{o.order_number}')
    shipments = sum(len(l['shipments']) for l in ctx['links'])
    if shipments:
        used.append(f'відправлення: {shipments}')
    if ctx['other_threads']:
        used.append(f'інших листувань: {len(ctx["other_threads"])}')
    if ctx['notes']:
        used.append(f'нотаток CRM: {len(ctx["notes"])}')
    return used


def context_text(ctx, limit=6000) -> str:
    """Факти для промпту AI (обмежено за довжиною)."""
    parts = []
    state = state_lines(ctx)
    if state:
        parts.append('ПОТОЧНИЙ СТАН:\n' + '\n'.join(state))
    history = history_events(ctx)
    if history:
        parts.append('ХРОНОЛОГІЯ:\n' + '\n'.join(f'{_fmt(d)} {icon} {text}' for d, icon, text in history[-25:]))
    if ctx['other_threads']:
        snippets = []
        for t in ctx['other_threads']:
            last = t['last']
            snippets.append(f'«{t["thread"].subject}» — останній лист {_fmt(last.sent_at)} від '
                            f'{last.from_email}:\n{(last.body_text or "")[:400]}')
        parts.append('ІНШІ ЛИСТУВАННЯ З ЦИМ КЛІЄНТОМ:\n' + '\n---\n'.join(snippets))
    return '\n\n'.join(parts)[:limit]
