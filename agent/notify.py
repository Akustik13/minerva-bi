"""🔔 Агент закінчив завдання — лист і Telegram власнику завдання.

Викликає `tasks._finish` після кожного проходу, що тривав щонайменше `NOTIFY_AFTER_SECONDS` (коротке завдання людина
й так бачить на сторінці): готово / чекає перевірки, зупинився на ліміті витрат, помилка. «⏹ Зупинити» людини — без
повідомлення. Пошта — «Налаштування → Сповіщення» (SMTP), без нього — перша скринька власника; адреса — email
користувача, інакше його скринька. Telegram — бот «Налаштування → Сповіщення» на Telegram ID з профілю. Завдання MCP
(їх Minerva не виконує) повідомлень не шле. Збій відправлення лише пишеться в «Кроки агента».
"""
import logging
from html import escape

from agent.models import AgentProposal, AgentTask

logger = logging.getLogger(__name__)

NOTIFY_AFTER_SECONDS = 60
SUMMARY_MAX = 700


def due(task) -> bool:
    if task.status not in (AgentTask.REVIEW, AgentTask.DONE, AgentTask.FAILED) or task.mcp_session:
        return False
    if not (task.started_at and task.finished_at):
        return False
    return (task.finished_at - task.started_at).total_seconds() >= NOTIFY_AFTER_SECONDS


def _link(task) -> str:
    from django.urls import reverse
    path = reverse('admin:agent_agenttask_review', args=[task.pk])
    try:
        from config.models import SystemSettings
        s = SystemSettings.objects.filter(pk=1).first()
        if s is not None and s.site_domain:
            return s.site_url.rstrip('/') + path
    except Exception:  # noqa: BLE001
        pass
    return path


def compose(task, note='') -> dict:
    """{subject, head, lines, summary, link} — що сталось, людською мовою."""
    pending = task.proposals.filter(status=AgentProposal.PENDING).count()
    if task.status == AgentTask.FAILED:
        head = f'⚠️ Агент зупинився з помилкою: {task.error or "невідома помилка"}'
    elif note:
        head = f'⏸ Агент зупинився: {note}. Знайдене лишається — «▶ Продовжити» на сторінці завдання.'
    elif pending:
        head = f'✅ Агент закінчив — {pending} пропозицій чекають вашої перевірки (✓ / ✕).'
    else:
        head = '✅ Агент закінчив — нових пропозицій немає.'
    lines = []
    total = task.products.count()
    if task.each_product and total:
        lines.append(f'Товарів: {len(task.done_products or [])} з {total}')
    elif total:
        lines.append(f'Товарів у завданні: {total}')
    lines.append(f'Витрачено: {task.cost_usd:.2f} $ з {task.budget_usd} $')
    summary = (task.summary or '').strip()
    if len(summary) > SUMMARY_MAX:
        summary = summary[:SUMMARY_MAX].rstrip() + '…'
    icon = '⚠️' if task.status == AgentTask.FAILED else ('⏸' if note else '🤖')
    return {'subject': f'{icon} Агент: {task.title}'[:200], 'head': head, 'lines': lines, 'summary': summary,
            'link': _link(task)}


def _email_to(user) -> str:
    if user.email and '@' in user.email:
        return user.email
    from email_assistant.models import EmailAccount
    acc = EmailAccount.objects.filter(user=user).exclude(email_address='').first()
    return acc.email_address if acc else ''


def _send_mail(user, m) -> str:
    from worktime import reports
    to = _email_to(user)
    if not to:
        return 'немає email'
    text = '\n'.join([m['head'], ''] + m['lines'] + (['', m['summary']] if m['summary'] else []) +
                     ['', f'Відкрити завдання: {m["link"]}'])
    html = (f'<p><b>{escape(m["head"])}</b></p><p>' + '<br>'.join(escape(x) for x in m['lines']) + '</p>'
            + (f'<p style="white-space:pre-wrap;color:#444">{escape(m["summary"])}</p>' if m['summary'] else '')
            + f'<p><a href="{escape(m["link"])}">Відкрити завдання →</a></p>')
    err = reports.deliver(None, [to], m['subject'], text, html)
    if err and 'SMTP' in err:                        # сповіщення не налаштовані — зі своєї скриньки
        from email_assistant.models import EmailAccount
        own = EmailAccount.objects.filter(user=user, is_active=True).exclude(smtp_host='').first()
        if own is not None:
            err = reports.deliver(own, [to], m['subject'], text, html)
    return err


def _send_telegram(user, m) -> str:
    chat = getattr(getattr(user, 'profile', None), 'telegram_id', None)
    if not chat:
        return ''
    from config.models import NotificationSettings
    from dashboard.notifications import _send_telegram as send
    ns = NotificationSettings.get()
    if not ns.telegram_bot_token:
        return 'немає бота в «Налаштування → Сповіщення»'
    text = (f'<b>{escape(m["subject"])}</b>\n{escape(m["head"])}\n' + '\n'.join(escape(x) for x in m['lines'])
            + (f'\n\n{escape(m["summary"][:500])}' if m['summary'] else '')
            + f'\n\n<a href="{escape(m["link"])}">Відкрити завдання</a>')
    try:
        send(ns, text, chat_id_override=chat)
    except Exception as e:  # noqa: BLE001
        return str(e)[:150]
    return ''


def task_finished(task_id, note='') -> str:
    """Після `_finish`: повідомити власника, якщо прохід був довгим. → що записано в кроки ('' — нічого)."""
    task = AgentTask.objects.select_related('owner', 'owner__profile').filter(pk=task_id).first()
    if task is None or not due(task):
        return ''
    m = compose(task, note)
    telegram = bool(getattr(getattr(task.owner, 'profile', None), 'telegram_id', None))
    sent, failed = [], []
    for name, fn, wanted in (('пошта', _send_mail, True), ('Telegram', _send_telegram, telegram)):
        if not wanted:
            continue
        try:
            err = fn(task.owner, m)
        except Exception as e:  # noqa: BLE001 — повідомлення не має ламати завдання
            logger.warning('agent notify %s task %s: %s', name, task.pk, e)
            err = type(e).__name__
        if err:
            failed.append(f'{name}: {err}')
        else:
            sent.append(name)
    parts = (['🔔 Повідомлено: ' + ', '.join(sent)] if sent else []) + \
        (['🔕 Не вдалося повідомити — ' + '; '.join(failed)] if failed else [])
    return '; '.join(parts)
