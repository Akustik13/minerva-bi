"""🔔 Нарада розшифрована / не вдалося — лист і Telegram тому, хто записав (раз, на першому кінцевому стані:
review будь-яким шляхом або failed, і з прибирання теж). Лише дата, код проєкту й кількість пунктів — без назви,
підсумку, цитат і тексту розшифровки. Доставка — agent.notify.deliver (SMTP «Налаштування → Сповіщення», бот
Telegram на Telegram ID профілю); канали — «⚙️ Налаштування нарад».

Відправка — у фоні після commit (BACKGROUND; тести ставлять False і отримують її одразу).
"""
import logging
import threading

from django.db import connection, transaction
from django.utils import timezone

from .models import Meeting, MeetingItem, MeetingSettings

logger = logging.getLogger(__name__)

BACKGROUND = True
LINK_TEXT = "Відкрити нараду"


def _plural(n, one, few, many) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def schedule(meeting_pk):
    """Після commit — повідомити власника, якщо ще не повідомляли. У фоні чи одразу — як BACKGROUND у мить виклику
    (manage.py meetings_cleanup вимикає фон: команда завершується, і фоновий потік зник би разом із нею)."""
    background = BACKGROUND
    transaction.on_commit(lambda: _start(meeting_pk, background))


def _start(meeting_pk, background=None):
    if not (BACKGROUND if background is None else background):
        _safe(meeting_pk)
        return

    def run():
        try:
            _safe(meeting_pk)
        finally:
            connection.close()          # власне з'єднання фонового потоку
    threading.Thread(target=run, daemon=True).start()


def _safe(meeting_pk):
    try:
        return ready(meeting_pk)
    except Exception as e:  # noqa: BLE001 — повідомлення не ламає обробку наради
        logger.warning("meeting %s notify: %s", meeting_pk, e)
        return ""


def compose(m) -> dict:
    """{subject, head, lines, summary, link} — лише дата, код проєкту, кількість пунктів."""
    from .services import page_path, site_link
    day = f"{m.day:%d.%m.%Y}"
    code = f" ({m.project.code})" if m.project_id else ""
    link = site_link(page_path(m))
    if m.status == Meeting.FAILED:
        return {"subject": "⚠️ Запис наради не вдалося обробити",
                "head": f"⚠️ Запис наради від {day}{code} не вдалося обробити. Відкрийте сторінку наради — там "
                        f"написано, що робити далі.",
                "lines": [], "summary": "", "link": link}
    pending = m.items.filter(status=MeetingItem.PENDING).count()
    lines = []
    if m.moments_error:
        head = f"🎙 Нарада від {day}{code} розшифрована, але ключові моменти знайти не вдалося."
        if pending:
            lines.append(f"Позначок під час запису чекають перевірки: {pending}")
    elif pending:
        word = _plural(pending, "пункт", "пункти", "пунктів")
        verb = "чекає" if _plural(pending, 1, 2, 2) == 1 else "чекають"
        head = f"🎙 Нарада від {day}{code} розшифрована — {pending} {word} {verb} перевірки (✓ / ✕)."
    else:
        head = f"🎙 Нарада від {day}{code} розшифрована — ключових моментів не знайдено."
    return {"subject": "🎙 Розшифровка наради готова", "head": head, "lines": lines, "summary": "", "link": link}


def ready(meeting_or_pk, now=None) -> str:
    """Атомарно забрати право повідомити (notified_at) і надіслати. → «🔔 Повідомлено…» / «🔕…» / ''."""
    now = now or timezone.now()
    pk = getattr(meeting_or_pk, "pk", meeting_or_pk)
    claimed = Meeting.objects.filter(pk=pk, notified_at__isnull=True,
                                     status__in=(Meeting.REVIEW, Meeting.FAILED)).update(notified_at=now)
    if not claimed:
        return ""
    m = Meeting.objects.select_related("owner", "owner__profile", "project").filter(pk=pk).first()
    if m is None or m.owner is None or not m.owner.is_active:
        return ""
    s = MeetingSettings.load()
    if not (s.notify_email or s.notify_telegram):
        return ""
    try:
        from agent.notify import deliver
    except ImportError:
        return ""
    return deliver(m.owner, compose(m), LINK_TEXT, mail=s.notify_email, telegram=s.notify_telegram)
