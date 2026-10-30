"""
dashboard/schedule_guard.py — запобіжник для звітів і попереджень за розкладом (cron_runner.sh).

Звіт, який не вдалося надіслати (помилка SMTP / Telegram), розклад повторює не одразу, а через
RETRY_MINUTES і не більше MAX_FAILURES разів на день: так помилка, після якої лист усе ж пішов
(обрив з'єднання після відправки), не перетворюється на лист щохвилини. Стан — у спільному кеші
(core.shared_cache), окремо для кожного звіту й дня. Кнопки «… зараз» в адмінці запобіжник не зупиняє.
"""
import time

from django.utils import timezone

from core.shared_cache import cache

RETRY_MINUTES = 30
MAX_FAILURES = 3


def _key(name: str) -> str:
    return f"schedule-fail:{name}:{timezone.localdate().isoformat()}"


def may_try(name: str) -> bool:
    """Чи можна зараз спробувати надіслати звіт `name` після сьогоднішніх невдач."""
    state = cache.get(_key(name)) or {}
    if state.get("n", 0) >= MAX_FAILURES:
        return False
    return time.time() - state.get("at", 0) >= RETRY_MINUTES * 60


def note_failure(name: str) -> None:
    key = _key(name)
    state = cache.get(key) or {}
    cache.set(key, {"n": state.get("n", 0) + 1, "at": time.time()}, 2 * 24 * 3600)


def clear(name: str) -> None:
    cache.delete(_key(name))


def channel_errors(result: dict) -> list:
    """Помилки каналів з результату send_digest / run_alerts ({'email': {'error': …}, …})."""
    return [f"{ch}: {info['error']}" for ch, info in (result or {}).items()
            if isinstance(info, dict) and info.get("error")]
