"""Стан листа за дорученням агента — так, як він є зараз, а не лише що агент зробив тоді (AgentRun.status):
«✉️ Заплановано» після відправки — «✓ Надіслано 09.11 17:33», «📝 Чернетка готова» після того, як ви її надіслали
або видалили, — «✓ Надіслано без змін» / «🗑 Чернетку видалено». Один текст для «🧪 Перевірити, як працює»,
«📜 Останніх листів за цим дорученням», журналу агента й рядка «🤖 Агент» у прев'ю листа.
"""
from django.utils import timezone

from agent.models import AgentLetter, AgentRun


def _when(dt) -> str:
    if not dt:
        return ''
    local = timezone.localtime(dt)
    return local.strftime('%H:%M') if local.date() == timezone.localdate() else local.strftime('%d.%m %H:%M')


def _letter(run):
    """Лист агента, що пішов за цією роботою (agent/letters.py), — None, якщо такого немає."""
    cache = getattr(run, '_prefetched_objects_cache', {}).get('letters')
    letters = list(cache) if cache is not None else list(run.letters.all())
    sent = [x for x in letters if x.message_id]
    return sent[0] if sent else (letters[0] if letters else None)


def done(run) -> str:
    """Чим закінчився лист, якщо вже закінчився: надіслано / скасовано / не надіслано / чернетку видалено ('' — ще
    чекає: запланований лист у черзі, чернетка в «📝 Чернетках», або агент лише пропустив / позначив)."""
    from email_assistant.models import ScheduledEmail
    if run.status == AgentRun.SCHEDULED:
        s = run.scheduled
        if s is None:
            letter = _letter(run)
            return '✓ Надіслано' if letter and letter.message_id else '✕ Запланований лист видалено'
        if s.status == ScheduledEmail.STATUS_SENT:
            return f'✓ Надіслано {_when(s.sent_at)}'.strip()
        if s.status == ScheduledEmail.STATUS_CANCELLED:
            return '✕ Відправку скасовано'
        if s.status == ScheduledEmail.STATUS_FAILED:
            return '⚠️ Не надіслано — помилка відправки'
        return ''
    if run.status == AgentRun.DRAFTED:
        if run.outcome:
            return run.get_outcome_display()
        if run.draft_id:
            return ''
        letter = _letter(run)
        if letter is None:
            return '📝 Чернетку закрито'
        if letter.message_id:
            return dict(AgentRun.OUTCOMES).get(
                AgentRun.SENT_EDITED if letter.how == AgentLetter.EDITED else AgentRun.SENT_SAME)
        s = letter.scheduled
        if s is not None and s.status == ScheduledEmail.STATUS_PENDING:
            return f'⏳ Ви запланували на {_when(s.scheduled_at)}'
        return '📝 Чернетку закрито'
    return ''


def text(run) -> str:
    """Стан для таблиць: чим закінчилось, інакше — що зараз (без подвійного значка)."""
    final = done(run)
    if final:
        return final
    if run.status == AgentRun.SCHEDULED and run.scheduled is not None:
        if run.scheduled.sent_at:
            return '📤 Надсилається…'
        return f'⏳ Надішле о {_when(run.scheduled.scheduled_at)}'
    if run.status == AgentRun.DRAFTED:
        return '📝 Чернетка готова — чекає вас'
    return run.get_status_display()
