"""Агент і відправлення: що треба відправити, замовлення, перевізники — читає; пропонує ЧЕРНЕТКУ відправлення й зміни
замовлення. Застосовує людина (✓), оформлення в перевізника — лише людина.

Інструменти (agent/tools.py бере звідси): orders_to_ship (`sales.revenue.to_ship`), get_order (позиції зі складом —
`stock.order_lines_cover`, адреса — `Shipment.copy_from_order`, упаковка — та сама, що форма «Нове відправлення»,
проблеми перед відправкою), list_carriers, propose_shipment, propose_order_update (трек-номер, кур'єр, статус, строк
відправки). Читати замовлення можна лише людині з `sales.view_salesorder` (власник завдання / людина ключа MCP).
Застосування (agent/decide.py `apply_one`): чернетка — `build_draft` як `ShipmentAdmin.create_from_order_view`
(отримувач, відправник з перевізника, упаковка й вага `_fill_packaging_from_order`, опис, вартість, митниця поза ЄС,
коробки), право shipping.add_shipment, у замовлення вже є відправлення — ні; далі людина на сторінці чернетки перевіряє
й оформлює (UPS / DHL / FedEx / Jumingo — POST і один раз, shipping/admin.py). Зміна замовлення — та сама швидка
зміна, що в панелі праворуч (`core.panel_edit.apply_edit`: права картки, сигнали складу й CRM, журнал; значення
змінилось після пропозиції — ні).
"""
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from agent.models import AgentProposal

_ORDER = {'type': 'string', 'description': 'Номер замовлення'}
_ORDER_SOURCE = {'type': 'string', 'description': 'Джерело замовлення (digikey, webshop…) — лише якщо номер не '
                 'однозначний'}
_EVIDENCE = {'type': 'string', 'description': 'Чому: цитата з завдання, листа чи даних'}
LIST_LIMIT = 60
BLOCKING = ('draft', 'submitted', 'label_ready', 'in_transit', 'delivered')   # такі відправлення — нове не потрібне
# поле пропозиції → швидка зміна панелі праворуч (sales/side_panel.py)
ORDER_FIELDS = {'tracking': ('tracking', 'tracking_number', 'Трек-номер'),
                'courier': ('courier', 'shipping_courier', 'Кур’єр'),
                'status': ('status', 'status', 'Статус'),
                'deadline': ('deadline', 'shipping_deadline', 'Строк відправки')}

ORDERS_TO_SHIP = {'name': 'orders_to_ship', 'description': 'Замовлення, що чекають відправки (не відправлені й не '
                  'скасовані): номер, джерело, клієнт, країна, строк відправки (overdue — прострочено), статус, '
                  'трек-номер, відправлення, проблеми (бракує складу, немає адреси…). Фільтри необов\'язкові.',
                  'input_schema': {'type': 'object', 'properties': {
                      'order_source': {'type': 'string', 'description': 'Лише це джерело'},
                      'due_days': {'type': 'integer', 'minimum': 0, 'maximum': 90,
                                   'description': 'Строк відправки не пізніше ніж через N днів (0 — сьогодні)'},
                      'overdue_only': {'type': 'boolean'}}}}
GET_ORDER = {'name': 'get_order', 'description': 'Замовлення повністю: статус, дати, строк відправки, адреса доставки, '
             'позиції зі складом (бракує?), трек-номер і кур\'єр, відправлення, рекомендована упаковка й вага, чи '
             'потрібна митниця, проблеми перед відправкою.',
             'input_schema': {'type': 'object', 'properties': {'order': _ORDER, 'order_source': _ORDER_SOURCE},
                              'required': ['order']}}
CARRIERS = {'name': 'list_carriers', 'description': 'Активні перевізники Minerva (UPS, DHL, FedEx, Jumingo…): id, назва, '
            'тип, чи за замовчуванням.', 'input_schema': {'type': 'object', 'properties': {}}}
PROPOSE_SHIPMENT = {'name': 'propose_shipment', 'description': 'Запропонувати ЧЕРНЕТКУ відправлення для замовлення '
                    '(людина підтвердить ✓). Minerva заповнить її як форма «Нове відправлення з замовлення»: '
                    'отримувач, відправник, упаковка й вага, митниця. Оформлення в перевізника, етикетку й оплату '
                    'робить людина. carrier — id чи назва з list_carriers (порожньо — за замовчуванням).',
                    'input_schema': {'type': 'object', 'properties': {
                        'order': _ORDER, 'order_source': _ORDER_SOURCE, 'carrier': {'type': 'string'},
                        'evidence': _EVIDENCE}, 'required': ['order', 'evidence']}}
PROPOSE_ORDER = {'name': 'propose_order_update', 'description': 'Запропонувати зміну замовлення (людина підтвердить ✓): '
                 'tracking — трек-номер, courier — перевізник / служба, status — received / processing / shipped / '
                 'delivered / cancelled, deadline — строк відправки YYYY-MM-DD. Підстава — цитата з листа чи завдання.',
                 'input_schema': {'type': 'object', 'properties': {
                     'order': _ORDER, 'order_source': _ORDER_SOURCE,
                     'field': {'type': 'string', 'enum': list(ORDER_FIELDS)}, 'value': {'type': 'string'},
                     'evidence': _EVIDENCE,
                     'source': {'type': 'string', 'description': 'Звідки: лист (тема, дата) чи завдання'}},
                     'required': ['order', 'field', 'value', 'evidence']}}
READ_TOOLS = [ORDERS_TO_SHIP, GET_ORDER, CARRIERS]
TOOLS = READ_TOOLS + [PROPOSE_SHIPMENT, PROPOSE_ORDER]


def _error(text):
    from agent.tools import ToolError
    return ToolError(text)


def can_read(user) -> bool:
    return bool(user is not None and getattr(user, 'is_active', False) and user.has_perm('sales.view_salesorder'))


def _check(ctx):
    if not can_read(getattr(ctx.task, 'owner', None)):
        raise _error('немає права бачити замовлення (Продажі)')


def _order(ctx, number, order_source=''):
    from sales.models import SalesOrder
    _check(ctx)
    number = str(number or '').strip().lstrip('#')
    if not number:
        raise _error('потрібен номер замовлення')
    qs = SalesOrder.objects.filter(order_number__iexact=number)
    if str(order_source or '').strip():
        qs = qs.filter(source__iexact=str(order_source).strip())
    found = list(qs.order_by('-pk')[:5])
    if not found:
        raise _error(f'замовлення «{number}» немає в Minerva')
    if len(found) > 1:
        raise _error(f'кілька замовлень з номером {number}: джерела {", ".join(o.source for o in found)} — вкажіть '
                     f'order_source')
    return found[0]


def _carrier(value):
    from shipping.models import Carrier
    qs = Carrier.objects.filter(is_active=True)
    value = str(value or '').strip()
    if not value:
        carrier = qs.order_by('-is_default', 'name').first()
        if carrier is None:
            raise _error('немає активного перевізника — «Доставка → Перевізники»')
        return carrier
    carrier = (qs.filter(pk=int(value)).first() if value.isdigit() else None) or qs.filter(name__iexact=value).first() \
        or qs.filter(carrier_type__iexact=value).order_by('-is_default').first()
    if carrier is None:
        raise _error(f'перевізника «{value}» немає серед активних — див. list_carriers')
    return carrier


def _blocking(order) -> list:
    return list(order.shipments.filter(status__in=BLOCKING).select_related('carrier').order_by('-pk'))


def _eu() -> set:
    from shipping.services.jumingo import JumingoService
    return set(JumingoService._EU_COUNTRIES)


def _country(order) -> str:
    from labels_app.order_label import country
    return country(order)


def _ship_to(order):
    from shipping.models import Shipment
    s = Shipment(order=order)
    s.copy_from_order()
    return s


def _packaging(shipment) -> dict:
    """Та сама упаковка й вага, що форма «Нове відправлення з замовлення» (OrderPackaging → історія → товари)."""
    from django.contrib import admin
    from shipping.models import Shipment
    ma = admin.site._registry.get(Shipment)
    try:
        return (ma._fill_packaging_from_order(shipment) if ma is not None else None) or {}
    except Exception:  # noqa: BLE001 — без підказки упаковки чернетка все одно можлива
        return {}


def _days(d):
    return (d - timezone.localdate()).days if isinstance(d, date) else None


def problems(order, cover=None, shipments=None) -> list:
    """Що завадить відправити: склад, адреса, телефон, митниця, наявне відправлення, статус."""
    out = []
    if order.status == 'cancelled':
        out.append('замовлення скасовано')
    if order.status in ('shipped', 'delivered') or order.shipped_at:
        out.append('вже відправлено')
    for s in shipments if shipments is not None else _blocking(order):
        out.append(f'вже є відправлення #{s.pk} ({s.get_status_display()})')
    if not (order.addr_street or order.addr_city or (order.shipping_address or '').strip()):
        out.append('немає адреси доставки')
    code = _country(order)
    if not code:
        out.append('не визначено країну доставки')
    if not (order.ship_phone or order.phone):
        out.append('немає телефону отримувача (UPS / DHL для міжнародних відправлень вимагають)')
    lines = list(order.lines.select_related('product'))
    cover = cover if cover is not None else _cover(order)
    for line in lines:
        c = cover.get(line.pk)
        if line.product_id is None:
            out.append(f'позиція {line.sku_raw or "?"} без товару складу')
        elif c is not None and c['free'] < (line.qty or 0):
            out.append(f'бракує {line.product.sku}: потрібно {_n(line.qty)}, вільно {_n(c["free"])}')
    if code and code not in _eu():
        no_hs = [l.product.sku for l in lines if l.product_id and not _hs(l.product)]
        if no_hs:
            out.append('поза ЄС — немає HS-коду для митниці: ' + ', '.join(no_hs[:10]))
    no_weight = [l.product.sku for l in lines if l.product_id and not l.product.net_weight_g]
    if no_weight:
        out.append('немає ваги 1 шт (вага відправлення буде приблизна): ' + ', '.join(no_weight[:10]))
    return out


def _hs(product) -> str:
    if product.hs_code:
        return product.hs_code
    from inventory.models import ProductCategory
    cat = ProductCategory.objects.filter(slug=product.category).only('customs_hs_code').first() \
        if product.category else None
    return getattr(cat, 'customs_hs_code', '') or ''


def _cover(order) -> dict:
    from inventory.services.stock import order_lines_cover
    try:
        return order_lines_cover(order)
    except Exception:  # noqa: BLE001
        return {}


def _n(value) -> str:
    try:
        return format(Decimal(value).normalize(), 'f')
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


def _shipment_row(s) -> dict:
    return {'id': s.pk, 'carrier': s.carrier.name if s.carrier_id else '', 'status': s.get_status_display(),
            'tracking': s.tracking_number or '', 'created': s.created_at.date().isoformat() if s.created_at else ''}


# ── Читання ──────────────────────────────────────────────────────────────────

def orders_to_ship(ctx, args):
    from sales.revenue import to_ship
    _check(ctx)
    qs = to_ship().prefetch_related('shipments__carrier')
    if str(args.get('order_source') or '').strip():
        qs = qs.filter(source__iexact=str(args['order_source']).strip())
    today = timezone.localdate()
    if args.get('overdue_only'):
        qs = qs.filter(shipping_deadline__lt=today)
    elif args.get('due_days') is not None:
        try:
            qs = qs.filter(shipping_deadline__lte=today + timedelta(days=int(args['due_days'])))
        except (TypeError, ValueError):
            raise _error('due_days — ціле число днів') from None
    total = qs.count()
    rows = []
    for o in qs.order_by('shipping_deadline', 'order_date', 'pk')[:LIST_LIMIT]:
        shipments = [s for s in o.shipments.all() if s.status in BLOCKING]
        left = _days(o.shipping_deadline)
        rows.append({'order': o.order_number, 'source': o.source, 'customer': o.client or o.contact_name or '',
                     'country': _country(o), 'status': o.get_status_display(),
                     'deadline': o.shipping_deadline.isoformat() if o.shipping_deadline else '',
                     'overdue': left is not None and left < 0, 'tracking': o.tracking_number or '',
                     'shipments': [_shipment_row(s) for s in shipments],
                     'problems': problems(o, shipments=shipments)})
    return json.dumps({'total': total, 'shown': len(rows), 'orders': rows}, ensure_ascii=False)


def get_order(ctx, args):
    o = _order(ctx, args.get('order'), args.get('order_source'))
    cover = _cover(o)
    lines = []
    for line in o.lines.select_related('product').order_by('pk'):
        c = cover.get(line.pk) or {}
        row = {'sku': line.product.sku if line.product_id else (line.sku_raw or ''),
               'name': line.product.name if line.product_id else '', 'qty': _n(line.qty)}
        if c:
            row['free_on_stock'] = _n(c['free'])
            row['short'] = c['free'] < (line.qty or 0)
        lines.append(row)
    to = _ship_to(o)
    pack = _packaging(to)
    code = _country(o)
    shipments = list(o.shipments.select_related('carrier').order_by('-pk')[:10])
    data = {
        'order': o.order_number, 'source': o.source, 'status': o.get_status_display(), 'status_code': o.status,
        'order_date': o.order_date.isoformat() if o.order_date else '',
        'deadline': o.shipping_deadline.isoformat() if o.shipping_deadline else '',
        'days_left': _days(o.shipping_deadline), 'shipped_at': o.shipped_at.isoformat() if o.shipped_at else '',
        'customer': o.client or '', 'tracking': o.tracking_number or '', 'courier': o.shipping_courier or '',
        'ship_to': {'name': to.recipient_name, 'company': to.recipient_company, 'street': to.recipient_street,
                    'zip': to.recipient_zip, 'city': to.recipient_city, 'state': to.recipient_state,
                    'country': code or to.recipient_country, 'phone': to.recipient_phone,
                    'email': to.recipient_email},
        'lines': lines,
        'shipments': [_shipment_row(s) for s in shipments],
        'packaging': {'boxes': pack.get('boxes') or ([str(pack['box'])] if pack.get('box') else []),
                      'weight_kg': _n(pack['weight_kg']) if pack.get('weight_kg') else '',
                      'source': pack.get('source', ''), 'missing_weight': bool(pack.get('missing_weight')),
                      'approx_weight': bool(pack.get('approx_weight'))},
        'customs_needed': bool(code) and code not in _eu(),
        'problems': problems(o, cover, [s for s in shipments if s.status in BLOCKING]),
    }
    return json.dumps(data, ensure_ascii=False, default=str)


def list_carriers(ctx, args):
    from shipping.models import Carrier
    rows = [{'id': c.pk, 'name': c.name, 'type': c.carrier_type, 'default': c.is_default}
            for c in Carrier.objects.filter(is_active=True).order_by('-is_default', 'name')]
    return json.dumps(rows, ensure_ascii=False) if rows else 'Активних перевізників немає'


# ── Пропозиції ───────────────────────────────────────────────────────────────

def _save(ctx, kind, field, old, new, data, evidence, source=''):
    if not str(evidence or '').strip():
        raise _error('потрібна підстава')
    AgentProposal.objects.update_or_create(
        task=ctx.task, product=None, kind=kind, field=field[:200], status=AgentProposal.PENDING,
        defaults={'old_value': old, 'new_value': new, 'data': data, 'evidence': str(evidence)[:2000],
                  'source': str(source or '')[:500]})
    ctx.proposed += 1


def _order_ref(o) -> dict:
    from django.urls import reverse
    return {'order': o.pk, 'number': o.order_number, 'order_source': o.source,
            'order_url': reverse('admin:sales_salesorder_change', args=[o.pk])}


def propose_shipment(ctx, args):
    o = _order(ctx, args.get('order'), args.get('order_source'))
    carrier = _carrier(args.get('carrier'))
    if o.status == 'cancelled':
        raise _error(f'замовлення {o.order_number} скасовано')
    existing = _blocking(o)
    if existing:
        s = existing[0]
        raise _error(f'у замовлення {o.order_number} вже є відправлення #{s.pk} ({s.get_status_display()}) — '
                     f'нове не потрібне')
    to = _ship_to(o)
    pack = _packaging(to)
    code = _country(o)
    preview = {'to': ' · '.join(x for x in (to.recipient_company or to.recipient_name, to.recipient_city, code) if x),
               'weight_kg': _n(to.weight_kg) if to.weight_kg else '',
               'boxes': pack.get('boxes') or ([str(pack['box'])] if pack.get('box') else []),
               'customs': bool(code) and code not in _eu(), 'problems': problems(o, shipments=[])}
    data = {**_order_ref(o), 'carrier': carrier.pk, 'carrier_name': carrier.name, 'preview': preview}
    new = f'{carrier.name} → {preview["to"] or "?"}' + (f' · {preview["weight_kg"]} кг' if preview['weight_kg'] else '')
    _save(ctx, AgentProposal.SHIPMENT, o.order_number, '', new, data, args.get('evidence'))
    warn = f' Увага: {"; ".join(preview["problems"])}.' if preview['problems'] else ''
    return f'Записано на перевірку: чернетка відправлення {o.order_number} ({carrier.name}).{warn}'


def clean_order_value(field, value) -> str:
    value = str(value or '').strip()
    if field == 'status':
        from sales.models import SalesOrder
        codes = {c for c, _ in SalesOrder.STATUS_CHOICES}
        if value not in codes:
            raise _error('статус — один з: ' + ', '.join(sorted(codes)))
        return value
    if field == 'deadline':
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            raise _error('строк — дата YYYY-MM-DD') from None
    limit = 128 if field == 'tracking' else 64
    if not value or len(value) > limit:
        raise _error(f'значення — від 1 до {limit} символів')
    if field == 'tracking' and (' ' in value or not any(ch.isdigit() for ch in value)):
        raise _error('трек-номер — без пробілів і з цифрами')
    return value


def current(order, field) -> str:
    from core import side_panel as sp
    return sp.value_text(sp.field_value(order, ORDER_FIELDS[field][1]))


def propose_order_update(ctx, args):
    field = str(args.get('field') or '')
    if field not in ORDER_FIELDS:
        raise _error('field — tracking / courier / status / deadline')
    o = _order(ctx, args.get('order'), args.get('order_source'))
    value = clean_order_value(field, args.get('value'))
    old = current(o, field)
    if old == value:
        return f'У замовлення {o.order_number} уже так'
    data = {**_order_ref(o), 'field': field, 'value': value, 'old': old}
    _save(ctx, AgentProposal.ORDER, f'{o.order_number}:{field}', old, value, data, args.get('evidence'),
          args.get('source'))
    return f'Записано на перевірку: {o.order_number} · {ORDER_FIELDS[field][2]}: «{old}» → «{value}»'


HANDLERS = {'orders_to_ship': orders_to_ship, 'get_order': get_order, 'list_carriers': list_carriers,
            'propose_shipment': propose_shipment, 'propose_order_update': propose_order_update}


# ── Застосування (✓ людини) ──────────────────────────────────────────────────

def _sender(carrier) -> dict:
    return {'sender_name': carrier.sender_name or carrier.sender_company or '',
            'sender_company': carrier.sender_company or '', 'sender_street': carrier.sender_street or '',
            'sender_city': carrier.sender_city or '', 'sender_zip': carrier.sender_zip or '',
            'sender_country': (carrier.sender_country or 'DE').upper()[:2], 'sender_phone': carrier.sender_phone or '',
            'sender_email': carrier.sender_email or ''}


def build_draft(order, carrier, user):
    """Чернетка відправлення так, як її заповнює форма «Нове відправлення з замовлення» (ShipmentAdmin)."""
    from django.contrib import admin
    from shipping.models import Shipment, ShipmentPackage
    from shipping.services.jumingo import build_customs_articles
    s = Shipment(order=order, carrier=carrier, status=Shipment.Status.DRAFT, created_by=user)
    s.copy_from_order()
    for name, value in _sender(carrier).items():
        setattr(s, name, value)
    hint = _packaging(s)
    s.weight_kg = max(Decimal('0.1'), Decimal(s.weight_kg or 1))
    ma = admin.site._registry.get(Shipment)
    if not s.description and ma is not None:
        s.description = ma._build_description_from_order(order)
    try:
        total = order.order_total()
        if total:
            s.declared_value = Decimal(str(total)).quantize(Decimal('0.01'))
            s.declared_currency = (order.currency or 'EUR').upper()[:3]
    except (InvalidOperation, TypeError, ValueError):
        pass
    eu = _eu()
    to, frm = (s.recipient_country or '').upper(), (s.sender_country or '').upper()
    if (to and to not in eu) or (frm and frm not in eu):
        s.customs_articles = {'type': 'commercial', 'incoterm': 'DAP', 'customs_line_items': build_customs_articles(
            order, s.sender_country or 'DE', s.declared_currency or order.currency or 'EUR')}
    s.save()
    rows = hint.get('pkg_rows') or []
    if len(rows) > 1 or (rows and int(rows[0].get('quantity') or 1) > 1):
        for r in rows:
            ShipmentPackage.objects.create(
                shipment=s, weight_kg=max(Decimal('0.1'), Decimal(str(r.get('weight_kg') or 1))),
                length_cm=Decimal(str(r.get('length_cm') or 30)), width_cm=Decimal(str(r.get('width_cm') or 20)),
                height_cm=Decimal(str(r.get('height_cm') or 15)), quantity=max(1, int(r.get('quantity') or 1)))
    return s


def apply_shipment(p, user) -> str:
    from django.db import transaction
    from django.urls import reverse
    from core.models import AuditLog
    from sales.models import SalesOrder
    from shipping.models import Carrier
    if not user.has_perm('shipping.add_shipment'):
        return 'немає права створювати відправлення'
    d = p.data or {}
    with transaction.atomic():
        order = SalesOrder.objects.select_for_update().filter(pk=d.get('order')).first()
        if order is None:
            return 'замовлення вже немає'
        if order.status == 'cancelled':
            return 'замовлення скасовано'
        existing = _blocking(order)
        if existing:
            return f'вже є відправлення #{existing[0].pk} ({existing[0].get_status_display()})'
        carrier = Carrier.objects.filter(pk=d.get('carrier'), is_active=True).first()
        if carrier is None:
            return 'перевізник вимкнений або його вже немає'
        s = build_draft(order, carrier, user)
    AuditLog.log(AuditLog.Action.CREATE, module='shipping', model_name='Shipment', object_id=s.pk,
                 object_repr=f'#{s.pk} {order.order_number}', user=user,
                 extra={'via': 'agent', 'task': p.task_id, 'status': 'draft'})
    p.result_url = reverse('admin:shipping_shipment_edit_draft', args=[s.pk])
    return ''


def _request(user):
    """Запит для швидкої зміни, коли ✓ прийшов не зі сторінки (тести, MCP-сценарії)."""
    from django.contrib.messages.storage.cookie import CookieStorage
    from django.test import RequestFactory
    request = RequestFactory().post('/agent/')
    request.user = user
    request._messages = CookieStorage(request)
    return request


def apply_order(p, user, request=None) -> str:
    from django.contrib import admin
    from core import panel_edit
    from core import side_panel as sp
    from sales.models import SalesOrder
    d = p.data or {}
    field = d.get('field')
    if field not in ORDER_FIELDS:
        return 'невідома зміна'
    if not user.has_perm('sales.change_salesorder'):
        return 'немає права змінювати замовлення'
    panel, ma = sp.get('sales.salesorder'), admin.site._registry.get(SalesOrder)
    if panel is None or ma is None:
        return 'замовлення зараз змінити не можна'
    if request is None or getattr(request, 'user', None) != user:
        request = _request(user)
    try:
        status, payload = panel_edit.apply_edit(request, panel, ma, d.get('order'),
                                                {'key': ORDER_FIELDS[field][0], 'value': d.get('value'),
                                                 'expect': d.get('old', '')})
    except Exception as e:  # noqa: BLE001 — зміну відкочено всередині
        return f'не вдалося: {type(e).__name__}'
    if status == 409:
        order = SalesOrder.objects.filter(pk=d.get('order')).first()
        now = current(order, field) if order is not None else ''
        return f'змінилось після пропозиції (зараз «{now}») — запустіть завдання ще раз'
    if status != 200 or not payload.get('ok'):
        return payload.get('message') or 'не вдалося'
    p.result_url = d.get('order_url', '')
    return ''


def describe(p) -> tuple:
    d = p.data or {}
    if p.kind == AgentProposal.SHIPMENT:
        pv = d.get('preview') or {}
        extra = []
        if pv.get('boxes'):
            extra.append(', '.join(pv['boxes'][:3]))
        if pv.get('customs'):
            extra.append('митниця')
        if pv.get('problems'):
            extra.append('⚠️ ' + '; '.join(pv['problems'][:3]))
        return '🚚 Чернетка відправлення', '', p.new_value + (' · ' + ' · '.join(extra) if extra else '')
    label = ORDER_FIELDS.get(d.get('field'), ('', '', 'Замовлення'))[2]
    old, new = p.old_value, p.new_value
    if d.get('field') == 'status':
        from sales.models import SalesOrder
        names = dict(SalesOrder.STATUS_CHOICES)
        old, new = names.get(old, old), names.get(new, new)
    return f'🧾 {label}', old or '—', new
