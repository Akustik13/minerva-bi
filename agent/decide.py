"""✓ / ✕ людини над пропозиціями агента (сторінка завдання, agent/admin.py).

Поля й параметри товару — agent/products.py `apply`. Лист — лише на адреси з тексту завдання (`tools.allowed_addresses`,
перевіряється й тут: людина могла дописати адресу в полі «Кому») зі скриньки людини: «📝 Чернетка» — EmailDraft
(відкривається в «✏️ Написати»), «✉️ Надіслати» — ScheduledEmail зараз (cron `send_scheduled`, до 2 хв, у
«⏳ Заплановані» ще можна скасувати); даташити — копією у вкладенні (`attachments.store_file`).
"""
from django.utils import timezone

from agent import products as product_fields
from agent.models import AgentProposal, AgentTask
from agent.tools import EMAIL_RE, ToolError, _datasheet_bytes, allowed_addresses


def apply_selected(task, ids, user) -> dict:
    out = {'applied': 0, 'failed': []}
    for p in task.proposals.filter(pk__in=ids, status=AgentProposal.PENDING).exclude(kind=AgentProposal.MAIL) \
            .select_related('product'):
        error = product_fields.apply(p, user)
        product_fields.decided(p, user, AgentProposal.FAILED if error else AgentProposal.APPLIED, error)
        if error:
            out['failed'].append(f'{p.product.sku if p.product else "—"} · {product_fields.label(p)}: {error}')
        else:
            out['applied'] += 1
    _settle(task)
    return out


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
