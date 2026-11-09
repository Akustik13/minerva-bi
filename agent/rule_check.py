"""🧪 «Перевірити, як працює» на сторінці доручення агента: що робитиме доручення (людською мовою), які листи
скриньки за останні дні під нього підходять (і чому інші — ні), і що агент зробив би з вибраним листом.

Нічого не зберігає й не надсилає: `one` — це `mail.try_on` (відповідь / клієнту замовлення — виклик AI, як
«🧪 Спробувати на цьому листі» у пошті; пересилання й позначка — без AI) плюс опис результату. Перевіряється
збережене доручення.
"""
import datetime

from django.utils import timezone

from agent import mail as agent_mail
from agent.models import AgentRule, AgentRun

DAYS = 60
SCAN = 400          # листів скриньки, які переглядаємо
SHOW = 10           # підходять — показати до стількох
NEAR = 5            # від того самого відправника, але не підійшли — до стількох
AI_KINDS = (AgentRule.MAIL_REPLY, AgentRule.MAIL_CUSTOMER)
SIGN_TEXT = {
    AgentRule.SIGN_FULL: '🤖 Наприкінці листа — підпис AI-помічника мовою листа (ім’я помічника, чий він, фірма); '
                         'під ним — підпис скриньки.',
    AgentRule.SIGN_LINE: '🤖 Наприкінці листа — рядок «Цей лист написав наш AI-помічник» мовою листа.',
    AgentRule.SIGN_OFF: '✍️ Без позначки AI — лист виглядає як від вас.',
}
UNTIL_TEXT = {'reaction': 'немає ні підтвердження прочитання, ні відповіді', 'reply': 'немає відповіді'}


def options_text(rule) -> str:
    """«❗ важливо · 📬 запит на прочитання · ⏰ нагадаю через 3 дн., якщо немає відповіді» ('' — нічого)."""
    parts = []
    if rule.opt_important:
        parts.append('❗ з позначкою «важливо»')
    if rule.opt_receipt:
        parts.append('📬 із запитом на прочитання')
    if rule.remind_days:
        parts.append(f'⏰ нагадаю вам через {rule.remind_days} дн., якщо {UNTIL_TEXT.get(rule.remind_until, "")}')
    return ' · '.join(parts)


def _mode_text(rule) -> str:
    if rule.mode == AgentRule.SEND:
        return (f'✉️ надсилає сам: лист стає в «⏳ Заплановані» на {rule.send_delay_minutes} хв (можна скасувати); '
                f'понад {rule.daily_limit} листів на день — лише чернетка')
    return '📝 лише чернетка в «📝 Чернетках» — ви перевіряєте й надсилаєте'


def plan(rule) -> list:
    """Що робитиме доручення з кожним новим листом, що підходить, — рядками."""
    lines = []
    if not rule.enabled:
        lines.append('⏸ Доручення вимкнене — агент нічого не робить, доки не ввімкнете «Увімкнено».')
    if rule.kind == AgentRule.MAIL_REPLY:
        lines.append('✉️ Агент (AI) пише відповідь відправнику листа за інструкцією «Що робити».')
        lines.append(_mode_text(rule) + '; не впевнений у відповіді — завжди чернетка.')
    elif rule.kind == AgentRule.MAIL_CUSTOMER:
        lines.append('📨 Агент знаходить у листі трек-номер замовлення Minerva і (AI) пише клієнту цього замовлення '
                     'за інструкцією; немає замовлення з таким трек-номером — пропускає.')
        lines.append(_mode_text(rule) + '.')
        if rule.repeat_days == AgentRule.REPEAT_NEVER:
            lines.append('🔁 Про те саме замовлення клієнту вдруге не пише.')
        else:
            lines.append(f'🔁 Про те саме замовлення ще раз — {rule.get_repeat_days_display()}: повторне нагадування '
                         f'з датою попереднього листа; чернетка, яку ви ще не надіслали, — друга не створюється.')
    elif rule.kind == AgentRule.MAIL_FORWARD:
        to = ', '.join(agent_mail.forward_list(rule)) or '— не вказано, кому! —'
        lines.append(f'📤 Пересилає лист з усіма вкладеннями на {to}, без AI; над листом — текст «Що робити».')
        lines.append(_mode_text(rule) + '.')
    elif rule.kind == AgentRule.MAIL_MARK:
        lines.append(f'{rule.get_importance_display()} — позначка листа в списку й у папці «⭐ Важливі», без AI.')
        lines.append('🔔 Одразу лист і Telegram вам.' if rule.notify else '🔕 Без повідомлення — лише позначка.')
    elif rule.kind == AgentRule.MAIL_TASK:
        lines.append(f'🧩 З кожного такого листа — завдання агенту ({rule.get_task_model_display()}, до '
                     f'{rule.task_budget_usd} $): агент читає лист і вкладення й пропонує зміни на сторінці завдання.')
        if rule.auto_apply:
            lines.append('✓ Трек-номер і кур’єра в порожнє поле замовлення вносить сам.')
    if rule.kind in (AgentRule.MAIL_REPLY, AgentRule.MAIL_CUSTOMER):
        lines.append(SIGN_TEXT[rule.ai_sign])
    if rule.kind in (AgentRule.MAIL_REPLY, AgentRule.MAIL_CUSTOMER, AgentRule.MAIL_FORWARD):
        cc = agent_mail.cc_list(rule)
        if cc:
            lines.append('Копія (CC): ' + ', '.join(cc))
        opts = options_text(rule)
        if opts:
            lines.append('Лист піде ' + opts + '.')
    lines.append('Агент бере лише нові листи у Вхідних (не старші за три дні), кожен — один раз; перевіряє щохвилини.')
    return lines


def candidates(rule, days=DAYS) -> dict:
    """{found: [{msg, why, blocked, run}], near: [{msg, why}], scanned, days} — листи скриньки за `days` днів."""
    from email_assistant.models import EmailMessage
    since = timezone.now() - datetime.timedelta(days=days)
    qs = (EmailMessage.objects.filter(account_id=rule.account_id, folder=EmailMessage.FOLDER_INBOX, is_spam=False,
                                      is_deleted=False, sent_at__gte=since)
          .select_related('account', 'thread').order_by('-sent_at')[:SCAN])
    found, near, scanned = [], [], 0
    with_sender = bool(rule.lines('senders'))
    for msg in qs:
        scanned += 1
        ok, why = agent_mail.matches(rule, msg)
        if ok:
            if len(found) < SHOW:
                found.append({'msg': msg, 'why': why, 'blocked': agent_mail.why_not(rule, msg, check_age=False),
                              'run': AgentRun.objects.filter(rule=rule, message=msg).first()})
        elif with_sender and why != 'інший відправник' and len(near) < NEAR:
            near.append({'msg': msg, 'why': why})
    return {'found': found, 'near': near, 'scanned': scanned, 'days': days}


def one(rule, msg) -> dict:
    """Що агент зробив би з цим листом: try_on + {outcome, options, ai}. Нічого не зберігає й не надсилає."""
    ok, why = agent_mail.matches(rule, msg)
    out = {'matches': ok, 'why': why, 'blocked': agent_mail.why_not(rule, msg, check_age=False),
           'ai': rule.kind in AI_KINDS, 'options': options_text(rule)}
    if rule.kind == AgentRule.MAIL_TASK:
        return {**out, 'outcome': f'🧩 Завдання агенту з цього листа ({rule.get_task_model_display()}, до '
                                  f'{rule.task_budget_usd} $). У перевірці не запускається — агент читав би лист '
                                  f'і вкладення й пропонував зміни на сторінці завдання.'}
    res = agent_mail.try_on(rule, msg)
    out.update(res)
    out['matches'], out['why'] = ok, why
    if res.get('error'):
        out['outcome'] = '⚠️ ' + res['error']
    elif res.get('skip'):
        out['outcome'] = '↷ Пропустив би: ' + (res.get('skip_reason') or 'так вирішив агент')
    elif rule.kind == AgentRule.MAIL_MARK:
        out['outcome'] = res.get('reply', '')
    elif rule.mode == AgentRule.SEND and res.get('confident', True):
        when = timezone.localtime(timezone.now() + datetime.timedelta(minutes=rule.send_delay_minutes))
        out['outcome'] = (f'✉️ Надіслав би сам о {when:%H:%M} (через {rule.send_delay_minutes} хв, у «⏳ Запланованих»'
                          f' — можна скасувати)')
    else:
        out['outcome'] = '📝 Поклав би чернетку в «📝 Чернетки»' + (
            ' — не впевнений' + (f': {res["doubts"]}' if res.get('doubts') else '')
            if rule.mode == AgentRule.SEND else '')
    if out['blocked']:
        out['outcome'] += f' · ⚠️ насправді — ні: {out["blocked"]}'
    return out
