"""📦 Склад і 📝 Angebote для агента (MCP і завдання, agent/toolsets.py) — лише читання, пропозицій тут немає.

stock_report — залишки й «що замовити»: low — `stock.low_stock_items` (те саме, що «Сигнали» й дашборд; вимкнено в
налаштуваннях складу — пораховано за тими самими правилами), reorder — товари з попитом, яким бракує
(`stock.reorder_advice`: ціль = max(3 міс продажів × 1,2; точка дозамовлення) мінус доступно й уже замовлене), all —
усі активні; доступно — `annotate_stock` (на складі − резерв), «очікується» — відкриті закупівлі, уже замовлені в
постачальника (`po_open_qty`). Право `inventory.view_product`.
stock_movements — рухи складу товару (`InventoryTransaction`, кількість зі знаком, локація, документ, замовлення з
ключа руху); право `inventory.view_inventorytransaction`.
find_offers — Angebote (номер, версія, статус, клієнт, дати, сума, позицій, проєкт); нова чернетка — propose_offer
(agent/offers_tool.py). Право `offers.view_offer`.
"""
import datetime
import re
from decimal import Decimal

from django.db.models import DecimalField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from agent.toolsets import clamp, date, money, one_line, require

STOCK_PERM = 'inventory.view_product'
MOVES_PERM = 'inventory.view_inventorytransaction'
OFFERS_PERM = 'offers.view_offer'
OUT_MAX = 6000
SALES_DAYS = 90                    # як low_stock_items: продажі за 3 міс, без них — 2 роки з меншою вагою
_DEC = DecimalField(max_digits=18, decimal_places=3)
_ZERO = Value(Decimal('0'), output_field=_DEC)
STATUS = {'critical': '🔥 терміново', 'warning': '⚠️ мало', 'low': '📉 менше ніж на 3 міс', 'ok': '✅ достатньо',
          'no_sales': '💤 без продажів'}
SEVERITY = {'critical': 0, 'warning': 1, 'low': 2, 'ok': 3, 'no_sales': 4}
OFFER_NUMBER_RE = re.compile(r'^[A-Za-z]{1,6}-\d{4}-\d+', re.I)

STOCK_REPORT = {
    'name': 'stock_report',
    'description': 'Склад Minerva: на складі, резерв, доступно, очікується (замовлено в постачальника), точка '
                   'дозамовлення і скільки замовити з поясненням. mode: low — що пора дозамовити (як «Сигнали»), '
                   'reorder — товари з продажами / точкою дозамовлення, яким бракує, all — усі активні. category — '
                   'частина назви категорії, query — артикул, назва чи виробник.',
    'input_schema': {'type': 'object', 'properties': {
        'mode': {'type': 'string', 'enum': ['low', 'reorder', 'all']}, 'category': {'type': 'string'},
        'query': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 80}}}}
STOCK_MOVES = {
    'name': 'stock_movements',
    'description': 'Рухи складу товару за останні days днів (найновіші першими): дата, тип (прихід, витрата, '
                   'резерв, коригування), кількість зі знаком, локація, документ-підстава, замовлення.',
    'input_schema': {'type': 'object', 'properties': {
        'sku': {'type': 'string', 'description': 'Артикул товару (SKU) зі складу Minerva'},
        'days': {'type': 'integer', 'minimum': 1, 'maximum': 730},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, 'required': ['sku']}}
FIND_OFFERS = {
    'name': 'find_offers',
    'description': 'Angebote (комерційні пропозиції): номер, версія, статус, клієнт / контакт, дата, дійсна до, сума '
                   'нетто й валюта, позицій, проєкт. query — номер A-…, клієнт (компанія, ім’я, email), артикул або '
                   'код проєкту; status; за останні days днів (номер — за весь час). Нова чернетка — propose_offer.',
    'input_schema': {'type': 'object', 'properties': {
        'query': {'type': 'string'},
        'status': {'type': 'string', 'enum': ['draft', 'review', 'approved', 'sent', 'accepted', 'rejected',
                                              'expired', 'cancelled']},
        'days': {'type': 'integer', 'minimum': 1, 'maximum': 3650},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}}}
TOOLS = [STOCK_REPORT, STOCK_MOVES, FIND_OFFERS]
KINDS = {}
APPLY_PERMS = ()


def _has(user, perm) -> bool:
    return bool(user is not None and getattr(user, 'is_active', False) and user.has_perm(perm))


def _offers_installed() -> bool:
    try:
        import offers.models  # noqa: F401
    except ImportError:
        return False
    return True


def tools_for(user) -> list:
    out = []
    if _has(user, STOCK_PERM):
        out.append(STOCK_REPORT)
    if _has(user, MOVES_PERM):
        out.append(STOCK_MOVES)
    if _offers_installed() and _has(user, OFFERS_PERM):
        out.append(FIND_OFFERS)
    return out


def _error(text):
    from agent.tools import ToolError
    return ToolError(text)


def _n(value) -> str:
    """3.000 → «3», 2.500 → «2.5», -4 → «-4»."""
    try:
        d = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return str(value)
    return format(d.quantize(Decimal(1)) if d == d.to_integral_value() else d.normalize(), 'f')


def _fit(head, lines, limit=OUT_MAX) -> str:
    out, size = [head], len(head)
    for i, line in enumerate(lines):
        if size + len(line) + 1 > limit:
            out.append(f'… ще {len(lines) - i} не вмістилось — звузьте запит (query / category / limit)')
            break
        out.append(line)
        size += len(line) + 1
    return '\n'.join(out)


# ── Залишки й «що замовити» ──────────────────────────────────────────────────

def _products(ctx, args):
    from inventory.models import Product, ProductCategory
    qs = Product.objects.filter(is_active=True)
    category = ' '.join(str(args.get('category') or '').split())[:100]
    if category:
        slugs = list(ProductCategory.objects.filter(Q(name__icontains=category) | Q(slug__icontains=category))
                     .values_list('slug', flat=True))
        qs = qs.filter(Q(category__in=slugs) | Q(category__icontains=category))
    query = ' '.join(str(args.get('query') or '').split())[:100]
    if query:
        qs = qs.filter(Q(sku__icontains=query) | Q(sku_short__icontains=query) | Q(name__icontains=query)
                       | Q(manufacturer__icontains=query))
    if ctx.allowed is not None:
        qs = qs.filter(pk__in=ctx.allowed)
    return qs


def _advice(qs) -> list:
    """reorder_advice для кожного товару — ті самі вхідні дані, що `stock.low_stock_items`."""
    from inventory.services import stock
    today = timezone.now().date()

    def sold_since(since):
        return (stock.monthly_sales_qs(since).filter(product=OuterRef('pk'))
                .values('product').annotate(t=Sum('qty')).values('t'))

    qs = stock.annotate_stock(qs).annotate(
        _sold=Coalesce(Subquery(sold_since(today - datetime.timedelta(days=SALES_DAYS)), output_field=_DEC), _ZERO),
        _sold_2y=Coalesce(Subquery(sold_since(today - datetime.timedelta(days=730)), output_field=_DEC), _ZERO),
        _confirmed=Coalesce(Subquery(stock.incoming_subquery(stock.CONFIRMED_PO_STATUSES), output_field=_DEC),
                            _ZERO))
    rows = []
    for p in qs:
        monthly = float(p._sold) / (SALES_DAYS / 30.0)
        if monthly <= 0:
            monthly = float(p._sold_2y) / 24 / 6
        d = stock.reorder_advice(available=p._available, monthly=monthly, reorder_point=p.reorder_point,
                                 incoming=p._confirmed)
        d.update(product=p, pk=p.pk, sku=p.sku, name=p.name, on_hand=float(p._on_hand), reserved=float(p._reserved),
                 available=float(p._available), incoming=float(p._confirmed), reorder_point=p.reorder_point)
        rows.append(d)
    return rows


def _reason(d) -> str:
    parts = []
    if d.get('monthly'):
        parts.append(f'продажі ≈{_n(d["monthly"])} шт/міс')
    if d.get('months_left') is not None:
        parts.append(f'вистачить на {_n(d["months_left"])} міс')
    if d.get('below_reorder_point'):
        parts.append('доступно ≤ точки дозамовлення')
    if d.get('incoming'):
        parts.append(f'уже замовлено {_n(d["incoming"])}')
    return '; '.join(parts)


def _stock_line(d) -> str:
    status = d.get('status') or ''
    line = (f'• {d["sku"]} — {one_line(d.get("name"), 60) or "—"} · на складі {_n(d["on_hand"])} · резерв '
            f'{_n(d["reserved"])} · доступно {_n(d["available"])} · очікується {_n(d["incoming"])} · точка '
            f'{d.get("reorder_point") or 0} · {STATUS.get(status, status)}')
    if d.get('reorder_qty'):
        line += f' · замовити {d["reorder_qty"]}'
    reason = _reason(d)
    return line + (f' — {reason}' if reason else '')


def _sorted(rows) -> list:
    return sorted(rows, key=lambda d: (SEVERITY.get(d.get('status'), 9),
                                       d['months_left'] if d.get('months_left') is not None else 99, d['sku']))


def stock_report(ctx, args) -> str:
    from inventory.models import InventorySettings
    from inventory.services import stock
    require(ctx.task.owner, STOCK_PERM)
    mode = str(args.get('mode') or 'low').strip().lower()
    if mode not in ('low', 'reorder', 'all'):
        raise _error('mode — low, reorder або all')
    limit = clamp(args.get('limit'), 30, 1, 80)
    base = _products(ctx, args)
    note = ''
    if mode == 'low':
        if InventorySettings.get().low_stock_alert_enabled:
            wanted = set(base.values_list('pk', flat=True))
            rows = [d for d in stock.low_stock_items() if d['pk'] in wanted]
        else:
            rows = _sorted(d for d in _advice(base) if d['status'] in ('critical', 'warning'))
            note = (' (попередження про низький залишок у налаштуваннях складу вимкнено — пораховано за тими ж '
                    'правилами)')
        head = f'Пора дозамовити: {len(rows)}{note}'
    elif mode == 'reorder':
        rows = _sorted(d for d in _advice(base) if d['reorder_qty'] > 0)
        head = (f'Що замовити: {len(rows)} (ціль — max(3 міс продажів × 1,2; точка дозамовлення) мінус доступно й '
                'уже замовлене)')
    else:
        from inventory.models import Product
        total = base.count()
        pks = list(base.order_by('sku').values_list('pk', flat=True)[:limit])
        rows = sorted(_advice(Product.objects.filter(pk__in=pks)), key=lambda d: d['sku'])
        head = f'Активних товарів: {total}' + (f', показано {len(rows)}' if total > len(rows) else '')
    if not rows:
        return head + ' — нічого не знайдено'
    shown = rows[:limit]
    if mode != 'all' and len(rows) > len(shown):
        head += f', показано {len(shown)}'
    return _fit(head, [_stock_line(d) for d in shown])


# ── Рухи складу ──────────────────────────────────────────────────────────────

def _move_ref(key) -> str:
    """Ключ руху → людською мовою: замовлення, скасування, BOM, агент; інакше — сам ключ."""
    key = str(key or '')
    if key.startswith('so:'):
        parts = key.split(':')
        if len(parts) >= 3:
            sent = ' · відправлено посилкою' if ':sent:' in key else ''
            return f'замовлення {parts[2]} ({parts[1]}){sent}'
    if key.startswith('so-cancel:'):
        return 'повернення після скасування замовлення'
    if key.startswith('so-writeoff:'):
        return 'списання при скасуванні (без повернення)'
    if key.startswith('so-bom:') or key.startswith('bom_deduction_'):
        return 'компонент BOM замовлення'
    if key.startswith('agent:'):
        return 'з пропозиції агента'
    if key.startswith('po:') and len(key.split(':')) >= 2:
        return f'закупівля {key.split(":")[1]}'
    for prefix, text in (('manual:', 'вручну'), ('api:', 'через API'), ('excel:', 'з Excel')):
        if key.startswith(prefix):
            return text
    return 'ключ ' + one_line(key, 60) if key else ''


def stock_movements(ctx, args) -> str:
    from inventory.models import InventoryTransaction, Product
    from inventory.services.stock import annotate_stock
    require(ctx.task.owner, MOVES_PERM)
    product = ctx.product(args.get('sku'))
    days = clamp(args.get('days'), 90, 1, 730)
    limit = clamp(args.get('limit'), 30, 1, 100)
    qs = (InventoryTransaction.objects.filter(product=product,
                                              tx_date__gte=timezone.now() - datetime.timedelta(days=days))
          .select_related('location').order_by('-tx_date', '-pk'))
    total = qs.count()
    now = annotate_stock(Product.objects.filter(pk=product.pk)).first()
    head = (f'{product.sku} — {one_line(product.name, 60) or "—"}: зараз на складі {_n(now._on_hand)}, резерв '
            f'{_n(now._reserved)}, доступно {_n(now._available)}. Рухів за {days} дн.: {total}'
            + (f', показано {limit}' if total > limit else ''))
    if not total:
        return head
    labels = dict(InventoryTransaction.TxType.choices)
    lines = []
    for tx in qs[:limit]:
        qty = _n(tx.qty)
        qty = qty if qty.startswith('-') else f'+{qty}'
        parts = [date(tx.tx_date, '%d.%m.%Y %H:%M'), labels.get(tx.tx_type, tx.tx_type), qty,
                 tx.location.code if tx.location_id else '—']
        if tx.ref_doc:
            parts.append(one_line(tx.ref_doc, 80))
        ref = _move_ref(tx.external_key)
        if ref:
            parts.append(ref)
        lines.append('• ' + ' · '.join(parts))
    return _fit(head, lines)


# ── Angebote ─────────────────────────────────────────────────────────────────

def find_offers(ctx, args) -> str:
    from django.db.models import Count
    if not _offers_installed():
        raise _error('розділу Angebote в цій Minerva немає')
    from offers.models import Offer
    require(ctx.task.owner, OFFERS_PERM)
    limit = clamp(args.get('limit'), 20, 1, 50)
    days = clamp(args.get('days'), 180, 1, 3650)
    qs = Offer.objects.all()
    query = ' '.join(str(args.get('query') or '').split())[:100]
    by_number = bool(OFFER_NUMBER_RE.match(query))
    if query:
        qs = qs.filter(Q(number__icontains=query) | Q(customer__company__icontains=query)
                       | Q(customer__name__icontains=query) | Q(customer__email__icontains=query)
                       | Q(contact_name__icontains=query) | Q(contact_email__icontains=query)
                       | Q(lines__sku__icontains=query) | Q(project__code__icontains=query))
    status = str(args.get('status') or '').strip().lower()
    statuses = dict(Offer.STATUS_CHOICES)
    if status:
        if status not in statuses:
            raise _error('status — один з: ' + ', '.join(statuses))
        qs = qs.filter(status=status)
    if not by_number:
        qs = qs.filter(created_at__gte=timezone.now() - datetime.timedelta(days=days))
    found = list(Offer.objects.filter(pk__in=qs.values('pk')).select_related('customer', 'project')
                 .annotate(n_lines=Count('lines')).order_by('-created_at', '-pk')[:limit + 1])
    more = len(found) > limit
    period = '' if by_number else f' за {days} дн.'
    if not found:
        return f'Angebote{period} не знайдено. Нова чернетка — propose_offer.'
    lines = []
    for o in found[:limit]:
        who = ''
        if o.customer_id:
            who = ' · '.join(x for x in (o.customer.company, o.customer.name) if x)
        who = who or o.contact_name or o.contact_email or '—'
        line = (f'• {o.display_number} (версія {o.revision}) · {statuses.get(o.status, o.status)} · {one_line(who, 80)}'
                f' · від {date(o.created_at)} · дійсна до {date(o.valid_until)} · {money(o.net_total, o.currency)} '
                f'нетто · позицій {o.n_lines}')
        if o.project_id:
            line += f' · проєкт {o.project.code}'
        lines.append(line)
    head = f'Angebote{period}: {len(lines)}' + (' (є ще — звузьте запит)' if more else '') + \
        '. Нова чернетка — propose_offer.'
    return _fit(head, lines)


HANDLERS = {'stock_report': stock_report, 'stock_movements': stock_movements, 'find_offers': find_offers}
