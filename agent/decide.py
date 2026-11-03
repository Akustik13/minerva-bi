"""✓ / ✕ людини над пропозиціями агента (сторінка завдання, agent/admin.py).

Поля й параметри товару — agent/products.py `apply`; 🆕 новий товар (право add_product, артикул уже є — ні) — першим;
📦 прихід — `stock.create_movement` Incoming (ключ `agent:<pk пропозиції>` — двічі не прийде; право
add_inventorytransaction); 📅 подія — CalendarEvent людини (право add_calendarevent, лист завдання — якщо її скринька).
Лист — лише на адреси з тексту завдання (`tools.allowed_addresses`,
перевіряється й тут: людина могла дописати адресу в полі «Кому») зі скриньки людини: «📝 Чернетка» — EmailDraft
(відкривається в «✏️ Написати»), «✉️ Надіслати» — ScheduledEmail зараз (cron `send_scheduled`, до 2 хв, у
«⏳ Заплановані» ще можна скасувати); даташити — копією у вкладенні (`attachments.store_file`).
"""
from decimal import Decimal

from django.utils import timezone

from agent import products as product_fields
from agent.models import AgentProposal, AgentTask
from agent.tools import EMAIL_RE, ToolError, _datasheet_bytes, allowed_addresses


def apply_selected(task, ids, user, request=None) -> dict:
    """✓ вибраних: нові товари — першими (прихід нового артикула з того самого завдання знайде його)."""
    out = {'applied': 0, 'failed': []}
    chosen = task.proposals.filter(pk__in=ids, status=AgentProposal.PENDING).exclude(kind=AgentProposal.MAIL) \
        .select_related('product', 'task')
    for p in sorted(chosen, key=lambda x: (x.kind != AgentProposal.PRODUCT, x.pk)):
        error = apply_one(p, user, request)
        product_fields.decided(p, user, AgentProposal.FAILED if error else AgentProposal.APPLIED, error)
        if error:
            what, _old, new = describe(p)
            out['failed'].append(f'{what} {new}: {error}')
        else:
            out['applied'] += 1
    _settle(task)
    return out


def apply_one(p, user, request=None) -> str:
    from agent import channels, shipments
    if p.kind == AgentProposal.PRODUCT:
        return _create_product(p, user)
    if p.kind == AgentProposal.STOCK_IN:
        return _stock_in(p, user)
    if p.kind == AgentProposal.EVENT:
        return _event(p, user)
    if p.kind == AgentProposal.SHOP:
        return channels.apply_shop(p, user)
    if p.kind == AgentProposal.DK_PRICES:
        return channels.apply_dk_prices(p, user)
    if p.kind == AgentProposal.DK_LEAD:
        return channels.apply_dk_lead(p, user)
    if p.kind == AgentProposal.SHIPMENT:
        return shipments.apply_shipment(p, user)
    if p.kind == AgentProposal.ORDER:
        return shipments.apply_order(p, user, request)
    return product_fields.apply(p, user)


def describe(p) -> tuple:
    """(що, було, стане) — рядок таблиці на сторінці завдання."""
    from agent import channels, shipments
    if p.kind in (AgentProposal.SHOP, AgentProposal.DK_PRICES, AgentProposal.DK_LEAD):
        return channels.describe(p)
    if p.kind in (AgentProposal.SHIPMENT, AgentProposal.ORDER):
        return shipments.describe(p)
    d = p.data or {}
    if p.kind == AgentProposal.STOCK_IN:
        where = d.get('location') or 'локація за замовчуванням'
        ref = f' · {d["reference"]}' if d.get('reference') else ''
        return '📦 Прихід на склад', '', f'+{d.get("qty", p.new_value)} шт → {where}{ref}'
    if p.kind == AgentProposal.PRODUCT:
        maker = f' ({d["manufacturer"]})' if d.get('manufacturer') else ''
        return '🆕 Новий товар', '—', f'{d.get("sku", p.field)} — {d.get("name", p.new_value)}{maker}'
    if p.kind == AgentProposal.EVENT:
        kind = dict(EVENT_LABELS).get(d.get('type'), '📌 Подія')
        if d.get('all_day'):
            return kind, '', f'{d.get("title", p.field)} · {_local(d.get("start"), "%d.%m.%Y")} (весь день)'
        start = _local(d.get('start'))
        end = _local(d.get('end'), '%H:%M') if d.get('end') else ''
        return kind, '', f'{d.get("title", p.field)} · {start}' + (f'–{end}' if end else '')
    return product_fields.label(p), p.old_value or '—', p.new_value


EVENT_LABELS = [('meeting', '📅 Зустріч'), ('deadline', '📅 Дедлайн'), ('reminder', '📅 Нагадування'),
                ('other', '📅 Подія')]


def _local(value, fmt='%d.%m.%Y %H:%M') -> str:
    from django.utils.dateparse import parse_datetime
    when = parse_datetime(value or '')
    return timezone.localtime(when).strftime(fmt) if when else ''


def _create_product(p, user) -> str:
    from core.models import AuditLog
    from inventory.models import Product
    if not user.has_perm('inventory.add_product'):
        return 'немає права додавати товари'
    d = p.data or {}
    sku = d.get('sku') or p.field
    if Product.objects.filter(sku__iexact=sku).exists():
        return f'товар {sku} уже є'
    product = Product.objects.create(sku=sku, name=d.get('name') or p.new_value, manufacturer=d.get('manufacturer', ''))
    p.product = product
    p.save(update_fields=['product'])
    AuditLog.log(AuditLog.Action.CREATE, module='inventory', model_name='Product', object_id=product.pk,
                 object_repr=product.sku, user=user, extra={'via': 'agent', 'task': p.task_id})
    return ''


def _stock_in(p, user) -> str:
    from inventory.models import InventoryTransaction, Product
    from inventory.services import stock
    if not user.has_perm('inventory.add_inventorytransaction'):
        return 'немає права записувати рухи складу'
    d = p.data or {}
    product = p.product or Product.objects.filter(sku__iexact=d.get('sku') or p.field).first()
    if product is None:
        return f'товару {d.get("sku") or p.field} немає — спершу застосуйте «🆕 Новий товар»'
    ref = ' · '.join(x for x in (d.get('reference', ''), f'🤖 агент, завдання #{p.task_id}') if x)[:255]
    try:
        stock.create_movement(product=product, tx_type=InventoryTransaction.TxType.INCOMING,
                              qty=Decimal(str(d.get('qty') or p.new_value)),
                              location=stock.get_location(d.get('location') or None), ref_doc=ref,
                              external_key=f'agent:{p.pk}', performed_by=user)
    except stock.StockError as e:
        return str(e)
    return ''


def _event(p, user) -> str:
    from calendar_app.models import CalendarEvent
    from django.utils.dateparse import parse_datetime
    if not user.has_perm('calendar_app.add_calendarevent'):
        return 'немає права додавати події в календар'
    d = p.data or {}
    start = parse_datetime(d.get('start') or '')
    if start is None:
        return 'немає дати події'
    types = {'meeting': CalendarEvent.TYPE_MEETING, 'deadline': CalendarEvent.TYPE_DEADLINE,
             'reminder': CalendarEvent.TYPE_REMINDER}
    msg = p.task.message if p.task.message_id and p.task.message.account.user_id == user.pk else None
    note = '\n\n'.join(x for x in (d.get('description', ''), f'🤖 Агент, завдання #{p.task_id}: «{p.evidence[:300]}»'
                                   + (f' — {p.source}' if p.source else '')) if x)
    event = CalendarEvent.objects.create(
        user=user, title=d.get('title') or p.field, description=note, event_type=types.get(d.get('type'),
                                                                                           CalendarEvent.TYPE_OTHER),
        start_at=start, end_at=parse_datetime(d.get('end') or '') or None, all_day=bool(d.get('all_day')),
        email_message=msg)
    p.result_url = f'/admin/calendar_app/calendarevent/{event.pk}/change/'
    return ''


def reject_selected(task, ids, user) -> int:
    n = 0
    for p in task.proposals.filter(pk__in=ids, status=AgentProposal.PENDING):
        product_fields.decided(p, user, AgentProposal.REJECTED)
        n += 1
    _settle(task)
    return n


def _settle(task):
    """Усе вирішено — завдання «✅ Готово»."""
    if task.status == AgentTask.REVIEW and not task.proposals.filter(status=AgentProposal.PENDING).exists():
        AgentTask.objects.filter(pk=task.pk).update(status=AgentTask.DONE)


def mail(proposal, user, *, send=False, to='', subject='', body='') -> str:
    """Лист з пропозиції (можна з правками людини) → чернетка або відправка. '' — готово, інакше — чому ні."""
    from email_assistant import attachments
    from email_assistant.models import EmailAccount, EmailDraft, ScheduledEmail
    from inventory.models import Product
    account = EmailAccount.objects.filter(user=user, is_active=True).order_by('-is_primary').first()
    if account is None:
        return 'у вас немає скриньки в Minerva'
    addresses = [a.lower() for a in EMAIL_RE.findall(to or proposal.mail_to)]
    allowed = allowed_addresses(proposal.task)
    foreign = [a for a in addresses if a not in allowed]
    if not addresses or foreign:
        return (f'адреси {", ".join(foreign)} немає в тексті завдання' if foreign else 'кому?')
    subject = (subject or proposal.mail_subject).strip()[:500]
    body = (body or proposal.mail_body).strip()
    if not subject or not body:
        return 'потрібні тема й текст листа'
    items = []
    for sku in proposal.mail_attach or []:
        product = Product.objects.filter(sku=sku).first()
        if product is None:
            return f'товару {sku} вже немає'
        try:
            data, _source = _datasheet_bytes(product)
        except ToolError as e:
            return f'даташит {sku}: {e}'
        items.append(attachments.store_file(f'{sku}-datasheet.pdf', data, 'application/pdf'))
    if send:
        sched = ScheduledEmail.objects.create(account=account, subject=subject, to_emails=addresses, body=body,
                                              attachments=items, scheduled_at=timezone.now(), trigger='agent')
        proposal.result_url = f'/admin/email_assistant/scheduledemail/{sched.pk}/change/'
    else:
        draft = EmailDraft.objects.create(account=account, subject=subject, to_emails=addresses, body=body,
                                          attachments=items, source=EmailDraft.SOURCE_MANUAL)
        proposal.result_url = f'/email/compose/?draft={draft.pk}'
    proposal.mail_to, proposal.mail_subject, proposal.mail_body = ', '.join(addresses), subject, body
    proposal.save(update_fields=['mail_to', 'mail_subject', 'mail_body', 'result_url'])
    product_fields.decided(proposal, user, AgentProposal.APPLIED)
    _settle(proposal.task)
    return ''
