"""
suppliers/services/jobs.py — AI-дії постачальників у фоні (пошук в інтернеті, читання візитки, оцінка з листів,
розбір відповіді на запит): сторінка не чекає хвилину, а опитує стан. Стан — core.shared_cache (бачать усі
процеси gunicorn), лише власник завдання. Тести — BACKGROUND = False (одразу в тому самому потоці).
"""
import logging
import threading
import uuid

from django.db import close_old_connections

logger = logging.getLogger(__name__)

BACKGROUND = True
TTL = 3600


def _key(job_id):
    return f"suppliers-job:{job_id}"


def _set(job_id, **data):
    from core.shared_cache import cache
    state = cache.get(_key(job_id)) or {}
    state.update(data)
    cache.set(_key(job_id), state, TTL)
    return state


def start(user, title, fn, progress=False) -> str:
    """fn() → dict (результат для сторінки: {url, message} …). → id завдання. progress=True — fn(report): report(текст)
    показує людині, що зараз робиться («3 / 16 · PCBWay»)."""
    job_id = uuid.uuid4().hex
    _set(job_id, user=getattr(user, "pk", None), title=title, status="running", result=None, error="", progress="")

    def report(text):
        _set(job_id, progress=str(text or "")[:200])

    def run():
        try:
            result = (fn(report) if progress else fn()) or {}
            _set(job_id, status="done", result=result)
        except Exception as e:  # noqa: BLE001 — показати людині, а не впасти мовчки
            from suppliers.services.ai import AIError
            if not isinstance(e, AIError):
                logger.warning("suppliers job %s failed", title, exc_info=True)
            _set(job_id, status="failed", error=str(e)[:500] or type(e).__name__)
        finally:
            if BACKGROUND:
                close_old_connections()

    if BACKGROUND:
        threading.Thread(target=run, daemon=True, name=f"suppliers-{title[:20]}").start()
    else:
        run()
    return job_id


def state(job_id, user) -> dict | None:
    from core.shared_cache import cache
    s = cache.get(_key(str(job_id or "")))
    if not s or s.get("user") != getattr(user, "pk", None):
        return None
    return {k: v for k, v in s.items() if k != "user"}
