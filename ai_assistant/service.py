"""
Core AI service — Anthropic tool-use loop.
"""
import anthropic
from .models import AIConversation, AIMessage
from .permissions import get_allowed_tools, get_profile_for_telegram
from .prompts import build_system_prompt
from .router import choose_model
from .budget_guard import check_budget, record_usage
from .tools import execute_tool

MAX_TOOL_ITERATIONS = 5


def _get_client() -> anthropic.Anthropic:
    from strategy.models import AISettings
    return anthropic.Anthropic(api_key=AISettings.get().anthropic_api_key)


def _get_or_create_conversation(profile, channel: str, telegram_chat_id: str = '') -> AIConversation:
    """Reuse active conversation or start fresh."""
    qs = AIConversation.objects.filter(
        user_profile=profile,
        channel=channel,
        is_active=True,
    )
    if telegram_chat_id:
        qs = qs.filter(telegram_chat_id=telegram_chat_id)

    conv = qs.first()
    if not conv:
        conv = AIConversation.objects.create(
            user_profile=profile,
            channel=channel,
            telegram_chat_id=telegram_chat_id,
        )
    return conv


def _build_history(conversation: AIConversation) -> list:
    """Convert stored messages to Anthropic format (last 20 pairs max).
    Tool messages are skipped — we only store final text replies, not the
    intermediate tool_use blocks, so tool_result blocks would be orphaned
    and cause API 400 errors.
    """
    messages = []
    for msg in conversation.messages.order_by('created_at').select_related()[:40]:
        if msg.role == 'user':
            messages.append({'role': 'user', 'content': msg.content})
        elif msg.role == 'assistant':
            messages.append({'role': 'assistant', 'content': msg.content})
        # 'tool' role skipped intentionally — see docstring
    return messages


def chat(
    user_text: str,
    profile=None,
    channel: str = 'webchat',
    telegram_chat_id: str = '',
    enable_web_search: bool = False,
) -> str:
    """
    Main entry point. Returns assistant reply text.
    Raises ValueError on budget exceeded.
    """
    # Budget check
    allowed, reason = check_budget(profile)
    if not allowed:
        if reason == 'monthly_budget_exceeded':
            return 'Мої сили на сьогодні вичерпані. Повернись завтра. 🏛️'
        if reason == 'daily_limit_exceeded':
            return 'Твій денний ліміт вичерпано. Повернись завтра. 🏛️'

    conversation = _get_or_create_conversation(profile, channel, telegram_chat_id)
    model = choose_model(user_text)
    tools = get_allowed_tools(profile)
    _use_web_search = enable_web_search
    if not _use_web_search:
        try:
            from strategy.models import AISettings
            _use_web_search = AISettings.get().enable_web_search_chat
        except Exception:
            pass
    if _use_web_search:
        tools = [{"type": "web_search_20250305", "name": "web_search"}] + list(tools)
    system_prompt = build_system_prompt(profile)

    # Save user message
    user_msg = AIMessage.objects.create(
        conversation=conversation,
        role='user',
        content=user_text,
    )

    # Build messages list
    history = _build_history(conversation)
    # Remove the last message we just saved (it's included in history)
    # Actually we build history from DB which now includes user_msg, so remove last entry
    # Better: build history before saving, then append
    # Rebuild without the just-saved message
    messages = []
    for msg in conversation.messages.exclude(pk=user_msg.pk).order_by('created_at')[:39]:
        if msg.role == 'user':
            messages.append({'role': 'user', 'content': msg.content})
        elif msg.role == 'assistant':
            messages.append({'role': 'assistant', 'content': msg.content})
        # 'tool' role skipped — orphaned tool_result blocks cause API 400
    messages.append({'role': 'user', 'content': user_text})

    client = _get_client()
    final_text = ''
    total_input = 0
    total_output = 0

    for iteration in range(MAX_TOOL_ITERATIONS):
        kwargs = dict(
            model=model,
            max_tokens=1024,
            system=system_prompt,
            messages=messages,
        )
        if tools:
            kwargs['tools'] = tools

        response = client.messages.create(**kwargs)
        total_input += response.usage.input_tokens
        total_output += response.usage.output_tokens

        if response.stop_reason == 'end_turn':
            # Extract text from content blocks
            for block in response.content:
                if hasattr(block, 'text'):
                    final_text += block.text
            break

        if response.stop_reason == 'tool_use':
            # Collect tool use blocks
            assistant_content = response.content
            messages.append({'role': 'assistant', 'content': assistant_content})

            tool_results = []
            for block in assistant_content:
                if block.type == 'tool_use':
                    if block.name == 'web_search':
                        # Server-side managed tool — Anthropic handles the search.
                        # We must return a tool_result to satisfy the API contract,
                        # but the actual results are injected by Anthropic's infrastructure.
                        tool_results.append({
                            'type': 'tool_result',
                            'tool_use_id': block.id,
                            'content': '',
                        })
                        continue
                    result = execute_tool(block.name, block.input, profile)
                    # Save tool message
                    AIMessage.objects.create(
                        conversation=conversation,
                        role='tool',
                        content=f'Tool: {block.name}',
                        tool_name=block.name,
                        tool_input={'tool_use_id': block.id, **block.input},
                        tool_result=result,
                    )
                    tool_results.append({
                        'type': 'tool_result',
                        'tool_use_id': block.id,
                        'content': str(result),
                    })

            messages.append({'role': 'user', 'content': tool_results})
        else:
            # Unexpected stop reason
            for block in response.content:
                if hasattr(block, 'text'):
                    final_text += block.text
            break

    if not final_text:
        final_text = 'Вибач, не вдалося отримати відповідь. Спробуй ще раз.'

    # Save assistant message
    assistant_msg = AIMessage.objects.create(
        conversation=conversation,
        role='assistant',
        content=final_text,
    )

    # Record usage
    record_usage(conversation, assistant_msg, model, total_input, total_output)

    return final_text


def generate_structured(prompt: str, profile=None) -> str:
    """
    Генерувати структурований JSON без Minerva-персонажа і без tools.
    Використовується для ai_suggest_strategy та інших JSON-only запитів.
    Повертає raw рядок (не зберігає в conversation history).
    """
    allowed, reason = check_budget(profile)
    if not allowed:
        return ''

    from strategy.models import AISettings
    s = AISettings.get()
    client = _get_client()
    model = choose_model(prompt)

    system = (
        'You are a JSON generator. '
        'Return ONLY valid JSON, no markdown, no explanations, no text before or after. '
        'Respond in Ukrainian for string values.'
    )

    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=system,
        messages=[{'role': 'user', 'content': prompt}],
    )

    text = ''
    for block in response.content:
        if hasattr(block, 'text'):
            text += block.text

    # Record usage against global monthly budget only (no conversation/message objects)
    try:
        from .budget_guard import calc_cost
        from .models import AIBudgetLog
        cost = calc_cost(model, response.usage.input_tokens, response.usage.output_tokens)
        budget_log = AIBudgetLog.current()
        budget_log.total_requests += 1
        budget_log.total_input_tokens += response.usage.input_tokens
        budget_log.total_output_tokens += response.usage.output_tokens
        budget_log.total_cost_usd += cost
        budget_log.save(update_fields=[
            'total_requests', 'total_input_tokens',
            'total_output_tokens', 'total_cost_usd',
        ])
    except Exception:
        pass

    return text


def reset_conversation(profile, channel: str = 'webchat', telegram_chat_id: str = ''):
    """Mark current conversation inactive — next message starts fresh."""
    qs = AIConversation.objects.filter(
        user_profile=profile,
        channel=channel,
        is_active=True,
    )
    if telegram_chat_id:
        qs = qs.filter(telegram_chat_id=telegram_chat_id)
    qs.update(is_active=False)
