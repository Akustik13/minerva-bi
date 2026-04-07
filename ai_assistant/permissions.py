from core.models import UserProfile

ROLE_TOOLS = {
    'viewer': [
        'get_system_overview',
        'get_inventory_status',
    ],
    'staff': [
        'get_system_overview',
        'get_inventory_status',
        'get_recent_orders',
        'get_customer_info',
        'get_sales_analytics',
    ],
    'manager': [
        'get_system_overview',
        'get_inventory_status',
        'get_recent_orders',
        'get_customer_info',
        'get_sales_analytics',
        'get_financial_overview',
        'compose_email',
        'get_email_strategy',
    ],
    'admin': [
        'get_system_overview',
        'get_inventory_status',
        'get_recent_orders',
        'get_customer_info',
        'get_sales_analytics',
        'get_financial_overview',
        'compose_email',
        'get_email_strategy',
        'send_email',
        'create_order',
        'update_inventory',
        'get_audit_log',
    ],
}


def get_profile_for_telegram(telegram_id: int):
    """Знайти UserProfile по telegram_id."""
    try:
        return UserProfile.objects.select_related('user').get(telegram_id=telegram_id)
    except UserProfile.DoesNotExist:
        return None


def get_allowed_tools(profile) -> list:
    """
    Повертає список allowed tool schemas для Anthropic API.
    Пріоритет: глобальні заборони AISettings > роль юзера
    """
    from ai_assistant.tools import ALL_TOOLS
    from strategy.models import AISettings

    settings = AISettings.get()
    role = profile.ai_assistant_role if profile else 'viewer'
    allowed_names = set(ROLE_TOOLS.get(role, ROLE_TOOLS['viewer']))

    # Глобальні заборони адміна
    if not settings.ai_allow_email_sending:
        allowed_names -= {'compose_email', 'send_email', 'get_email_strategy'}
    if not settings.ai_allow_order_creation:
        allowed_names -= {'create_order'}
    if not settings.ai_allow_inventory_edit:
        allowed_names -= {'update_inventory'}
    if not settings.ai_allow_financial_data:
        allowed_names -= {'get_financial_overview'}

    return [t for t in ALL_TOOLS if t['name'] in allowed_names]


def build_user_context(profile) -> str:
    """Контекстний рядок про юзера для system prompt."""
    if not profile:
        return 'Юзер: гість. Роль: viewer. Доступ обмежений.'

    lines = [
        f"Юзер: {profile.user.get_full_name() or profile.user.username}",
        f"Роль в системі: {profile.get_ai_assistant_role_display()}",
    ]
    if profile.notes:
        lines.append(f"Контекст: {profile.notes[:200]}")
    return '\n'.join(lines)
