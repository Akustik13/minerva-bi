"""🧾 Продажі й 🚚 відправлення для агента (MCP, agent/toolsets.py) — лише читання.

find_orders — пошук замовлень (номер, клієнт, email, трек-номер, артикул, статус, джерело, період, оплата; повністю —
get_order з agent/shipments.py), sales_summary — виручка й кількість продажів за період з порівнянням (лише
sales/revenue.py: sale_orders / sale_lines, revenue / line_revenue, валюта Minerva, суми без курсу — окремо «без
курсу»), find_shipments — відправлення від нас і посилки до нас (без адрес і телефонів: лише ім'я й країна
отримувача). Права — `sales.view_salesorder` / `shipping.view_shipment` людини (у `tools_for` і ще раз у інструменті).
"""
import datetime
from decimal import Decimal

from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from agent.toolsets import clamp, date, money, one_line, parse_date, require

SALES_PERM = 'sales.view_salesorder'
SHIP_PERM = 'shipping.view_shipment'
LIST_MAX = 50
OUT_MAX = 6000
GROUPS = ('source', 'month', 'product', 'customer', 'country')
GROUP_TITLES = {'source': 'джерелом', 'month': 'місяцями', 'product': 'товарами', 'customer': 'клієнтами',
                'country': 'країнами доставки'}

_DATE = {'type': 'string', 'description': 'Дата YYYY-MM-DD або DD.MM.YYYY'}

FIND_ORDERS = {
    'name': 'find_orders',
    'description': 'Пошук замовлень: query — номер замовлення, клієнт / компанія, email, трек-номер або частина '
                   'артикула; фільтри — статус (received / processing / shipped / delivered / cancelled), джерело, '
                   'період дати замовлення, статус оплати (unpaid / pending / paid / refunded / failed). Повертає '
                   'номер, дату, джерело, клієнта, статус, суму з валютою, оплату, трек-номер, строк відправки. '
                   'Замовлення повністю (позиції, склад, адреса) — get_order.',
    'input_schema': {'type': 'object', 'properties': {
        'query': {'type': 'string'}, 'status': {'type': 'string'}, 'source': {'type': 'string'},
        'date_from': _DATE, 'date_to': _DATE, 'payment_status': {'type': 'string'},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': LIST_MAX}}}}
SALES_SUMMARY = {
    'name': 'sales_summary',
    'description': 'Аналітика продажів за період (date_from / date_to або останні days днів, за замовчуванням 30): '
                   'виручка у валюті Minerva (суми без курсу — окремо), кількість замовлень, середній чек, порівняння '
                   'з попереднім періодом такої ж довжини (compare) і топ груп — group_by: source (джерело), month, '
                   'product (за рядками замовлень), customer, country. Продаж — як у дашборді: Sale, не скасоване, '
                   '«Впливає на склад».',
    'input_schema': {'type': 'object', 'properties': {
        'date_from': _DATE, 'date_to': _DATE, 'days': {'type': 'integer', 'minimum': 1, 'maximum': 3660},
        'group_by': {'type': 'string', 'enum': list(GROUPS)},
        'top': {'type': 'integer', 'minimum': 1, 'maximum': 30}, 'compare': {'type': 'boolean'}}}}
FIND_SHIPMENTS = {
    'name': 'find_shipments',
    'description': 'Відправлення за останні days днів (за замовчуванням 60): 📤 від нас (замовлення клієнтів) і 📥 до '
                   'нас (посилки постачальників). query — номер замовлення, трек-номер або код закупівлі PO-…; '
                   'фільтри — статус (draft / submitted / label_ready / in_transit / delivered / error / cancelled), '
                   'direction out | in, перевізник. Повертає id, напрямок, замовлення чи PO, перевізника й послугу, '
                   'статус, трек-номер, дати, очікувану доставку й затримку, ім’я й країну отримувача.',
    'input_schema': {'type': 'object', 'properties': {
        'query': {'type': 'string'}, 'status': {'type': 'string'},
        'direction': {'type': 'string', 'enum': ['out', 'in']}, 'carrier': {'type': 'string'},
        'days': {'type': 'integer', 'minimum': 1, 'maximum': 730},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': LIST_MAX}}}}

TOOLS = [FIND_ORDERS, SALES_SUMMARY, FIND_SHIPMENTS]


def _can(user, perm) -> bool:
    return bool(user is not None and getattr(user, 'is_active', False) and user.has_perm(perm))


def tools_for(user) -> list:
    out = []
    if _can(user, SALES_PERM):
        out += [FIND_ORDERS, SALES_SUMMARY]
    if _can(user, SHIP_PERM):
        out.append(FIND_SHIPMENTS)
    return out


def _error(text):
    from agent.tools import ToolError
    return ToolError(text)


def _cut(text) -> str:
    return text if len(text) <= OUT_MAX else text[:OUT_MAX].rstrip() + '\n…(обрізано — звузьте пошук)'


def _date_arg(args, name):
    value = args.get(name)
    if value in (None, ''):
        return None
    d = parse_date(value)
    if d is None:
        raise _error(f'{name}: дата YYYY-MM-DD або DD.MM.YYYY')
    return d


def _choice(value, choices, what):
    """Код або назва з choices (без регістру) → код; невідоме — ToolError з переліком кодів."""
    value = str(value or '').strip()
    if not value:
        return ''
    for code, label in choices:
        if code and value.lower() in (str(code).lower(), str(label).lower()):
            return code
    raise _error(f'{what} — один з: ' + ', '.join(str(c) for c, _ in choices if c))


# ── find_orders ──────────────────────────────────────────────────────────────

def order_total(o) -> str:
    """Сума замовлення (як sales/revenue: total_price, інакше рядки у своїй валюті); рядки — з prefetch."""
    if o.total_price is not None:
        return money(o.total_price, o.currency)
    sums = {}
    for line in o.lines.all():
        if line.total_price is not None:
            amount = line.total_price
        elif line.unit_price is not None and line.qty is not None:
            amount = line.unit_price * line.qty
        else:
            continue
        cur = (line.currency or o.currency or '').upper()
        sums[cur] = sums.get(cur, Decimal('0')) + amount
    return ' + '.join(money(v, c) for c, v in sums.items() if v) or '—'


def _deadline(o, today) -> str:
    if not o.shipping_deadline:
        return ''
    late = (o.shipping_deadline < today and o.shipped_at is None
            and o.status not in ('shipped', 'delivered', 'cancelled'))
    return f'відправити до {date(o.shipping_deadline)}' + (' (прострочено)' if late else '')


def find_orders(ctx, args):
    from sales.models import SalesOrder, SalesOrderLine
    from shipping.models import Shipment
    require(ctx.task.owner, SALES_PERM)
    qs = SalesOrder.objects.all()
    query = one_line(args.get('query'), 120)
    if query:
        number = query.lstrip('#')
        lines = SalesOrderLine.objects.filter(Q(sku_raw__icontains=query) | Q(product__sku__icontains=query))
        parcels = Shipment.objects.filter(tracking_number__icontains=query, order__isnull=False)
        qs = qs.filter(Q(order_number__icontains=number) | Q(client__icontains=query)
                       | Q(contact_name__icontains=query) | Q(ship_company__icontains=query)
                       | Q(ship_name__icontains=query) | Q(email__icontains=query) | Q(ship_email__icontains=query)
                       | Q(tracking_number__icontains=query) | Q(pk__in=lines.values('order_id'))
                       | Q(pk__in=parcels.values('order_id')))
    status = _choice(args.get('status'), SalesOrder.STATUS_CHOICES, 'status')
    if status:
        qs = qs.filter(status=status)
    pay = _choice(args.get('payment_status'), SalesOrder.PAYMENT_STATUS_CHOICES, 'payment_status')
    if pay:
        qs = qs.filter(payment_status=pay)
    if str(args.get('source') or '').strip():
        qs = qs.filter(source__iexact=str(args['source']).strip())
    d_from, d_to = _date_arg(args, 'date_from'), _date_arg(args, 'date_to')
    if d_from:
        qs = qs.filter(order_date__gte=d_from)
    if d_to:
        qs = qs.filter(order_date__lte=d_to)
    limit = clamp(args.get('limit'), 20, 1, LIST_MAX)
    total = qs.count()
    if not total:
        return 'Замовлень за цими умовами немає.'
    today = timezone.localdate()
    labels = dict(SalesOrder.PAYMENT_STATUS_CHOICES)
    out = [f'Замовлень: {total}' + (f' — показано {limit} найновіших' if total > limit else '')
           + ' (повністю — get_order з номером і джерелом):']
    rows = qs.prefetch_related('lines').order_by(F('order_date').desc(nulls_last=True), '-pk')[:limit]
    for o in rows:
        who = one_line(o.client or o.ship_company or o.contact_name or o.email or '—', 80)
        parts = [f'{o.order_number} [{o.source}]', date(o.order_date), who, o.get_status_display(), order_total(o)]
        if o.payment_status:
            parts.append(f'оплата: {labels.get(o.payment_status, o.payment_status)}')
        if o.tracking_number:
            parts.append(f'трек: {o.tracking_number}')
        if o.document_type != 'SALE':
            parts.append(f'тип: {o.get_document_type_display()}')
        if not o.affects_stock:
            parts.append('поза складом і статистикою')
        dl = _deadline(o, today)
        if dl:
            parts.append(dl)
        out.append('- ' + ' · '.join(parts))
    return _cut('\n'.join(out))


# ── sales_summary ────────────────────────────────────────────────────────────

def _period(args):
    """(від, до, днів, попередній від, попередній до) — обидва включно, як дашборд (dashboard.views.period_bounds)."""
    d_from, d_to = _date_arg(args, 'date_from'), _date_arg(args, 'date_to')
    today = timezone.localdate()
    if d_from or d_to:
        d_to = d_to or today
        d_from = d_from or d_to - datetime.timedelta(days=29)
        if d_from > d_to:
            raise _error('date_from пізніше за date_to')
        days = (d_to - d_from).days + 1
    else:
        days = clamp(args.get('days'), 30, 1, 3660)
        d_to, d_from = today, today - datetime.timedelta(days=days - 1)
    prev_to = d_from - datetime.timedelta(days=1)
    return d_from, d_to, days, prev_to - datetime.timedelta(days=days - 1), prev_to


def _customer_expr(prefix):
    from django.db.models import CharField, Value
    from django.db.models.functions import Coalesce, NullIf
    return Coalesce(NullIf(F(prefix + 'client'), Value('')), NullIf(F(prefix + 'ship_company'), Value('')),
                    NullIf(F(prefix + 'contact_name'), Value('')), F(prefix + 'email'), output_field=CharField())


def _order_group(group):
    """Групування замовлень: поле або функція(префікс) → вираз (формат sales.revenue.revenue)."""
    from django.db.models.functions import TruncMonth
    if group == 'month':
        return lambda p: TruncMonth(p + 'order_date')
    if group == 'customer':
        return _customer_expr
    return {'source': 'source', 'country': 'addr_country'}[group]


def _expr(spec, prefix=''):
    return spec(prefix) if callable(spec) else F(prefix + spec)


def _stats(start, end, group):
    """{'money': Money, 'orders': N, 'groups': {ключ: {'money', 'orders', 'qty', 'name'}}} за період."""
    from sales import revenue as rv
    rates = rv.Rates()
    orders = rv.sale_orders().filter(order_date__gte=start, order_date__lte=end)
    out = {'money': rv.revenue(orders, rates=rates), 'orders': orders.count(), 'groups': {}}
    groups = out['groups']
    if group == 'product':
        lines = rv.sale_lines().filter(order__order_date__gte=start, order__order_date__lte=end)
        for key, m in rv.line_revenue(lines, by={'sku': 'product__sku'}, rates=rates).items():
            groups.setdefault(key[0], {})['money'] = m
        for r in lines.order_by().values('product__sku', 'product__name').annotate(
                qty=Sum('qty'), n=Count('order', distinct=True)):
            g = groups.setdefault(r['product__sku'], {})
            g.update(orders=r['n'], qty=r['qty'], name=r['product__name'] or '')
        return out
    spec = _order_group(group)
    for key, m in rv.revenue(orders, by={'g': spec}, rates=rates).items():
        groups.setdefault(key[0], {})['money'] = m
    for r in orders.order_by().annotate(_g=_expr(spec)).values('_g').annotate(n=Count('id')):
        groups.setdefault(r['_g'], {})['orders'] = r['n']
    return out


def _change(now, before) -> str:
    now, before = float(now or 0), float(before or 0)
    if not before:
        return 'нове' if now else '0'
    pct = (now - before) / before * 100
    return f'{pct:+.1f} %'.replace('.', ',')


def _key_label(group, key) -> str:
    if key in (None, ''):
        return '—'
    if group == 'month':
        return key.strftime('%m.%Y') if hasattr(key, 'strftime') else str(key)[:7]
    return one_line(key, 80)


def sales_summary(ctx, args):
    from sales.revenue import Money, home_currency
    require(ctx.task.owner, SALES_PERM)
    group = str(args.get('group_by') or 'source').strip()
    if group not in GROUPS:
        raise _error('group_by — один з: ' + ', '.join(GROUPS))
    top = clamp(args.get('top'), 10, 1, 30)
    compare = args.get('compare', True) not in (False, 'false', 0, '0')
    start, end, days, p_start, p_end = _period(args)
    home = home_currency()
    now = _stats(start, end, group)
    prev = _stats(p_start, p_end, group) if compare else None
    m = now['money']
    out = [f'Продажі {date(start)} – {date(end)} ({days} дн.), суми у валюті Minerva {home}:',
           f'Виручка: {m.full_text} · замовлень: {now["orders"]}'
           + (f' · середній чек: {Money(home, m.total / now["orders"]).text}'
              if now['orders'] and m.total and not m.missing else '')]
    if prev is not None:
        pm = prev['money']
        out.append(f'Попередній період {date(p_start)} – {date(p_end)}: {pm.full_text} · замовлень: {prev["orders"]} '
                   f'→ виручка {_change(m.total, pm.total)}, замовлень {_change(now["orders"], prev["orders"])}')
    groups = now['groups']
    zero = Money(home)
    if group == 'month':
        keys = sorted(groups, key=lambda k: (k is None, str(k)))[-top:]
    else:
        keys = sorted(groups, key=lambda k: (-(groups[k].get('money') or zero).total,
                                             -(groups[k].get('orders') or 0)))[:top]
    head = f'За {GROUP_TITLES[group]}' + (f' (топ {len(keys)} з {len(groups)})' if len(groups) > len(keys) else '')
    out.append(head + (' — виручка за рядками замовлень:' if group == 'product' else ':'))
    if not keys:
        out.append('- продажів немає')
    for i, key in enumerate(keys, 1):
        g = groups[key]
        gm = g.get('money') or zero
        share = f' ({float(gm.total) / float(m.total) * 100:.1f} %)'.replace('.', ',') if m.total else ''
        label = _key_label(group, key) + (f' {one_line(g["name"], 60)}' if g.get('name') else '')
        row = f'{i}. {label} — {gm.full_text}{share} · замовлень: {g.get("orders", 0)}'
        if g.get('qty') is not None:
            row += f' · {g["qty"].normalize():f} шт' if isinstance(g['qty'], Decimal) else f' · {g["qty"]} шт'
        if prev is not None and group != 'month':
            pg = prev['groups'].get(key) or {}
            pgm = pg.get('money') or zero
            row += f' · раніше {pgm.text} ({_change(gm.total, pgm.total)})'
        out.append(row)
    return _cut('\n'.join(out))


# ── find_shipments ───────────────────────────────────────────────────────────

def _eta(s, today) -> str:
    start = s.eta_from or s.eta_to or s.carrier_eta
    end = s.eta_to or start
    parts = [f'очікується {date(start)}' + (f'–{date(end)}' if end and end != start else '')] if start else []
    open_ = s.status not in ('delivered', 'cancelled', 'error', 'draft')
    if s.carrier_delayed and open_:
        parts.append('⚠️ затримка (перевізник)')
    elif end and end < today and open_:
        parts.append('⚠️ мало бути доставлено')
    return ' · '.join(parts)


def find_shipments(ctx, args):
    from shipping.models import Shipment
    require(ctx.task.owner, SHIP_PERM)
    days = clamp(args.get('days'), 60, 1, 730)
    qs = Shipment.objects.filter(created_at__gte=timezone.now() - datetime.timedelta(days=days))
    query = one_line(args.get('query'), 120)
    if query:
        number = query.lstrip('#')
        qs = qs.filter(Q(order__order_number__icontains=number) | Q(tracking_number__icontains=query)
                       | Q(purchase_order__code__icontains=query) | Q(reference__icontains=query)
                       | (Q(pk=int(number)) if number.isdigit() else Q(pk__in=[])))
    status = _choice(args.get('status'), Shipment.Status.choices, 'status')
    if status:
        qs = qs.filter(status=status)
    direction = str(args.get('direction') or '').strip().lower()
    if direction:
        if direction not in (Shipment.OUT, Shipment.IN):
            raise _error('direction — out або in')
        qs = qs.filter(direction=direction)
    carrier = str(args.get('carrier') or '').strip()
    if carrier:
        qs = qs.filter(Q(carrier__name__iexact=carrier) | Q(carrier__carrier_type__iexact=carrier)
                       | (Q(carrier_id=int(carrier)) if carrier.isdigit() else Q(pk__in=[])))
    limit = clamp(args.get('limit'), 20, 1, LIST_MAX)
    total = qs.count()
    if not total:
        return f'Відправлень за {days} дн. за цими умовами немає.'
    today = timezone.localdate()
    out = [f'Відправлень за {days} дн.: {total}' + (f' — показано {limit} найновіших' if total > limit else '') + ':']
    rows = qs.select_related('carrier', 'order', 'purchase_order__supplier').order_by('-created_at', '-pk')[:limit]
    for s in rows:
        icon = '📥' if s.direction == Shipment.IN else '📤'
        if s.order_id:
            what = f'замовлення {s.order.order_number} [{s.order.source}]'
        elif s.purchase_order_id:
            po = s.purchase_order
            what = f'закупівля {po.code or "#" + str(po.pk)}' + (f' ({one_line(po.supplier.name, 60)})'
                                                                 if po.supplier_id else '')
        else:
            what = 'без замовлення'
        carrier_text = s.carrier.name if s.carrier_id else '—'
        if s.carrier_service:
            carrier_text += f' ({one_line(s.carrier_service, 60)})'
        parts = [f'#{s.pk} {icon} {what}', carrier_text, s.get_status_display()]
        if s.carrier_status_label:
            parts.append(f'у перевізника: {one_line(s.carrier_status_label, 80)}')
        parts.append(f'трек: {s.tracking_number}' if s.tracking_number else 'без трек-номера')
        parts.append(f'створено {date(s.created_at)}')
        if s.submitted_at:
            parts.append(f'передано {date(s.submitted_at)}')
        if s.delivered_at:
            parts.append(f'доставлено {date(s.delivered_at)}')
        eta = _eta(s, today) if not s.delivered_at else ''
        if eta:
            parts.append(eta)
        if s.direction == Shipment.OUT:
            to = one_line(s.recipient_company or s.recipient_name, 60)
            if to or s.recipient_country:
                parts.append('отримувач: ' + ', '.join(x for x in (to, s.recipient_country) if x))
        out.append('- ' + ' · '.join(parts))
    return _cut('\n'.join(out))


HANDLERS = {'find_orders': find_orders, 'sales_summary': sales_summary, 'find_shipments': find_shipments}
