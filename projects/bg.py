"""Довгі дії AI проєкту у фоні — чернетка плану з листування (ai_plan) і моменти й ризики з листів (ai_scan).

Проксі NAS чекає відповіді ≈ 60 с, а Claude з роздумами відповідає довше — синхронний запит закінчувався HTTP 504.
Тепер POST лише запускає роботу в потоці й одразу відповідає, браузер опитує стан (GET). Стан — core.shared_cache
(спільний для процесів gunicorn): {} — не запускали, {running, started}, {done, result}, {error}. Хто й що
запускає (права, бюджет) — перевіряє той, хто викликає start. BACKGROUND = False — без потоку (тести).
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

BACKGROUND = True
TTL = 3600
STALE_TEXT = "AI не відповів вчасно — спробуйте ще раз"


def state(key, stale_seconds) -> dict:
    """Стан роботи; «складає» довше за stale_seconds — потік загинув (перезапуск процесу), можна знову."""
    from core.shared_cache import cache
    st = cache.get(key) or {}
    if st.get("running") and time.time() - st.get("started", 0) > stale_seconds:
        return {"error": STALE_TEXT}
    return st


def start(key, stale_seconds, work, label) -> dict:
    """Запустити work() → dict (лягає в стан як result); ValueError — текст помилки людині. Уже йде — нічого
    нового. → state."""
    from django.db import connection
    from core.shared_cache import cache
    st = state(key, stale_seconds)
    if st.get("running"):
        return st
    cache.set(key, {"running": True, "started": time.time()}, TTL)

    def run():
        try:
            cache.set(key, {"done": True, "result": work() or {}}, TTL)
        except ValueError as e:
            cache.set(key, {"error": str(e)}, TTL)
        except Exception as e:  # noqa: BLE001 — помилку побачить людина, а не потік
            logger.exception("%s: %s", label, e)
            cache.set(key, {"error": f"Не вдалося ({type(e).__name__}) — спробуйте ще раз"}, TTL)
        finally:
            if BACKGROUND:
                connection.close()

    if BACKGROUND:
        threading.Thread(target=run, daemon=True).start()
    else:
        run()
    return state(key, stale_seconds)
