ANALYTICS_KEYWORDS = [
    'аналіз', 'аналітика', 'порівняй', 'звіт', 'статистика',
    'топ', 'найбільше', 'прогноз', 'рекомендація', 'стратегія',
    'analyse', 'bericht', 'report', 'strategy', 'recommend',
    'vergleich', 'übersicht',
]


def choose_model(message: str) -> str:
    """Haiku для простих запитів, Sonnet для аналітики."""
    if any(k in message.lower() for k in ANALYTICS_KEYWORDS):
        return 'claude-sonnet-4-6'
    return 'claude-haiku-4-5-20251001'
