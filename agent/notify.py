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


def _send_mail(user, m, link_text='Відкрити завдання', to=None) -> str:
    """to — адреси (звіт доручення «Кому звіт»); None / порожньо — email людини."""
    from worktime import reports
    to = list(to or []) or [a for a in [_email_to(user)] if a]
    if not to:
        return 'немає email'
    text = '\n'.join([m['head'], ''] + m['lines'] + (['', m['summary']] if m['summary'] else []) +
                     ['', f'{link_text}: {m["link"]}'])
    html = (f'<p><b>{escape(m["head"])}</b></p><p>' + '<br>'.join(escape(x) for x in m['lines']) + '</p>'
            + (f'<p style="white-space:pre-wrap;color:#444">{escape(m["summary"])}</p>' if m['summary'] else '')
            + f'<p><a href="{escape(m["link"])}">{escape(link_text)} →</a></p>')
    err = reports.deliver(None, to, m['subject'], text, html)
    if err and 'SMTP' in err:                        # сповіщення не налаштовані — зі своєї скриньки
        from email_assistant.models import EmailAccount
        own = EmailAccount.objects.filter(user=user, is_active=True).exclude(smtp_host='').first()
        if own is not None:
            err = reports.deliver(own, to, m['subject'], text, html)
    return err


def _send_telegram(user, m, link_text='Відкрити завдання', chats=None) -> str:
    """chats — Chat ID (звіт доручення «Telegram-чати звіту»); None / порожньо — Telegram ID людини."""
    tg_max = m.get('tg_max', 500)
    own = getattr(getattr(user, 'profile', None), 'telegram_id', None)
    chats = list(chats or []) or ([own] if own else [])
    if not chats:
        return ''
    from config.models import NotificationSettings
    from dashboard.notifications import _send_telegram as send
    ns = NotificationSettings.get()
    if not ns.telegram_bot_token:
        return 'немає бота в «Налаштування → Сповіщення»'
    text = (f'<b>{escape(m["subject"])}</b>\n{escape(m["head"])}\n' + '\n'.join(escape(x) for x in m['lines'])
            + (f'\n\n{escape(m["summary"][:tg_max])}' if m['summary'] else '')
            + f'\n\n<a href="{escape(m["link"])}">{escape(link_text)}</a>')
    errors = []
    for chat in chats:
        try:
            send(ns, text, chat_id_override=chat)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e)[:150])
    return '; '.join(errors)


def task_finished(task_id, note='') -> str:
    """Після `_finish`: повідомити власника, якщо прохід був довгим. → що записано в кроки ('' — нічого)."""
    task = AgentTask.objects.select_related('owner', 'owner__profile', 'schedule').filter(pk=task_id).first()
    if task is None or task.quiet:          # 🧪 пробний запуск — лише на сторінці завдання
        return ''
    if task.schedule_id:                    # ⏰ регулярне завдання — звіт завжди, у вибрані канали
        s = task.schedule
        return deliver(task.owner, report(task, note), mail=s.notify_email, telegram=s.notify_telegram)
    rule = rule_of(task)
    if rule is not None:                    # 🧩 доручення — «🔔 Звіт про зроблене» за його налаштуваннями
        return rule_report(task, rule, note)
    if not due(task):
        return ''
    return deliver(task.owner, compose(task, note))


REPORT_MAX = 3500       # звіт регулярного завдання — пошта
REPORT_TG_MAX = 3000    # і Telegram (ліміт повідомлення 4096 разом з розміткою)


def report(task, note='') -> dict:
    """Звіт регулярного завдання (agent/schedules.py): підсумок агента повністю (до REPORT_MAX)."""
    from django.utils import timezone
    m = compose(task, note)
    s = task.schedule
    summary = (task.summary or '').strip()
    if len(summary) > REPORT_MAX:
        summary = summary[:REPORT_MAX].rstrip() + '…'
    pending = task.proposals.filter(status=AgentProposal.PENDING).count()
    ok = task.status != AgentTask.FAILED and not note
    lines = [f'Витрачено: {task.cost_usd:.2f} $ з {task.budget_usd} $']
    if pending:
        lines.insert(0, f'Пропозицій чекають вашої перевірки: {pending}')
    return {'subject': f'🤖 {s.name} — {timezone.localtime():%d.%m.%Y}'[:200],
            'head': f'🤖 Звіт агента — «{s.name}»' if ok else m['head'],
            'lines': lines, 'summary': summary or 'Агент нічого не написав.', 'link': m['link'],
            'tg_max': REPORT_TG_MAX}


def deliver(user, m, link_text='Відкрити завдання', *, mail=True, telegram=True, to=None, chats=None) -> str:
    """Лист і (якщо в профілі є Telegram ID) Telegram людині. m — {subject, head, lines, summary, link}.
    to / chats — інші адреси й Telegram-чати замість людини (звіт доручення).
    Ним же — «⚠️ Немає реакції на лист» (email_assistant/followups.py), «‼️ Дуже важливий лист» (agent/mail.py).
    → «🔔 Повідомлено: пошта, Telegram» / «🔕 Не вдалося повідомити — …»."""
    telegram = telegram and bool(chats or getattr(getattr(user, 'profile', None), 'telegram_id', None))
    sent, failed = [], []
    for name, fn, wanted, extra in (('пошта', _send_mail, mail, to), ('Telegram', _send_telegram, telegram, chats)):
        if not wanted:
            continue
        try:
            err = fn(user, m, link_text, extra)
        except Exception as e:  # noqa: BLE001 — повідомлення не має ламати роботу
            logger.warning('notify %s user %s: %s', name, getattr(user, 'pk', None), e)
            err = type(e).__name__
        if err:
            failed.append(f'{name}: {err}')
        else:
            sent.append(name)
    parts = (['🔔 Повідомлено: ' + ', '.join(sent)] if sent else []) + \
        (['🔕 Не вдалося повідомити — ' + '; '.join(failed)] if failed else [])
    return '; '.join(parts)


# ── 🧩 Звіт доручення «Завдання агенту з кожного такого листа» ─────────────────

RULE_SUMMARY_MAX = 1500


def rule_of(task):
    """Доручення, з якого створено завдання (AgentRun.task), або None."""
    from agent.models import AgentRule, AgentRun
    run = AgentRun.objects.filter(task=task, rule__kind=AgentRule.MAIL_TASK).select_related('rule').first()
    return run.rule if run is not None else None


def has_result(task, note='') -> bool:
    """Чи є про що звітувати: пропозиції (і внесене самому), помилка або зупинка на ліміті."""
    return bool(task.status == AgentTask.FAILED or note or task.proposals.exists())


def recipients(rule) -> tuple:
    """(адреси, чати) з налаштувань доручення; неправильні рядки пропускаються (форма їх не пускає)."""
    from dashboard.notify_routes import parse_chats, parse_emails
    return parse_emails(rule.report_to)[0], parse_chats(rule.report_chats)[0]


def rule_report(task, rule, note='') -> str:
    """Звіт «🔔 що зробив агент» за листом доручення: канали, кому й коли — з доручення (порожньо — власнику)."""
    if not (rule.report_email or rule.report_telegram):
        return ''
    if rule.report_when != rule.REPORT_ALWAYS and not has_result(task, note):
        return ''
    m = compose(task, note)
    summary = (task.summary or '').strip()
    if len(summary) > RULE_SUMMARY_MAX:
        summary = summary[:RULE_SUMMARY_MAX].rstrip() + '…'
    m['summary'] = summary
    msg = task.message
    if msg is not None:
        m['lines'].insert(0, f'✉️ Лист: «{(msg.subject or "(без теми)")[:150]}» від {msg.from_email or "?"}')
    m['lines'].insert(0, f'🧩 Доручення «{rule.name}»')
    m['subject'] = f'🧩 {rule.name}: {(msg.subject if msg is not None else task.title) or task.title}'[:200]
    m['tg_max'] = 1200
    to, chats = recipients(rule)
    return deliver(task.owner, m, mail=rule.report_email, telegram=rule.report_telegram, to=to, chats=chats)
