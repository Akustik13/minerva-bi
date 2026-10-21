"""email_assistant/ai_helper.py — AI helpers for email operations."""
import logging
import re
import threading

logger = logging.getLogger('email_assistant')

# Моделі покоління 5.x: «роздуми» входять у max_tokens — даємо запас, щоб відповідь не обрізалась
_THINKING_MODELS = {'claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-fable-5-1'}
# Відмова моделі з міркувань безпеки → сервер сам повторює запит на запасній моделі
_FALLBACK_MODELS = {'claude-opus-5-5', 'claude-sonnet-5-5', 'claude-fable-5-1'}
_FALLBACK_BETA = 'server-side-fallback-2026-07-01'

_last = threading.local()   # яка модель відповіла востаннє (для «Minerva врахувала: … · модель»)


def _user(account=None, user_profile=None):
    if account is not None and getattr(account, 'user_id', None):
        return account.user
    return getattr(user_profile, 'user', None)


def resolve_model(prompt: str, user=None) -> str:
    """Модель з «Налаштувань пошти» користувача; «Автоматично» — роутер Minerva AI."""
    if user is not None:
        from email_assistant.models import EmailSettings
        chosen = EmailSettings.get_for_user(user).ai_model
        if chosen and chosen != EmailSettings.AI_MODEL_AUTO:
            return chosen
    from ai_assistant.router import choose_model
    return choose_model(prompt)


def last_model() -> str:
    return getattr(_last, 'model', '')


def _call_ai_direct(prompt: str, max_tokens: int = 1024, user=None) -> str:
    """Direct Anthropic API call — isolated per-request, no conversation history, no tools."""
    _last.model = ''
    try:
        import anthropic
        from strategy.models import AISettings
        s = AISettings.get()
        if not s.anthropic_api_key:
            return ''
        client = anthropic.Anthropic(api_key=s.anthropic_api_key)
        model = resolve_model(prompt, user)
        params = {
            'model': model,
            'max_tokens': max(max_tokens, 16000) if model in _THINKING_MODELS else max_tokens,
            'messages': [{'role': 'user', 'content': prompt}],
        }
        if model in _FALLBACK_MODELS:
            fallback = {'extra_headers': {'anthropic-beta': _FALLBACK_BETA},
                        'extra_body': {'fallbacks': 'default'}}
            try:
                response = client.messages.create(**params, **fallback)
            except anthropic.BadRequestError as e:   # акаунт/платформа без fallbacks — звичайний запит
                logger.warning('AI fallbacks rejected (%s), retry without: %s', model, e)
                response = client.messages.create(**params)
        else:
            response = client.messages.create(**params)
        if response.stop_reason == 'refusal':
            logger.warning('AI refusal (%s): %s', model, getattr(response, 'stop_details', None))
            return ''
        _last.model = getattr(response, 'model', '') or model
        return ''.join(b.text for b in response.content if getattr(b, 'type', '') == 'text')
    except Exception as e:
        logger.error('_call_ai_direct error: %s', e)
        return ''


def _thread_text(thread_messages, account, per_message=1500, last=8, newest_chars=8000) -> str:
    """Переписка для промпту; найновіший лист — майже повністю (терміни часто в кінці пересланого листа)."""
    parts = []
    own = (account.email_address or '').lower()
    recent = thread_messages[-last:]
    for i, msg in enumerate(recent):
        limit = newest_chars if i == len(recent) - 1 else per_message
        outgoing = msg.folder == 'sent' or (msg.from_email or '').lower() == own
        direction = '→ НАДІСЛАЛИ МИ' if outgoing else '← ОТРИМАЛИ'
        date_str = msg.sent_at.strftime('%d.%m.%Y %H:%M') if msg.sent_at else '?'
        parts.append(f'{direction} · {date_str} · від {msg.from_email} · тема: {msg.subject or "—"}\n'
                     f'{(msg.body_text or "")[:limit]}')
    return '\n\n---\n'.join(parts)


# Прощальна фраза наприкінці чернетки: підпис користувача з прощанням додається сам
_CLOSING_RE = re.compile(
    r'^((best|kind|warm|warmest)\s+)?(regards|wishes)\b|^sincerely|^yours (truly|sincerely|faithfully)|^cheers\b'
    r'|^thanks? (you )?(and|&) (best |kind )?regards|^mit (freundlichen|besten) grüßen|^(freundliche|viele|beste|liebe) grüße'
    r'|^mfg\b|^з повагою|^з найкращими побажаннями|^cordialement|^saludos|^pozdrawiam|^distinti saluti',
    re.IGNORECASE)


def strip_signoff(text: str) -> str:
    """Прибрати «Best regards,\nViacheslav» в кінці (якщо після прощання лише коротке ім'я/посада)."""
    lines = (text or '').rstrip().split('\n')
    for i in range(max(0, len(lines) - 4), len(lines)):
        if _CLOSING_RE.match(lines[i].strip()):
            tail = [ln for ln in lines[i + 1:] if ln.strip()]
            if all(len(ln.split()) <= 5 for ln in tail):
                return '\n'.join(lines[:i]).rstrip()
    return (text or '').rstrip()


def generate_reply(thread_messages: list, account, user_profile=None, instruction: str = '',
                   facts: str = '', recipient: str = '') -> str:
    """
    Чернетка відповіді на останній лист гілки.
    facts — досьє з Minerva (email_assistant/context.py): замовлення, відправлення, клієнт, інші листування;
    instruction — що саме написати (поле «Що відповісти?»).
    """
    task = instruction.strip() or 'Склади доречну відповідь на останній лист співрозмовника.'
    prompt = (
        f'Ти — email-асистент {account.from_header} (компанія відправника листів «→ НАДІСЛАЛИ МИ»).\n'
        f'Напиши ТІЛЬКИ текст тіла листа-відповіді: без теми, без підпису.\n\n'
        f'ПРАВИЛА:\n'
        f'- Мова відповіді — та сама, якою написав співрозмовник в останньому листі, '
        f'якщо в завданні не сказано інакше.\n'
        f'- Номери замовлень, трек-номери, дати, статуси, суми бери ЛИШЕ з розділу «ФАКТИ З MINERVA» '
        f'або з переписки. Нічого не вигадуй; якщо потрібного факту немає — напиши [уточнити].\n'
        f'- Факти з Minerva новіші за переписку: якщо статус змінився, спирайся на Minerva.\n'
        f'- Пиши коротко, ввічливо, по суті; без внутрішніх подробиць, які клієнту не потрібні.\n'
        f'- Звичайний текст листа, без Markdown (без #, **, таблиць).\n'
        f'- НЕ пиши прощальну фразу (Best regards, Mit freundlichen Grüßen, З повагою) та ім\'я відправника '
        f'в кінці: підпис із прощанням додається автоматично.\n'
        f'- Терміни (дедлайни) бери з тексту листів, а не з часу подій перевізника.\n'
        f'- Якщо у фактах є «ОГЛЯД ПРОЄКТУ»: відповідь не має суперечити нашим обіцянкам; на відкриті питання '
        f'співрозмовника відповідай, якщо відповідь є у фактах, інакше напиши [уточнити]; нових обіцянок від нас '
        f'(строків, цін, знижок) понад завдання не давай.\n\n'
        + (f'АДРЕСАТ ЛИСТА: {recipient}. Пиши саме йому (якщо останній лист — внутрішнє '
           f'пересилання від колеги, звертайся не до колеги, а до цього адресата).\n\n' if recipient else '')
        + (f'ФАКТИ З MINERVA (достовірні дані з бази):\n{facts}\n\n' if facts else '')
        + f'ПЕРЕПИСКА (від старих до нових):\n{_thread_text(thread_messages, account)}\n\n'
        f'ЗАВДАННЯ: {task}\n\nТекст відповіді:'
    )
    return strip_signoff(_call_ai_direct(prompt, max_tokens=1500, user=_user(account, user_profile)) or '')


def summarize_history(thread_messages: list, account, facts: str = '') -> str:
    """Коротко (українською): про що мова, що вже зроблено, що зараз потрібно зробити."""
    prompt = (
        'Ти допомагаєш менеджеру швидко зрозуміти ситуацію за листуванням і даними з системи Minerva.\n'
        'Напиши українською 3–6 коротких пунктів:\n'
        '1) про що мова (клієнт, замовлення, товар);\n'
        '2) що вже відбулося (ключові дати й дії);\n'
        '3) поточний стан (статус замовлення/доставки);\n'
        '4) що зараз потрібно зробити нам і до якого терміну, якщо він є.\n'
        'Спирайся лише на факти нижче; нічого не вигадуй. Терміни бери з тексту листів '
        '(напр. «Antwort erforderlich bis …»), а не з часу подій перевізника.\n'
        'Формат: звичайний текст без Markdown (без #, ** і таблиць), пункти 1)–4).\n\n'
        + (f'ФАКТИ З MINERVA:\n{facts}\n\n' if facts else '')
        + f'ПЕРЕПИСКА:\n{_thread_text(thread_messages, account, per_message=1000)}'
    )
    return _call_ai_direct(prompt, max_tokens=700, user=_user(account)) or ''


def translate_email(text: str, target_lang: str, user_profile=None) -> str:
    """Translate email body to target language."""
    lang_names = {
        'uk': 'українську',
        'de': 'німецьку',
        'en': 'англійську',
        'pl': 'польську',
        'fr': 'французьку',
        'es': 'іспанську',
        'it': 'італійську',
        'cs': 'чеську',
        'ru': 'російську',
    }
    lang_name = lang_names.get(target_lang, target_lang)
    prompt = (
        f'Переклади цей email на {lang_name} мову (ділове листування). '
        f'Поверни ТІЛЬКИ переклад: без пояснень і без Markdown, збережи абзаци, '
        f'імена, номери замовлень, трек-номери, суми й дати без змін.\n\n'
        f'{text[:12000]}'
    )
    return _call_ai_direct(prompt, user=_user(user_profile=user_profile)) or ''


def summarize_thread(messages: list, user_profile=None) -> str:
    """Return a 2-3 sentence summary of an email thread."""
    context = '\n\n'.join(
        f'{m.from_email}: {(m.body_text or "")[:300]}' for m in messages[-6:]
    )
    prompt = f'Зроби стислий підсумок цієї переписки (2-3 речення):\n\n{context}'
    return _call_ai_direct(prompt, user=_user(user_profile=user_profile)) or ''


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
    corrected = (_call_ai_direct(prompt, user=_user(user_profile=user_profile)) or '').strip()
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
        raw = (_call_ai_direct(full_prompt, user=_user(account, user_profile)) or '').strip()
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
    return _call_ai_direct(prompt, user=_user(user_profile=user_profile)) or ''
