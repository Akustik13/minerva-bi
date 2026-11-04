"""❗ Важливо / ‼️ Дуже важливо — позначки листів (EmailMessage.importance / importance_by).

Звідки позначка:
- людина — меню правого кліку листа «❗ Важливість», рядок у відкритому листі (`set_level`, by «me»);
- відправник — заголовки Importance: high / X-Priority: 1–2 / Priority: urgent (`from_headers`, by «sender»; так
  позначає Outlook «Wichtigkeit: hoch»). Відправник може сказати лише «важливо» — «дуже важливо» ставить людина
  або доручення;
- доручення агента «❗ Позначати такі листи» (`raise_level`, by «agent»).

Вибір людини сильніший: доручення й відправник його не перебивають, доручення лише підвищує рівень.
Позначені листи — у папці «⭐ Важливі» разом із зірочкою (`folder_q`).
"""
import re

from django.db.models import Q

IMPORTANT, URGENT = 'important', 'urgent'
LEVELS = ('', IMPORTANT, URGENT)
RANK = {'': 0, IMPORTANT: 1, URGENT: 2}
ICONS = {IMPORTANT: '❗', URGENT: '‼️'}
LABELS = {'': 'Звичайний', IMPORTANT: '❗ Важливо', URGENT: '‼️ Дуже важливо'}
BY_SENDER, BY_ME, BY_AGENT = 'sender', 'me', 'agent'
BY_TEXT = {BY_SENDER: 'позначив відправник', BY_ME: 'позначили ви', BY_AGENT: 'позначило доручення агента'}

_PRIORITY_HIGH_RE = re.compile(r'^\s*[12]\b')


def from_headers(msg) -> str:
    """Заголовки вхідного листа (email.message.Message) → «important», якщо відправник позначив його важливим."""
    def head(name):
        return str(msg.get(name) or '').strip().lower()
    if head('Importance') == 'high' or head('Priority') == 'urgent' or head('X-MSMail-Priority') == 'high':
        return IMPORTANT
    return IMPORTANT if _PRIORITY_HIGH_RE.match(head('X-Priority')) else ''


def outgoing_headers(level) -> dict:
    """Заголовки надісланого листа «❗ Важливо» (Outlook, Thunderbird, Gmail показують позначку)."""
    if level not in (IMPORTANT, URGENT):
        return {}
    return {'Importance': 'high', 'X-Priority': '1 (Highest)', 'X-MSMail-Priority': 'High'}


def set_level(messages, level, by=BY_ME) -> int:
    """Рівень листам (людина). '' — зняти (і запам'ятати, що людина вирішила: доручення не поставить знову).
    → скільки листів змінено."""
    from email_assistant.models import EmailMessage
    if level not in LEVELS:
        raise ValueError('невідомий рівень важливості')
    pks = [m.pk for m in messages]
    return EmailMessage.objects.filter(pk__in=pks).exclude(importance=level, importance_by=by).update(
        importance=level, importance_by=by)


def raise_level(msg, level, by=BY_AGENT) -> bool:
    """Доручення агента: підняти рівень, якщо людина не вирішила інакше. → чи змінено."""
    from email_assistant.models import EmailMessage
    if level not in (IMPORTANT, URGENT) or msg.importance_by == BY_ME or RANK[level] <= RANK[msg.importance]:
        return False
    EmailMessage.objects.filter(pk=msg.pk).update(importance=level, importance_by=by)
    msg.importance, msg.importance_by = level, by
    return True


def folder_q() -> Q:
    """Лист у папці «⭐ Важливі»: зірочка або ❗ / ‼️."""
    return Q(is_starred=True) | Q(importance__in=(IMPORTANT, URGENT))


def describe(msg) -> str:
    """«‼️ Дуже важливо — позначили ви» — підказка позначки."""
    if not msg.importance:
        return ''
    by = BY_TEXT.get(msg.importance_by, '')
    return LABELS[msg.importance] + (f' — {by}' if by else '')
