"""Агент і закупівлі: посилка чи рахунок від постачальника в листі → закупівля (PO) з позиціями й трек-номер вхідної
посилки на сторінці «Відправлення» (📥 До нас). Агент лише читає й пропонує, застосовує людина (✓).

Інструменти (agent/tools.py бере звідси): find_purchase_orders (закупівлі й посилки — щоб не завести вдруге),
propose_purchase_order (постачальник, позиції рівно з документа, трек-номер і перевізник), propose_incoming_parcel
(трек-номер для наявної закупівлі). Читати — лише людині з `inventory.view_purchaseorder`.
Застосування (agent/decide.py `apply_one`): `apply_purchase` — постачальник `purchases.find_supplier` (новий —
з `inventory.add_supplier`), закупівля «Ordered» + позиції тим самим шляхом, що API закупівель
(`purchases.upsert(po=…)`), посилка — `shipping.services.incoming.create`; `apply_parcel` — посилка для закупівлі.
Той самий трек-номер або номер підтвердження постачальника вже є — не створюється вдруге. Прихід на склад — коли
посилка прибула: людина оприбутковує закупівлю.
"""
import json
from decimal import Decimal, InvalidOperation

from agent.models import AgentProposal

_EVIDENCE = {'type': 'string', 'description': 'Чому: цитата з листа чи документа'}
_SOURCE = {'type': 'string', 'description': 'Звідки: тема й дата листа, назва вкладення'}
LINES_MAX = 60
LIST_LIMIT = 30

FIND = {'name': 'find_purchase_orders', 'description': 'Закупівлі (PO) у постачальників: код, постачальник, стан, '
        'номер підтвердження постачальника, позиції (замовлено / отримано), посилки з трек-номерами. Шукає за '
        'постачальником, кодом PO, номером підтвердження, артикулом або трек-номером. Перед propose_purchase_order — '
        'перевір, чи такої закупівлі чи трек-номера ще немає.',
        'input_schema': {'type': 'object', 'properties': {
            'query': {'type': 'string', 'description': 'Постачальник, PO-12, номер підтвердження, артикул або '
                                                        'трек-номер; порожньо — останні відкриті'},
            'open_only': {'type': 'boolean', 'description': 'Лише ще не отримані й не скасовані'}}}}
PROPOSE_PO = {'name': 'propose_purchase_order', 'description': 'Запропонувати нову ЗАКУПІВЛЮ (PO) у постачальника '
              '(людина підтвердить ✓): постачальник, позиції рівно з документа (артикул складу, кількість, ціна за '
              'одиницю з інвойсу, якщо є), номер замовлення / підтвердження постачальника, дати, валюта; якщо в '
              'листі є трек-номер — він стане вхідною посилкою «📥 До нас» на сторінці «Відправлення» (статус '
              'оновлюватиметься сам). На склад товар іде пізніше, коли посилка прибуде.',
              'input_schema': {'type': 'object', 'properties': {
                  'supplier': {'type': 'object', 'description': 'Постачальник: назва фірми (не людини, що переслала '
                               'лист), email, країна ISO-2', 'properties': {
                                   'name': {'type': 'string'}, 'email': {'type': 'string'},
                                   'country': {'type': 'string'}}, 'required': ['name']},
                  'supplier_order_no': {'type': 'string', 'description': 'Номер замовлення / інвойсу постачальника'},
                  'order_date': {'type': 'string', 'description': 'YYYY-MM-DD'},
                  'expected_date': {'type': 'string', 'description': 'YYYY-MM-DD — очікувана доставка'},
                  'currency': {'type': 'string', 'description': 'EUR, USD…'},
                  'lines': {'type': 'array', 'items': {'type': 'object', 'properties': {
                      'sku': {'type': 'string'}, 'qty': {'type': 'number'},
                      'unit_price': {'type': 'number'}, 'description': {'type': 'string'}},
                      'required': ['sku', 'qty']}},
                  'tracking_number': {'type': 'string'},
                  'carrier': {'type': 'string', 'description': 'UPS, DHL, FedEx… (порожньо — за номером)'},
                  'notes': {'type': 'string'}, 'evidence': _EVIDENCE, 'source': _SOURCE},
                  'required': ['supplier', 'lines', 'evidence', 'source']}}
PROPOSE_PARCEL = {'name': 'propose_incoming_parcel', 'description': 'Запропонувати трек-номер вхідної посилки для '
                  'НАЯВНОЇ закупівлі (людина підтвердить ✓): посилка «📥 До нас» на сторінці «Відправлення», статус '
                  'оновлюватиметься сам.',
                  'input_schema': {'type': 'object', 'properties': {
                      'purchase_order': {'type': 'string', 'description': 'Код закупівлі, напр. PO-12'},
                      'tracking_number': {'type': 'string'}, 'carrier': {'type': 'string'},
                      'expected_date': {'type': 'string', 'description': 'YYYY-MM-DD'},
                      'evidence': _EVIDENCE, 'source': _SOURCE},
                      'required': ['purchase_order', 'tracking_number', 'evidence', 'source']}}
TOOLS = [FIND, PROPOSE_PO, PROPOSE_PARCEL]


def _error(text):
    from agent.tools import ToolError
    return ToolError(text)


def can_read(user) -> bool:
    return bool(user is not None and getattr(user, 'is_active', False)
                and user.has_perm('inventory.view_purchaseorder'))


def _check(ctx):
    if not can_read(getattr(ctx.task, 'owner', None)):
        raise _error('немає права бачити закупівлі (Склад)')


def _n(value) -> str:
    try:
        return format(Decimal(str(value)).normalize(), 'f')
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


def _date(value, field) -> str:
    from datetime import date
    value = str(value or '').strip()
    if not value:
        return ''
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        raise _error(f'{field}: дата РРРР-ММ-ДД') from None


def _po_row(po) -> dict:
    return {'code': po.code, 'supplier': po.supplier.name if po.supplier_id else '',
            'status': po.get_status_display(), 'order_date': po.order_date.isoformat() if po.order_date else '',
            'expected': po.expected_date.isoformat() if po.expected_date else '',
            'supplier_order_no': po.supplier_order_no, 'tracking': po.tracking_number,
            'lines': [f'{(l.product.sku if l.product else l.external_sku) or l.description} × {_n(l.qty_ordered)}'
                      + (f' (отримано {_n(l.qty_received)})' if l.qty_received else '') for l in po.lines.all()[:20]],
            'parcels': [f'{s.tracking_number} — {s.get_status_display()}' for s in po.parcels.all()[:10]]
                       + [f'{s.tracking_number} — {s.get_status_display()}' for s in po.incoming_shipments.all()[:10]]}


def find_purchase_orders(ctx, args):
    from django.db.models import Q
    from inventory.models import PurchaseOrder
    _check(ctx)
    q = str(args.get('query') or '').strip()
    qs = PurchaseOrder.objects.select_related('supplier').prefetch_related('lines__product', 'parcels',
                                                                            'incoming_shipments')
    if args.get('open_only'):
        qs = qs.exclude(status__in=(PurchaseOrder.Status.RECEIVED, PurchaseOrder.Status.CANCELLED))
    if q:
        qs = qs.filter(Q(code__iexact=q) | Q(supplier__name__icontains=q) | Q(supplier__email__icontains=q)
                       | Q(supplier_order_no__icontains=q) | Q(tracking_number__iexact=q)
                       | Q(lines__product__sku__iexact=q) | Q(lines__external_sku__iexact=q)
                       | Q(parcels__tracking_number__iexact=q) | Q(incoming_shipments__tracking_number__iexact=q)
                       ).distinct()
    rows = [_po_row(po) for po in qs.order_by('-order_date', '-pk')[:LIST_LIMIT]]
    out = {'found': len(rows), 'purchase_orders': rows}
    if q:
        from shipping.services import incoming
        parcel = incoming.find(q)
        if parcel is not None:
            out['parcel_with_this_tracking'] = {'id': parcel.pk, 'direction': parcel.get_direction_display(),
                                                'status': parcel.get_status_display(),
                                                'purchase_order': str(parcel.purchase_order or '')}
    return json.dumps(out, ensure_ascii=False)


def _tracking_taken(number) -> str:
    """'' або чому трек-номер уже є в Minerva."""
    from inventory.models import IncomingShipment
    from shipping.services import incoming
    number = str(number or '').strip()
    if not number:
        return ''
    s = incoming.find(number)
    if s is not None:
        return f'трек-номер {number} уже є — посилка #{s.pk}' + (f' ({s.purchase_order})' if s.purchase_order_id else '')
    old = IncomingShipment.objects.filter(tracking_number__iexact=number).select_related('purchase_order').first()
    if old is not None:
        return f'трек-номер {number} уже є в «Трекінгу приходу»' + (f' ({old.purchase_order})' if old.purchase_order_id else '')
    return ''


def _supplier_order_taken(name, number) -> str:
    from inventory.models import PurchaseOrder
    number = str(number or '').strip()
    if not number:
        return ''
    po = PurchaseOrder.objects.filter(supplier__name__iexact=str(name or '').strip(),
                                      supplier_order_no__iexact=number).first()
    return f'закупівля з номером {number} у цього постачальника вже є — {po.code}' if po else ''


def _new_sku(ctx, sku) -> bool:
    return AgentProposal.objects.filter(task=ctx.task, kind=AgentProposal.PRODUCT, data__sku=sku).exists()


def _lines(ctx, items) -> list:
    from inventory.services.stock import resolve_sku
    if not isinstance(items, list) or not items:
        raise _error('потрібні позиції (lines)')
    if len(items) > LINES_MAX:
        raise _error(f'забагато позицій (до {LINES_MAX})')
    out, seen = [], set()
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise _error(f'позиція {i}: очікується об’єкт')
        sku = str(item.get('sku') or '').strip()
        try:
            qty = Decimal(str(item.get('qty')))
        except (InvalidOperation, TypeError, ValueError):
            raise _error(f'позиція {i}: кількість — число') from None
        if not sku or qty <= 0:
            raise _error(f'позиція {i}: потрібні артикул і кількість більша за 0')
        product = resolve_sku(sku)
        if product is None and not _new_sku(ctx, sku):
            raise _error(f'артикула «{sku}» немає на складі — якщо це нова продукція, спершу propose_product')
        real = product.sku if product is not None else sku
        if real.lower() in seen:
            raise _error(f'артикул {real} двічі — об’єднай кількості в одну позицію')
        seen.add(real.lower())
        row = {'sku': real, 'qty': _n(qty)}
        if item.get('unit_price') not in (None, ''):
            try:
                row['unit_price'] = _n(Decimal(str(item['unit_price'])))
            except (InvalidOperation, TypeError, ValueError):
                raise _error(f'позиція {i}: ціна — число') from None
        if item.get('description'):
            row['description'] = str(item['description'])[:255]
        out.append(row)
    return out


def _carrier_name(number, wanted) -> str:
    from shipping.services import incoming
    try:
        return incoming.carrier_for(number, wanted).name
    except incoming.NoCarrier as e:
        raise _error(str(e)) from None


def _save(ctx, kind, field, new, data, evidence, source):
    if not str(evidence or '').strip():
        raise _error('потрібна підстава (цитата)')
    AgentProposal.objects.update_or_create(
        task=ctx.task, product=None, kind=kind, field=str(field)[:200], status=AgentProposal.PENDING,
        defaults={'old_value': '', 'new_value': new[:2000], 'data': data, 'evidence': str(evidence)[:2000],
                  'source': str(source or '')[:500]})
    ctx.proposed += 1


def propose_purchase_order(ctx, args):
    _check(ctx)
    sup = args.get('supplier') if isinstance(args.get('supplier'), dict) else {'name': args.get('supplier')}
    name = str(sup.get('name') or '').strip()[:255]
    if not name:
        raise _error('потрібна назва постачальника (фірма, від якої посилка чи рахунок)')
    lines = _lines(ctx, args.get('lines'))
    number = str(args.get('tracking_number') or '').strip()[:200]
    taken = _tracking_taken(number) or _supplier_order_taken(name, args.get('supplier_order_no'))
    if taken:
        raise _error(taken + ' — нову закупівлю не пропоную')
    data = {'supplier': {'name': name, 'email': str(sup.get('email') or '').strip()[:254],
                         'country': str(sup.get('country') or '').strip().upper()[:2]},
            'supplier_order_no': str(args.get('supplier_order_no') or '').strip()[:64],
            'order_date': _date(args.get('order_date'), 'order_date'),
            'expected_date': _date(args.get('expected_date'), 'expected_date'),
            'currency': str(args.get('currency') or '').strip().upper()[:3],
            'notes': str(args.get('notes') or '').strip()[:2000], 'lines': lines,
            'tracking_number': number, 'carrier': str(args.get('carrier') or '').strip()[:50]}
    if number:
        data['carrier_name'] = _carrier_name(number, data['carrier'])
    total = sum(Decimal(l['qty']) for l in lines)
    new = f'{name}: {len(lines)} поз., {_n(total)} шт' + (f' · {data["carrier_name"]} {number}' if number else '')
    _save(ctx, AgentProposal.PURCHASE, name + (f' {data["supplier_order_no"]}' if data['supplier_order_no'] else ''),
          new, data, args.get('evidence'), args.get('source'))
    return (f'Записано на перевірку: закупівля в {name} — {len(lines)} позицій'
            + (f', посилка {number}' if number else '') + '.')


def propose_incoming_parcel(ctx, args):
    from inventory.models import PurchaseOrder
    _check(ctx)
    code = str(args.get('purchase_order') or '').strip()
    po = PurchaseOrder.objects.select_related('supplier').filter(code__iexact=code).first()
    if po is None:
        raise _error(f'закупівлі «{code}» немає — find_purchase_orders')
    if po.status == PurchaseOrder.Status.CANCELLED:
        raise _error(f'закупівлю {po.code} скасовано')
    number = str(args.get('tracking_number') or '').strip()[:200]
    if not number:
        raise _error('потрібен трек-номер')
    taken = _tracking_taken(number)
    if taken:
        raise _error(taken)
    carrier = _carrier_name(number, args.get('carrier'))
    data = {'purchase_order': po.pk, 'code': po.code, 'tracking_number': number,
            'carrier': str(args.get('carrier') or '').strip()[:50], 'carrier_name': carrier,
            'expected_date': _date(args.get('expected_date'), 'expected_date')}
    _save(ctx, AgentProposal.PARCEL, number, f'{carrier} {number} → {po.code}', data, args.get('evidence'),
          args.get('source'))
    return f'Записано на перевірку: посилка {number} для {po.code}.'


HANDLERS = {'find_purchase_orders': find_purchase_orders, 'propose_purchase_order': propose_purchase_order,
            'propose_incoming_parcel': propose_incoming_parcel}


# ── Застосування (✓ людини) ──────────────────────────────────────────────────

def _parcel(po, number, carrier_wanted, user):
    from shipping.services import incoming
    if not number:
        return None, ''
    if not user.has_perm('shipping.add_shipment'):
        return None, 'посилку не додано: немає права створювати відправлення'
    taken = _tracking_taken(number)
    if taken:
        return None, taken
    try:
        return incoming.create(number, carrier=incoming.carrier_for(number, carrier_wanted), purchase_order=po), ''
    except (incoming.Duplicate, incoming.NoCarrier) as e:
        return None, str(e)


def _attach_mail(po, p, user) -> int:
    """Документи-вкладення листа завдання (лише своя скринька) → «📎 Документи» закупівлі
    (inventory/services/po_documents.py), категорія — за назвою файлу. Збій не скасовує закупівлю."""
    try:
        from agent.tools import mail_of
        from inventory.services import po_documents
        return po_documents.attach_message(po, mail_of(p.task), user)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning('agent po %s documents: %s', po.pk, e)
        return 0


def apply_purchase(p, user) -> str:
    from django.db import transaction
    from django.urls import reverse
    from core.models import AuditLog
    from inventory.models import PurchaseOrder, Supplier
    from inventory.services import purchases
    if not user.has_perm('inventory.add_purchaseorder'):
        return 'немає права створювати закупівлі'
    d = p.data or {}
    sup = d.get('supplier') or {}
    if not Supplier.objects.filter(name__iexact=sup.get('name', '')).exists() and \
            not (sup.get('email') and Supplier.objects.filter(email__iexact=sup['email']).exists()) and \
            not user.has_perm('inventory.add_supplier'):
        return f'постачальника «{sup.get("name")}» ще немає, а створювати постачальників немає права'
    taken = _tracking_taken(d.get('tracking_number')) or _supplier_order_taken(sup.get('name'), d.get('supplier_order_no'))
    if taken:
        return taken
    header = {k: d[k] for k in ('supplier_order_no', 'order_date', 'expected_date', 'currency', 'notes') if d.get(k)}
    header['lines'] = [{k: v for k, v in line.items() if k in ('sku', 'qty', 'unit_price', 'description')}
                       for line in d.get('lines') or []]
    try:
        with transaction.atomic():
            supplier = purchases.find_supplier(sup)
            po = PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.ORDERED,
                                              currency=d.get('currency') or supplier.currency or 'EUR')
            po, _created, warnings = purchases.upsert(header, source='', po=po)
    except purchases.PurchaseError as e:
        return str(e)
    AuditLog.log(AuditLog.Action.CREATE, module='inventory', model_name='PurchaseOrder', object_id=po.pk,
                 object_repr=f'{po.code} {supplier.name}', user=user, extra={'via': 'agent', 'task': p.task_id})
    p.result_url = reverse('admin:inventory_purchaseorder_change', args=[po.pk])
    _attach_mail(po, p, user)                       # рахунок, Lieferschein … з листа → «📎 Документи»
    _shipment, why = _parcel(po, d.get('tracking_number', ''), d.get('carrier', ''), user)
    notes = [w.get('message', '') for w in warnings if isinstance(w, dict)] + ([why] if why else [])
    notes = [n for n in notes if n]
    if notes:                      # закупівлю створено, але щось не вийшло — видно в її нотатках
        po.notes = ((po.notes + '\n') if po.notes else '') + '⚠️ 🤖 агент: ' + '; '.join(notes)
        PurchaseOrder.objects.filter(pk=po.pk).update(notes=po.notes[:4000])
    return ''


def apply_parcel(p, user) -> str:
    from django.urls import reverse
    from inventory.models import PurchaseOrder
    d = p.data or {}
    po = PurchaseOrder.objects.filter(pk=d.get('purchase_order')).first()
    if po is None:
        return 'закупівлі вже немає'
    if po.status == PurchaseOrder.Status.CANCELLED:
        return 'закупівлю скасовано'
    shipment, why = _parcel(po, d.get('tracking_number', ''), d.get('carrier', ''), user)
    if shipment is None:
        return why or 'посилку не додано'
    p.result_url = reverse('admin:shipping_shipment_detail', args=[shipment.pk])
    _attach_mail(po, p, user)
    return ''


def describe(p) -> tuple:
    d = p.data or {}
    if p.kind == AgentProposal.PARCEL:
        return '📥 Посилка до нас', '', p.new_value
    lines = ', '.join(f'{l.get("sku")} × {l.get("qty")}' for l in (d.get('lines') or [])[:6])
    more = ' …' if len(d.get('lines') or []) > 6 else ''
    extra = f' · № {d["supplier_order_no"]}' if d.get('supplier_order_no') else ''
    return '🛒 Закупівля', '', f'{p.new_value}{extra} — {lines}{more}'
