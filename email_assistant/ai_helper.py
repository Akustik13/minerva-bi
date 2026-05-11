"""email_assistant/ai_helper.py — AI helpers for email operations."""
import logging

logger = logging.getLogger('email_assistant')


def generate_reply(thread_messages: list, account, user_profile=None) -> str:
    """Generate a reply draft based on thread context."""
    from ai_assistant.service import chat

    context_parts = []
    for msg in thread_messages[-8:]:
        direction = '← ОТРИМАНО' if msg.folder == 'inbox' else '→ НАДІСЛАНО'
        date_str = msg.sent_at.strftime('%d.%m.%Y') if msg.sent_at else '?'
        context_parts.append(
            f'{direction} від {msg.from_email} ({date_str}):\n'
            f'{(msg.body_text or "")[:800]}'
        )

    context = '\n\n---\n'.join(context_parts)
    prompt = (
        f'Ти — email асистент {account.from_header}.\n'
        f'Прочитай переписку і склади ТІЛЬКИ текст відповіді на останній лист. '
        f'Без теми, без підпису, тільки текст тіла листа.\n\n'
        f'ПЕРЕПИСКА:\n{context}\n\nВідповідь:'
    )

    try:
        return chat(prompt, profile=user_profile, channel='system_briefing') or ''
    except Exception as e:
        logger.error('AI reply generation error: %s', e)
        return ''


def translate_email(text: str, target_lang: str, user_profile=None) -> str:
    """Translate email body to target language."""
    from ai_assistant.service import chat

    lang_names = {
        'uk': 'українську',
        'de': 'німецьку',
        'en': 'англійську',
        'pl': 'польську',
        'ru': 'російську',
    }
    lang_name = lang_names.get(target_lang, target_lang)
    prompt = (
        f'Перекладі цей email на {lang_name}. '
        f'Поверни ТІЛЬКИ переклад, збережи форматування.\n\n'
        f'{text[:4000]}'
    )

    try:
        return chat(prompt, profile=user_profile, channel='system_briefing') or ''
    except Exception as e:
        logger.error('AI translate error: %s', e)
        return ''


def summarize_thread(messages: list, user_profile=None) -> str:
    """Return a 2-3 sentence summary of an email thread."""
    from ai_assistant.service import chat

    context = '\n\n'.join(
        f'{m.from_email}: {(m.body_text or "")[:300]}' for m in messages[-6:]
    )
    prompt = f'Зроби стислий підсумок цієї переписки (2-3 речення):\n\n{context}'

    try:
        return chat(prompt, profile=user_profile, channel='system_briefing') or ''
    except Exception as e:
        logger.error('AI summarize error: %s', e)
        return ''
