"""
email_assistant/context.py — «досьє» листа: що Minerva знає про цю переписку.

Факти беруться з бази, а не вигадуються AI:
  - замовлення, згадані в гілці (номер / трек-номер, див. links.py): позиції, сума, статус, дати;
  - відправлення: перевізник, трек, статус перевізника, затримка, очікувана/фактична доставка;
  - клієнт: картка CRM і нотатки/дзвінки з хронології;
  - інші гілки листування з цим клієнтом (останній лист кожної);
  - лист, пересланий колегою (FW / WG): справжній відправник (`origin`), листи й замовлення, де згадано його адресу
    (або домен компанії), клієнт CRM за адресою / доменом — навіть якщо гілку з ним не пов'язано;
  - склад за артикулами каталогу, названими в листі (`stock`): на складі, у резерві, вільно, очікується, чого бракує.

Використовується:
  - AI-відповіддю (context_text → у промпт як достовірні факти);
  - кнопкою «📜 Історія» (history_events + state_lines; AI-підсумок за бажанням).
"""
import re
from datetime import date, datetime
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

MAX_OTHER_THREADS = 4
MAX_ORIGIN_THREADS = 6
MAX_ORIGIN_ORDERS = 5
MAX_STOCK = 8
MAX_NOTES = 8
# «35 units and 100 units», «500 pcs», «12 Stück», «20 шт» — кількості, коли артикул і число в різних рядках
_QTY_WORDS_RE = re.compile(r"(?<![\w.,])(\d{1,6})\s*(?:pcs|pieces|piece|units?|stk|stück|stueck|st\.|шт)(?!\w)",
                           re.IGNORECASE)


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

    origin = forwarded_origin(msg, thread_msgs)
    customer = None
    if msg.thread_id and msg.thread.crm_customer_id:
        customer = msg.thread.crm_customer
    if customer is None:
        customer = next((l['customer'] for l in links if l['customer']), None)
    if customer is None:
        ev = CustomerTimeline.objects.filter(email_message__in=thread_msgs).select_related('customer').first()
        customer = ev.customer if ev else None
    customer_from_origin = False
    if customer is None and origin:
        customer = _origin_customer(origin)
        customer_from_origin = customer is not None

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
    if origin:   # пересланий лист: листування, де згадано відправника, — навіть без зв'язку з CRM
        known = {t['thread'].pk for t in other_threads}
        for t in _origin_threads(msg, origin):
            if t.pk not in known:
                last = t.messages.filter(is_deleted=False).order_by('-sent_at').first()
                if last:
                    other_threads.append({'thread': t, 'last': last, 'via': origin['from'],
                                          'count': t.messages.filter(is_deleted=False).count()})

    # Що з іншого стосується саме цих замовлень: згадано номер замовлення чи трек-номер
    keys = set()
    for link in links:
        keys.add(link['order'].order_number.lower())
        keys.update(s['tracking'].lower() for s in link['shipments'] if s['tracking'])
    keys.discard('')

    def mentions(*parts):
        text = ' '.join(p or '' for p in parts).lower()
        return any(k in text for k in keys)

    for t in other_threads:
        t['related'] = bool(t.get('via')) or mentions(t['thread'].subject, t['last'].subject,
                                                       (t['last'].body_text or '')[:20000])
    for n in notes:
        n.related = mentions(n.title, n.body)

    orders = [l['order'] for l in links]
    return {
        'message':       msg,
        'thread_msgs':   thread_msgs,
        'links':         links,
        'orders':        orders,
        'customer':      customer,
        'other_threads': other_threads,
        'notes':         notes,
        'projects':      _project_overviews(msg.thread if msg.thread_id else None),
        'origin':        origin,
        'customer_from_origin': customer_from_origin,   # клієнта знайдено лише за адресою / доменом пересланого листа
        'origin_orders': _origin_orders(origin, exclude={o.pk for o in orders}) if origin else [],
        'stock':         stock_for(msg, origin),
    }


# ── Пересланий лист: хто насправді пише ──────────────────────────────────────

def forwarded_origin(msg, thread_msgs=()):
    """
    Внутрішній лист (колега → нам, без зовнішніх учасників), у якому переслано лист ззовні:
    {from, name, subject, text, domain, message} справжнього відправника. Інакше None — у звичайному листі клієнта
    процитований наш лист «пересланим» не вважається.
    """
    from email_assistant.categorize import counterparts
    from email_assistant.crm_link import FREE_MAIL_DOMAINS, is_own, own_addresses
    from email_assistant.quotes import forwarded
    own = own_addresses()
    for m in [msg] + [x for x in reversed(list(thread_msgs)) if x.pk != msg.pk]:
        if counterparts(m, own):          # є зовнішній учасник — це не внутрішнє пересилання
            return None
        fw = forwarded(m.body_text)
        if fw and not is_own(fw['from'], own):
            domain = fw['from'].split('@', 1)[1]
            return dict(fw, message=m, domain='' if domain in FREE_MAIL_DOMAINS else domain)
    return None


def _origin_customer(origin):
    """Клієнт CRM за адресою відправника, інакше — за доменом компанії."""
    from crm.models import Customer
    from email_assistant.crm_link import find_customer
    customer = find_customer([origin['from']])
    if customer is None and origin.get('domain'):
        customer = Customer.objects.filter(email__iendswith='@' + origin['domain']).order_by('-pk').first()
    return customer


def _origin_threads(msg, origin) -> list:
    """Гілки цієї скриньки, де відправник пише сам, отримує наш лист або згаданий у тексті (пересилання колег)."""
    from email_assistant.models import EmailMessage, EmailThread
    needle = '@' + origin['domain'] if origin.get('domain') else origin['from']
    cond = Q(from_email__iexact=origin['from']) | Q(to_emails__icontains=origin['from']) | Q(body_text__icontains=needle)
    ids = (EmailMessage.objects.filter(cond, account=msg.account, is_deleted=False).exclude(thread__isnull=True)
           .exclude(thread_id=msg.thread_id).values_list('thread_id', flat=True).distinct()[:300])
    return list(EmailThread.objects.filter(pk__in=list(ids)).order_by('-last_message_at')[:MAX_ORIGIN_THREADS])


def _origin_orders(origin, exclude=()) -> list:
    """Попередні замовлення цього відправника: email замовлення = адреса (або той самий домен компанії)."""
    from sales.models import SalesOrder
    cond = Q(email__iexact=origin['from'])
    if origin.get('domain'):
        cond |= Q(email__iendswith='@' + origin['domain'])
    return list(SalesOrder.objects.filter(cond).exclude(pk__in=list(exclude)).order_by('-order_date')[:MAX_ORIGIN_ORDERS])


# ── Склад за артикулами з листа ──────────────────────────────────────────────

def _qty(value) -> str:
    text = f'{Decimal(value):f}'
    return text.rstrip('0').rstrip('.') if '.' in text else text


def stock_for(msg, origin=None) -> list:
    """
    Артикули каталогу в листі (тема + новий текст або текст пересланого листа) зі станом складу:
    [{sku, name, requested: [к-сті], on_hand, reserved, available, incoming}]. Без каталогу / складу — [].
    """
    try:
        from email_assistant.quotes import new_text
        from inventory.models import Product
        from inventory.services.stock import annotate_stock
        from offers.services import detect_items
    except ImportError:
        return []
    text = '\n'.join([msg.subject or '', (origin or {}).get('text') or new_text(msg.body_text)])
    items = detect_items(text)[:MAX_STOCK]
    if not items:
        return []
    stock = {p.pk: p for p in annotate_stock(Product.objects.filter(pk__in=[p.pk for p, _q in items]))}
    loose = sorted({Decimal(m.group(1)) for m in _QTY_WORDS_RE.finditer(text)})
    out = []
    for product, qty in items:
        p = stock.get(product.pk, product)
        requested = [qty] if qty else (loose if len(items) == 1 else [])
        out.append({'sku': product.sku, 'name': product.name or '', 'requested': requested,
                    'on_hand': getattr(p, '_on_hand', Decimal(0)), 'reserved': getattr(p, '_reserved', Decimal(0)),
                    'available': getattr(p, '_available', Decimal(0)), 'incoming': getattr(p, '_incoming', Decimal(0))})
    return out


def stock_line(item) -> str:
    """«📦 FT220407: на складі 14, резерв 0, вільно 14 · просять 35 / 100 → бракує 21 / 86»."""
    line = (f"📦 {item['sku']}: на складі {_qty(item['on_hand'])}, резерв {_qty(item['reserved'])}, "
            f"вільно {_qty(item['available'])}" + (f", очікується {_qty(item['incoming'])}" if item['incoming'] else ''))
    if item['requested']:
        short = [max(Decimal(q) - item['available'], Decimal(0)) for q in item['requested']]
        line += f" · просять {' / '.join(_qty(q) for q in item['requested'])}"
        line += (' → вистачає' if not any(short)
                 else ' → бракує ' + ' / '.join(_qty(s) for s in short))
    return line


def _project_overviews(thread) -> list:
    """Збережені огляди проєктів гілки: обіцянки й відкриті питання — щоб відповідь їм не суперечила."""
    try:
        from projects.overview import thread_overviews
        return thread_overviews(thread)
    except Exception:  # noqa: BLE001 — досьє листа не має ламатися через проєкти
        return []


def _shipments(order):
    return list(order.shipments.select_related('carrier').order_by('-pk')[:3])


def state_lines(ctx) -> list:
    """Поточний стан: клієнт, замовлення, відправлення — одним рядком кожне."""
    lines = []
    origin = ctx.get('origin')
    if origin:
        lines.append(f"↪ Переслано колегою: лист від {origin['name'] or origin['from']} <{origin['from']}>"
                     + (f" · «{origin['subject']}»" if origin['subject'] else '')
                     + ('' if ctx['customer'] is not None else ' · клієнта з цією адресою в CRM немає'))
    c = ctx['customer']
    if c is not None:
        extra = ', '.join(x for x in [c.company if c.company and c.company != c.name else '',
                                      c.email, c.country] if x)
        lines.append(f'👤 {c.name}' + (f' ({extra})' if extra else ''))
    lines += [stock_line(item) for item in ctx.get('stock') or []]
    for order in ctx.get('origin_orders') or []:
        total = _money(order.total_price, order.currency)
        lines.append(f'🛒 Раніше замовляв: {order.source}:{order.order_number} від {_fmt(order.order_date)} — '
                     f'{order.get_status_display()}' + (f', {total}' if total else ''))
    for order in ctx['orders']:
        total = _money(order.total_price, order.currency)
        lines.append(f'🛒 {order.source}:{order.order_number} від {_fmt(order.order_date)} — '
                     f'{order.get_status_display()}' + (f', {total}' if total else ''))
        for s in _shipments(order):
            parts = [f'🚚 {s.carrier.name} {s.tracking_number or "без трек-номера"} — {s.get_status_display()}']
            if s.carrier_status_label:
                parts.append(f'остання подія перевізника: «{s.carrier_status_label}»')
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
    """
    Хронологія фактів: [(дата, іконка, текст, scope)] від старих до нових.
    scope='order' — ця переписка, її замовлення й відправлення, листування з тим самим номером;
    scope='customer' — решта по клієнту (інші замовлення/листування, нотатки), в інтерфейсі згорнуто.
    """
    events = []

    def add(when, icon, text, scope='order'):
        if when:
            events.append((_day(when), icon, text, scope))

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

    for order in ctx.get('origin_orders') or []:
        add(order.order_date, '🛒', f'Раніше: замовлення {order.source}:{order.order_number} '
                                    f'({order.get_status_display()})')
    for t in ctx['other_threads']:
        via = f' — згадано {t["via"]}' if t.get('via') else ''
        add(t['last'].sent_at, '✉️', f'Інша переписка: «{t["thread"].subject}» ({t["count"]} лист.){via}',
            'order' if t.get('related') else 'customer')

    for n in ctx['notes']:
        add(n.created_at, '📝', f'{n.get_event_type_display()}: {n.title}',
            'order' if getattr(n, 'related', False) else 'customer')

    events.sort(key=lambda e: e[0] or date.min)
    return events


def used_summary(ctx) -> list:
    """Що саме взято з Minerva — показується користувачу біля AI-відповіді."""
    used = []
    if ctx.get('origin'):
        used.append(f'переслано: лист від {ctx["origin"]["from"]}')
    if ctx['customer'] is not None:
        used.append(f'клієнт {ctx["customer"].name}')
    if ctx.get('stock'):
        used.append(f'склад: {", ".join(i["sku"] for i in ctx["stock"])}')
    if ctx.get('origin_orders'):
        used.append(f'раніші замовлення відправника: {len(ctx["origin_orders"])}')
    for o in ctx['orders']:
        used.append(f'замовлення {o.source}:{o.order_number}')
    shipments = sum(len(l['shipments']) for l in ctx['links'])
    if shipments:
        used.append(f'відправлення: {shipments}')
    if ctx['other_threads']:
        used.append(f'інших листувань: {len(ctx["other_threads"])}')
    if ctx['notes']:
        used.append(f'нотаток CRM: {len(ctx["notes"])}')
    for project, _ov, stale in ctx.get('projects', []):
        used.append(f'огляд проєкту {project.code}' + (' (застарів)' if stale else ''))
    return used


def context_text(ctx, limit=6000) -> str:
    """Факти для промпту AI (обмежено за довжиною). Огляд проєкту — першим: обрізається хронологія, а не він."""
    parts = []
    if ctx.get('projects'):
        from projects.overview import facts_text
        parts += [facts_text(p, ov, stale)[:2500] for p, ov, stale in ctx['projects'][:2]]
    state = state_lines(ctx)
    if state:
        parts.append('ПОТОЧНИЙ СТАН:\n' + '\n'.join(state))
    history = history_events(ctx)
    if history:
        parts.append('ХРОНОЛОГІЯ:\n' + '\n'.join(
            f'{_fmt(d)} {icon} {text}' + ('' if scope == 'order' else ' [інше по клієнту]')
            for d, icon, text, scope in history[-25:]))
    origin = ctx.get('origin')
    if origin:
        parts.append(f"ПЕРЕСЛАНИЙ ЛИСТ (колега переслав нам; справжній співрозмовник — цей відправник):\n"
                     f"від {origin['name'] or origin['from']} <{origin['from']}>, тема «{origin['subject']}»\n"
                     f"{origin['text'][:1500]}")
    if ctx['other_threads']:
        snippets = []
        for t in ctx['other_threads']:
            last = t['last']
            snippets.append(f'«{t["thread"].subject}» — останній лист {_fmt(last.sent_at)} від '
                            f'{last.from_email}' + (f' (згадано {t["via"]})' if t.get('via') else '')
                            + f':\n{(last.body_text or "")[:400]}')
        parts.append('ІНШІ ЛИСТУВАННЯ З ЦИМ КЛІЄНТОМ / ВІДПРАВНИКОМ:\n' + '\n---\n'.join(snippets))
    extra = 2500 * len(ctx.get('projects', [])[:2])     # огляд проєкту не забирає місце в хронології
    return '\n\n'.join(parts)[:limit + extra]
