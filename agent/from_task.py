"""🔁 З готового завдання агенту: те саме для інших листів і 🤖 доручення з нього.

• `repeat` — копія завдання (інструкція без службової примітки доручення / розкладу, товари, модель, ліміт, «Кому» й
  копія) для кожного вибраного листа зі своїх скриньок, одразу в роботу; завдання без листа — просто ще раз.
  Листи на вибір — `candidates`: Вхідні своїх скриньок за `DAYS`, схожі першими (той самий відправник, домен,
  початок теми), позначка, якщо таке завдання з цим листом уже було.
• `target` — куди веде «🤖 Створити доручення з завдання»: завдання з листа → нове доручення «🧩 Завдання агенту з
  кожного такого листа» (`rule_initial`: скринька листа, інструкція, умови — відправник і початок теми, модель,
  ліміт), без листа → ⏰ регулярне завдання (`schedule_initial`); завдання вже з доручення чи розкладу — до нього.
Нічого не зберігає саме: форму доручення чи розкладу людина перевіряє й зберігає.
"""
import datetime
import re
from decimal import Decimal

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from agent.models import AgentRule, AgentRun, AgentTask

DAYS = 30
CANDIDATES_MAX = 40
REPEAT_MAX = 10
RULE_BUDGET_MIN = Decimal('0.50')

# службові примітки, які додають доручення (agent/mail.py TASK_NOTE) і розклад (agent/schedules.py NOTE)
_NOTE_RE = re.compile(r'\s*\((?:Доручення «[^»]*»: цей лист підійшов під умови|Регулярне завдання за розкладом «[^»]*»)'
                      r'\..*\)\s*$', re.S)


def base_instruction(task) -> str:
    """Інструкція людини — без службової примітки доручення чи розкладу."""
    return _NOTE_RE.sub('', task.instruction or '').strip()


def own_message(task, user):
    """Лист завдання, якщо він зі скриньки цієї людини (інакше None)."""
    msg = task.message if task.message_id else None
    return msg if msg is not None and msg.account.user_id == user.pk else None


def source_rule(task):
    run = AgentRun.objects.filter(task=task).select_related('rule').first()
    return run.rule if run is not None else None


# ── 🔁 Те саме для інших листів ──────────────────────────────────────────────

def _domain(addr) -> str:
    addr = (addr or '').strip().lower()
    return addr.split('@', 1)[1] if '@' in addr else ''


def candidates(task, user, q='', days=DAYS) -> list:
    """Листи для повтору: [{msg, score, why, done (AgentTask | None)}], схожі першими, потім новіші."""
    from agent.mail import subject_phrase
    from email_assistant.models import EmailMessage
    since = timezone.now() - datetime.timedelta(days=days)
    qs = (EmailMessage.objects.filter(account__user=user, folder=EmailMessage.FOLDER_INBOX, is_spam=False,
                                      is_deleted=False, sent_at__gte=since)
          .exclude(pk=task.message_id or 0).select_related('account').order_by('-sent_at'))
    q = ' '.join(str(q or '').split())[:100]
    if q:
        qs = qs.filter(Q(subject__icontains=q) | Q(from_email__icontains=q) | Q(from_name__icontains=q))
    msgs = list(qs[:300])
    base = task.message if task.message_id else None
    sender = (base.from_email or '').strip().lower() if base else ''
    domain, phrase = _domain(sender), (subject_phrase(base.subject).lower() if base else '')
    rows = []
    for m in msgs:
        score, why = 0, []
        frm = (m.from_email or '').strip().lower()
        if sender and frm == sender:
            score, why = score + 3, why + ['той самий відправник']
        elif domain and _domain(frm) == domain:
            score, why = score + 2, why + ['той самий домен']
        if phrase and subject_phrase(m.subject).lower() == phrase:
            score, why = score + 2, why + ['схожа тема']
        rows.append({'msg': m, 'score': score, 'why': ', '.join(why), 'done': None})
    rows.sort(key=lambda r: (-r['score'], -(r['msg'].sent_at.timestamp() if r['msg'].sent_at else 0)))
    rows = rows[:CANDIDATES_MAX]
    text = base_instruction(task)
    done = {}
    for t in AgentTask.objects.filter(owner=user, message__in=[r['msg'] for r in rows]).order_by('-created_at'):
        if base_instruction(t) == text:
            done.setdefault(t.message_id, t)
    for r in rows:
        r['done'] = done.get(r['msg'].pk)
    return rows


def _copy(task, user, msg):
    new = AgentTask.objects.create(
        owner=user, title=task.title, instruction=base_instruction(task) or task.instruction, mail_to=task.mail_to,
        mail_cc=task.mail_cc, message=msg, each_product=task.each_product, web=task.web, model=task.model,
        budget_usd=task.budget_usd)
    new.products.set(task.products.all())
    return new


def repeat(task, user, message_ids=(), start=True) -> list:
    """Нові завдання: по одному на кожен вибраний лист зі своїх скриньок (до REPEAT_MAX); завдання без листа і
    без вибору — одна копія. → [AgentTask]."""
    from agent import tasks
    from email_assistant.models import EmailMessage
    ids = [int(x) for x in message_ids if str(x).isdigit()][:REPEAT_MAX]
    found = {m.pk: m for m in EmailMessage.objects.filter(pk__in=ids, account__user=user)} if ids else {}
    msgs = [found[i] for i in dict.fromkeys(ids) if i in found]          # у порядку вибору, без повторів
    if ids and not msgs:
        return []
    created = [_copy(task, user, m) for m in msgs] if msgs else [_copy(task, user, None)]
    if start:
        for t in created:
            tasks.start(t)
    return created


# ── 🤖 Доручення з завдання ──────────────────────────────────────────────────

def rule_initial(task, user) -> dict:
    """Початкові дані форми нового доручення «🧩 Завдання агенту з кожного такого листа» (лист — свій)."""
    from agent.mail import suggest
    msg = own_message(task, user)
    if msg is None:
        return {}
    s = suggest(msg)
    budget = min(task.budget_usd, max(RULE_BUDGET_MIN, (task.cost_usd * 3).quantize(Decimal('0.01'))))
    return {'kind': AgentRule.MAIL_TASK, 'name': task.title[:200], 'account': msg.account_id,
            'instruction': base_instruction(task), 'senders': s['sender'], 'subject_words': s['subject'],
            'category': s['category'] or None, 'task_model': task.model, 'task_budget_usd': budget}


def schedule_initial(task) -> dict:
    """Початкові дані форми ⏰ регулярного завдання — завдання без листа."""
    return {'name': task.title[:200], 'instruction': base_instruction(task), 'model': task.model,
            'budget_usd': task.budget_usd}


def target(task, user) -> tuple:
    """(адреса, підпис кнопки) для «🤖 Створити доручення з завдання»; ('', причина) — не можна."""
    if task.mcp_session:
        return '', 'пропозиції зовнішнього агента (MCP) — доручення з них не буває'
    if task.schedule_id:
        return (reverse('admin:agent_agentschedule_change', args=[task.schedule_id]),
                f'⏰ Регулярне завдання «{task.schedule.name}» →')
    rule = source_rule(task)
    if rule is not None:
        return reverse('admin:agent_agentrule_change', args=[rule.pk]), f'🤖 Доручення «{rule.name}» →'
    if task.message_id:
        if own_message(task, user) is None:
            return '', 'лист завдання не з вашої скриньки'
        if not user.has_perm('agent.add_agentrule'):
            return '', 'немає права створювати доручення агенту'
        return (reverse('admin:agent_agentrule_add') + f'?from_task={task.pk}',
                '🤖 Робити так з усіма подібними листами…')
    if not user.has_perm('agent.add_agentschedule'):
        return '', 'немає права створювати регулярні завдання'
    return reverse('admin:agent_agentschedule_add') + f'?from_task={task.pk}', '⏰ Робити це регулярно…'


def task_from_get(request):
    """?from_task=<pk> у формах доручення й розкладу → завдання цієї людини або None."""
    pk = str(request.GET.get('from_task') or '')
    return AgentTask.objects.filter(pk=pk, owner=request.user).select_related('message__account').first() \
        if pk.isdigit() else None
