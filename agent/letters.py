"""🤖 Листи, які написав агент: запис, коли лист справді пішов, і мітка «🤖 агент» у пошті Minerva.

Лист агента — `AgentLetter` за Message-ID (скринька + Message-ID), тож мітку бачать і локальна копія надісланого, і
копія з «Надісланих» сервера. Звідки береться:
• запланований лист агента (`ScheduledEmail.trigger='agent'`: доручення — `AgentRun.scheduled`, завдання —
  `AgentProposal.result_url`) — `scheduled_sent` із `send_scheduled` після відправки → «агент надіслав сам»;
• чернетка агента (доручення — `AgentRun.draft`, завдання — `AgentProposal.result_url` `/email/compose/?draft=`)
  — `draft_closed` з `email_assistant.drafts.discard`: надіслано з «Написати» → «ви надіслали без змін / зі змінами»;
  відкладено «⏰ Пізніше» → запис з `scheduled`, Message-ID допише `scheduled_sent`.
Мітки — `attach(account, messages)` (список листів і прев'ю), `draft_marks(account, drafts)` (папка «📝 Чернетки»).
Клієнт нічого не бачить: у самому листі позначки немає.
"""
import logging
import re

from agent.models import AgentLetter, AgentProposal, AgentRun

logger = logging.getLogger(__name__)

SHORT = '🤖 агент'


def _norm(text) -> str:
    return re.sub(r'\s+', ' ', str(text or '')).strip().lower()


def record(account, message_id, *, run=None, task=None, how=AgentLetter.SENT, scheduled=None):
    """Запам'ятати лист агента (повтор того самого Message-ID — той самий запис)."""
    message_id = str(message_id or '').strip()[:500]
    if not message_id and scheduled is None:
        return None
    if message_id:
        letter = AgentLetter.objects.filter(account=account, message_id=message_id).first()
        if letter is not None:
            return letter
    return AgentLetter.objects.create(account=account, message_id=message_id, run=run, task=task, how=how,
                                      scheduled=scheduled)


def _scheduled_proposal(scheduled):
    return (AgentProposal.objects.filter(kind=AgentProposal.MAIL,
                                         result_url=f'/admin/email_assistant/scheduledemail/{scheduled.pk}/change/')
            .select_related('task').first())


def _draft_proposal(draft):
    return (AgentProposal.objects.filter(kind=AgentProposal.MAIL, result_url=f'/email/compose/?draft={draft.pk}')
            .select_related('task').first())


def scheduled_sent(scheduled, message_id):
    """send_scheduled: запланований лист пішов. Лист агента (доручення чи завдання) або відкладена чернетка агента."""
    try:
        pending = AgentLetter.objects.filter(scheduled=scheduled, message_id='').first()
        if pending is not None:                    # чернетку агента відклали «⏰ Пізніше» — тепер вона пішла
            if message_id:
                AgentLetter.objects.filter(pk=pending.pk).update(message_id=str(message_id)[:500])
            return
        if getattr(scheduled, 'trigger', '') != 'agent':
            return
        run = AgentRun.objects.filter(scheduled=scheduled).first()
        proposal = None if run is not None else _scheduled_proposal(scheduled)
        if run is None and proposal is None:
            return
        record(scheduled.account, message_id, run=run, task=proposal.task if proposal else None)
    except Exception:  # noqa: BLE001 — мітка не заважає надсиланню
        logger.warning('agent letter for scheduled %s', getattr(scheduled, 'pk', None), exc_info=True)


def draft_closed(draft, how, text='', message_id='', scheduled=None):
    """drafts.discard: чернетку агента надіслали (sent) чи відклали (scheduled) — запис листа агента."""
    if how not in ('sent', 'scheduled'):
        return
    run = AgentRun.objects.filter(draft=draft).first()
    proposal = None if run is not None else _draft_proposal(draft)
    if run is None and proposal is None:
        return
    agent_text = run.reply if run is not None else proposal.mail_body
    same = bool(_norm(agent_text)) and _norm(agent_text) in _norm(text)
    kind = AgentLetter.DRAFT if same else AgentLetter.EDITED
    task = proposal.task if proposal is not None else None
    if how == 'sent':
        record(draft.account, message_id, run=run, task=task, how=kind)
    elif scheduled is not None:
        record(draft.account, '', run=run, task=task, how=kind, scheduled=scheduled)


# ── Мітки в пошті ────────────────────────────────────────────────────────────

def _title(letter) -> str:
    who = ''
    if letter.run_id and letter.run and letter.run.rule_id:
        who = f' · доручення «{letter.run.rule.name}»'
    elif letter.task_id and letter.task:
        who = f' · завдання «{letter.task.title}»'
    return f'Написав агент Minerva{who} · {letter.get_how_display()}'


def _url(letter) -> str:
    from django.urls import reverse
    if letter.run_id:
        return reverse('admin:agent_agentrun_change', args=[letter.run_id])
    if letter.task_id:
        return reverse('admin:agent_agenttask_review', args=[letter.task_id])
    return ''


def marks(account, message_ids) -> dict:
    """{Message-ID: {short, title, url, how}} — листи агента серед цих."""
    ids = {m for m in message_ids if m}
    if not ids or account is None:
        return {}
    out = {}
    for letter in (AgentLetter.objects.filter(account=account, message_id__in=ids)
                   .select_related('run__rule', 'task')):
        out[letter.message_id] = {'short': SHORT, 'title': _title(letter), 'url': _url(letter), 'how': letter.how}
    return out


def attach(account, messages):
    """m.agent_mark — для листів, які написав агент (інакше None)."""
    try:
        found = marks(account, [getattr(m, 'message_id', '') for m in messages])
    except Exception:  # noqa: BLE001 — мітки не ламають пошту
        logger.warning('agent letter marks', exc_info=True)
        found = {}
    for m in messages:
        m.agent_mark = found.get(getattr(m, 'message_id', '') or '')


def draft_marks(account, drafts):
    """d.agent_mark — чернетки, які підготував агент (доручення або завдання)."""
    try:
        drafts = list(drafts)
        pks = [d.pk for d in drafts]
        runs = {r.draft_id: r for r in AgentRun.objects.filter(draft_id__in=pks).select_related('rule')}
        urls = {f'/email/compose/?draft={pk}': pk for pk in pks}
        props = {urls[p.result_url]: p for p in AgentProposal.objects.filter(
            kind=AgentProposal.MAIL, result_url__in=list(urls)).select_related('task')}
    except Exception:  # noqa: BLE001
        logger.warning('agent draft marks', exc_info=True)
        runs, props = {}, {}
    for d in drafts:
        run, prop = runs.get(d.pk), props.get(d.pk)
        if run is not None:
            d.agent_mark = {'short': SHORT, 'title': f'Чернетку підготував агент Minerva · доручення «{run.rule.name}»'}
        elif prop is not None:
            d.agent_mark = {'short': SHORT, 'title': f'Чернетку підготував агент Minerva · завдання «{prop.task.title}»'}
        else:
            d.agent_mark = None
