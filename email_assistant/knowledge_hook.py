"""Повідомити базу знань (rag_assistant/knowledge.py) про зміни, зроблені масовим .update() без сигналів.

Збій бази знань ніколи не ламає дію в пошті; нічна звірка (knowledge_refresh) добирає пропущене.
"""


def touch(pks=(), thread_id=None):
    pks = [p for p in pks if p]
    if not pks and not thread_id:
        return
    try:
        from rag_assistant.knowledge import safe_touch
    except ImportError:
        return
    safe_touch(pks=pks, thread_id=thread_id)
