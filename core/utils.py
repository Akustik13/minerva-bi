"""core/utils.py — Role permissions + AI prompt builder."""

ROLE_PERMISSIONS = {
    'superadmin': {
        'modules': '__all__',
        'can_delete': True,
        'can_export': True,
    },
    'admin': {
        'modules': [
            'crm', 'strategy', 'sales', 'accounting', 'shipping', 'inventory',
            'tasks', 'bots', 'api', 'config', 'backup', 'auth', 'autoimport', 'core',
        ],
        'can_delete': True,
        'can_export': True,
    },
    'manager': {
        'modules': ['crm', 'strategy', 'sales', 'shipping'],
        'can_delete': False,
        'can_export': True,
    },
    'warehouse': {
        'modules': ['inventory', 'shipping'],
        'can_delete': False,
        'can_export': False,
    },
    'accountant': {
        'modules': ['sales', 'accounting', 'inventory'],
        'can_delete': False,
        'can_export': True,
    },
    'ai': {
        'modules': ['crm', 'strategy'],
        'can_delete': False,
        'can_export': False,
    },
}


def apply_role_defaults(profile):
    """Clear per-user module overrides so role defaults apply."""
    profile.allowed_modules = []
    profile.save(update_fields=['allowed_modules'])


def build_ai_system_prompt(profile, customer=None, strategy=None) -> str:
    """Build Anthropic-compatible system prompt from UserProfile AI settings."""
    base = profile.ai_system_prompt or (
        'Ти — AI-асистент системи Minerva Business Intelligence. '
        'Допомагаєш менеджерам з аналізом клієнтів, продажів та стратегіями CRM. '
        'Відповідай коротко і по суті українською мовою.'
    )

    parts = [base]

    if customer:
        try:
            rfm = customer.rfm_score()
            parts.append(
                f'\n\nКонтекст клієнта:\n'
                f'Назва: {customer.company or customer.name}\n'
                f'RFM: R={rfm["R"]} F={rfm["F"]} M={rfm["M"]}\n'
                f'Сегмент: {rfm["segment"]}'
            )
        except Exception:
            pass

    if strategy:
        parts.append(
            f'\n\nАктивна стратегія: {strategy.name or strategy.template}\n'
            f'Статус: {strategy.status}'
        )

    return ''.join(parts)
