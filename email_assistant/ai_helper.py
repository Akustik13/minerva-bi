"""email_assistant/ai_helper.py — AI helpers for email operations."""
import logging

logger = logging.getLogger('email_assistant')


def _call_ai_direct(prompt: str, max_tokens: int = 1024) -> str:
    """Direct Anthropic API call — isolated per-request, no conversation history, no tools."""
    try:
        import anthropic
        from strategy.models import AISettings
        s = AISettings.get()
        if not s.anthropic_api_key:
            return ''
        client = anthropic.Anthropic(api_key=s.anthropic_api_key)
        from ai_assistant.router import choose_model
        model = choose_model(prompt)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{'role': 'user', 'content': prompt}],
        )
        return ''.join(b.text for b in response.content if hasattr(b, 'text'))
    except Exception as e:
        logger.error('_call_ai_direct error: %s', e)
        return ''


def generate_reply(thread_messages: list, account, user_profile=None) -> str:
    """Generate a reply draft based on thread context."""
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
    return _call_ai_direct(prompt) or ''


def translate_email(text: str, target_lang: str, user_profile=None) -> str:
    """Translate email body to target language."""
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
    return _call_ai_direct(prompt) or ''


def summarize_thread(messages: list, user_profile=None) -> str:
    """Return a 2-3 sentence summary of an email thread."""
    context = '\n\n'.join(
        f'{m.from_email}: {(m.body_text or "")[:300]}' for m in messages[-6:]
    )
    prompt = f'Зроби стислий підсумок цієї переписки (2-3 речення):\n\n{context}'
    return _call_ai_direct(prompt) or ''


def check_grammar(body_text: str, user_profile=None) -> dict:
    """Check and fix grammar/spelling in email body."""
    if not body_text or not body_text.strip():
        return {'ok': False, 'error': 'Текст порожній'}

    prompt = (
        'Перевір граматику, орфографію та пунктуацію цього тексту.\n'
        'Виправ всі помилки. Збережи стиль, тон та форматування.\n'
        'Поверни ТІЛЬКИ виправлений текст, без коментарів і пояснень.\n'
        'Якщо тексту не потрібні виправлення — поверни його без змін.\n\n'
        f'{body_text[:4000]}'
    )
    corrected = (_call_ai_direct(prompt) or '').strip()
    if corrected:
        return {'ok': True, 'corrected': corrected, 'changed': corrected != body_text.strip()}
    return {'ok': False, 'error': 'AI не зміг перевірити граматику'}


def generate_from_prompt(prompt: str, account=None, user_profile=None) -> dict:
    """Generate email subject + body from a natural-language description."""
    import json

    header = f'Ти пишеш від імені {account.from_header}.\n' if account else ''
    full_prompt = (
        f'{header}'
        f'Склади email за цим описом.\n'
        f'Поверни ТІЛЬКИ JSON (без markdown, без пояснень):\n'
        f'{{"subject": "тема листа", "body": "текст листа"}}\n\n'
        f'Опис: {prompt[:1000]}'
    )

    try:
        raw = (_call_ai_direct(full_prompt) or '').strip()
        if raw.startswith('```'):
            raw = raw.split('```', 2)[1]
            if raw.startswith('json'):
                raw = raw[4:]
        result = json.loads(raw)
        if isinstance(result, dict) and 'body' in result:
            return {'ok': True, 'subject': result.get('subject', ''), 'body': result.get('body', '')}
    except Exception as e:
        logger.error('AI generate_from_prompt error: %s', e)
    return {'ok': False, 'error': 'AI не зміг згенерувати лист'}


def extract_deadlines(body_text: str, user_profile=None) -> list:
    """Extract dates/deadlines from email body."""
    import json

    if not body_text or not body_text.strip():
        return []

    prompt = (
        'Проаналізуй текст листа і знайди всі дедлайни, терміни, конкретні дати зустрічей.\n'
        'Поверни JSON масив: [{"title":"...", "date":"YYYY-MM-DD HH:MM", "description":"..."}]\n'
        'Правила:\n'
        '- "title" — коротка назва події (до 60 символів)\n'
        '- "date" — точна дата в форматі YYYY-MM-DD HH:MM, якщо час невідомий — 09:00\n'
        '- "description" — цитата з листа (до 200 символів)\n'
        'Якщо конкретних дат немає — поверни []\n'
        'ТІЛЬКИ JSON, без пояснень.\n\n'
        f'Текст листа:\n{body_text[:3000]}'
    )

    try:
        raw = (_call_ai_direct(prompt) or '').strip()
        if raw.startswith('```'):
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
        result = json.loads(raw)
        if isinstance(result, list):
            return result
    except Exception as e:
        logger.error('AI extract_deadlines error: %s', e)
    return []


def generate_order_draft(order, customer, account, user_profile=None) -> str:
    """Generate a draft email body for a new sales order."""
    lines_text = ''
    try:
        lines = order.lines.all()[:10]
        lines_text = '\n'.join(
            f'- {ln.sku or "?"}: {ln.qty} шт. × {ln.unit_price} = {ln.total_price}'
            for ln in lines
        )
    except Exception:
        pass

    prompt = (
        f'Склади короткий ввічливий email клієнту про нове замовлення.\n'
        f'Мова: визнач по імені клієнта, якщо незрозуміло — українська.\n'
        f'Тільки тіло листа без теми, без підпису.\n\n'
        f'Клієнт: {customer.company or customer.name}\n'
        f'Номер замовлення: {getattr(order, "order_number", order.pk)}\n'
        f'Статус: {order.get_status_display() if hasattr(order, "get_status_display") else order.status}\n'
        f'Позиції:\n{lines_text or "(немає даних)"}'
    )
    return _call_ai_direct(prompt) or ''
