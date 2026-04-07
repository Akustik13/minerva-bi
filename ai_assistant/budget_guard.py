from decimal import Decimal
from django.utils import timezone


PRICE_PER_INPUT_TOKEN = {
    'claude-haiku-4-5-20251001': Decimal('0.00000025'),
    'claude-sonnet-4-6':         Decimal('0.000003'),
}
PRICE_PER_OUTPUT_TOKEN = {
    'claude-haiku-4-5-20251001': Decimal('0.00000125'),
    'claude-sonnet-4-6':         Decimal('0.000015'),
}


def calc_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    pi = PRICE_PER_INPUT_TOKEN.get(model, Decimal('0.000003'))
    po = PRICE_PER_OUTPUT_TOKEN.get(model, Decimal('0.000015'))
    return pi * input_tokens + po * output_tokens


def check_budget(profile) -> tuple[bool, str]:
    """
    Returns (allowed: bool, reason: str).
    Checks monthly global budget AND per-user daily limit.
    """
    from strategy.models import AISettings
    from .models import AIBudgetLog

    settings = AISettings.get()

    # Monthly global budget
    budget_log = AIBudgetLog.current()
    if budget_log.total_cost_usd >= settings.monthly_budget_usd:
        return False, 'monthly_budget_exceeded'

    # Per-user daily limit
    if profile and settings.per_user_daily_limit_usd > 0:
        today = timezone.now().date()
        from .models import AIMessage
        day_cost = AIMessage.objects.filter(
            conversation__user_profile=profile,
            created_at__date=today,
        ).aggregate(
            s=__import__('django.db.models', fromlist=['Sum']).Sum('cost_usd')
        )['s'] or Decimal('0')
        if day_cost >= settings.per_user_daily_limit_usd:
            return False, 'daily_limit_exceeded'

    return True, 'ok'


def record_usage(conversation, message_obj, model: str, input_tokens: int, output_tokens: int):
    """Update conversation + monthly budget totals."""
    from .models import AIBudgetLog

    cost = calc_cost(model, input_tokens, output_tokens)
    message_obj.input_tokens = input_tokens
    message_obj.output_tokens = output_tokens
    message_obj.cost_usd = cost
    message_obj.model_used = model
    message_obj.save(update_fields=['input_tokens', 'output_tokens', 'cost_usd', 'model_used'])

    # Conversation totals
    conversation.total_input_tokens += input_tokens
    conversation.total_output_tokens += output_tokens
    conversation.total_cost_usd += cost
    conversation.save(update_fields=['total_input_tokens', 'total_output_tokens', 'total_cost_usd'])

    # Monthly budget log
    budget_log = AIBudgetLog.current()
    budget_log.total_requests += 1
    budget_log.total_input_tokens += input_tokens
    budget_log.total_output_tokens += output_tokens
    budget_log.total_cost_usd += cost
    budget_log.save(update_fields=['total_requests', 'total_input_tokens', 'total_output_tokens', 'total_cost_usd'])

    # User profile stats
    if conversation.user_profile:
        p = conversation.user_profile
        p.ai_total_requests += 1
        p.ai_total_spent_usd += cost
        p.ai_last_active = timezone.now()
        p.save(update_fields=['ai_total_requests', 'ai_total_spent_usd', 'ai_last_active'])

    # Alert if threshold crossed
    _maybe_send_budget_alert(budget_log)


def _maybe_send_budget_alert(budget_log):
    from strategy.models import AISettings
    settings = AISettings.get()

    if budget_log.alert_sent:
        return
    if budget_log.total_cost_usd < settings.alert_threshold_usd:
        return

    budget_log.alert_sent = True
    budget_log.save(update_fields=['alert_sent'])

    if not settings.budget_alert_telegram_id:
        return

    try:
        import requests
        token = settings.telegram_bot_token
        chat_id = settings.budget_alert_telegram_id
        text = (
            f"⚠️ Minerva AI: витрати перевищили поріг\n"
            f"Витрачено: ${budget_log.total_cost_usd:.2f} / ${settings.monthly_budget_usd:.2f}"
        )
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=5,
        )
    except Exception:
        pass
