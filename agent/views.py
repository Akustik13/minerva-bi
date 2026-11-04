"""Вікно «🤖 Доручити агенту» в пошті (agent/mail.py). Лише свій лист (скринька людини) і право додавати доручення."""
import json

from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_POST

from agent import mail as agent_mail
from agent.models import AgentRule


def _message(request, pk):
    from email_assistant.models import EmailMessage
    msg = (EmailMessage.objects.filter(pk=pk, account__user=request.user)
           .select_related('account', 'thread').first())
    if msg is None:
        raise Http404
    return msg


def _refused(request):
    if not request.user.has_perm('agent.add_agentrule'):
        return JsonResponse({'ok': False, 'error': 'Немає права створювати доручення агенту'}, status=403)
    return None


def _data(request) -> dict:
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@staff_member_required
def suggest_view(request, pk):
    """GET: що підставити у вікно з цього листа + свої категорії + доручення скриньки, під які лист уже підходить."""
    refused = _refused(request)
    if refused:
        return refused
    msg = _message(request, pk)
    from email_assistant.models import MailCategory
    rules = []
    for rule in AgentRule.objects.filter(account=msg.account).select_related('category'):
        ok, why = agent_mail.matches(rule, msg)
        if ok:
            mode = rule.get_mode_display() if rule.kind == AgentRule.MAIL_REPLY else rule.get_kind_display()
            rules.append({'id': rule.pk, 'name': rule.name, 'mode': mode, 'enabled': rule.enabled,
                          'why': why, 'url': f'/admin/agent/agentrule/{rule.pk}/change/'})
    return JsonResponse({
        'ok': True,
        **agent_mail.suggest(msg),
        'categories': [{'id': c.pk, 'name': f'{c.icon} {c.name}'.strip()}
                       for c in MailCategory.objects.order_by('order', 'name')],
        'rules': rules,
        'modes': [{'value': v, 'label': lbl} for v, lbl in AgentRule.MODES],
        'kinds': [{'value': v, 'label': lbl} for v, lbl in AgentRule.KINDS],
        'can_task': request.user.has_perm('agent.add_agenttask'),
    })


@staff_member_required
@require_POST
def try_view(request, pk):
    """«🧪 Спробувати на цьому листі» — Claude пише відповідь, нічого не зберігається й не надсилається."""
    refused = _refused(request)
    if refused:
        return refused
    msg = _message(request, pk)
    try:
        rule = agent_mail.rule_from(_data(request), msg, request.user)
    except ValueError as e:
        return JsonResponse({'ok': False, 'error': str(e)})
    if rule.kind == AgentRule.MAIL_TASK:
        return JsonResponse({'ok': False, 'error': 'Для завдання — «І одразу завдання з цього листа» під час збереження'})
    return JsonResponse({'ok': True, **agent_mail.try_on(rule, msg)})


@staff_member_required
@require_POST
def save_view(request, pk):
    """«💾 Зберегти доручення» (+ reply з «🧪 Спробувати» — чернетка й для цього листа)."""
    refused = _refused(request)
    if refused:
        return refused
    msg = _message(request, pk)
    data = _data(request)
    try:
        rule = agent_mail.rule_from(data, msg, request.user)
    except ValueError as e:
        return JsonResponse({'ok': False, 'error': str(e)})
    if rule.mode == AgentRule.SEND and not request.user.has_perm('agent.change_agentrule'):
        rule.mode = AgentRule.DRAFT
    if rule.kind == AgentRule.MAIL_TASK and not request.user.has_perm('agent.add_agenttask'):
        return JsonResponse({'ok': False, 'error': 'Немає права давати завдання агенту'}, status=403)
    out = agent_mail.save(rule, msg, reply=str(data.get('reply') or '') if data.get('draft_now') else '',
                          task_now=bool(data.get('task_now')), subject=str(data.get('subject') or '')[:300])
    task = out.get('task')
    return JsonResponse({'ok': True, 'id': out['rule'].pk, 'url': f'/admin/agent/agentrule/{out["rule"].pk}/change/',
                         'draft': out['draft'].pk if out['draft'] else None,
                         'task': task.pk if task else None,
                         'task_url': f'/admin/agent/agenttask/{task.pk}/review/' if task else ''})
