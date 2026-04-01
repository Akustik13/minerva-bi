"""core/utils.py — Role permissions + AI prompt builder."""

ROLE_DEFAULTS = {
    'superadmin': {
        'modules': '__all__',
        'can_delete': True,
        'can_export': True,
        'can_import': True,
        'can_view_audit': True,
    },
    'admin': {
        'modules': [
            'crm', 'strategy', 'sales', 'accounting', 'shipping', 'inventory',
            'tasks', 'bots', 'api', 'config', 'backup', 'auth', 'autoimport', 'core',
        ],
        'can_delete': True,
        'can_export': True,
        'can_import': True,
        'can_view_audit': True,
    },
    'manager': {
        'modules': ['crm', 'strategy', 'sales', 'shipping'],
        'can_delete': False,
        'can_export': True,
        'can_import': False,
        'can_view_audit': False,
    },
    'warehouse': {
        'modules': ['inventory', 'shipping'],
        'can_delete': False,
        'can_export': False,
        'can_import': True,
        'can_view_audit': False,
    },
    'accountant': {
        'modules': ['sales', 'accounting', 'inventory'],
        'can_delete': False,
        'can_export': True,
        'can_import': False,
        'can_view_audit': False,
    },
    'ai': {
        'modules': ['crm', 'strategy'],
        'can_delete': False,
        'can_export': False,
        'can_import': False,
        'can_view_audit': False,
    },
}

# Backward-compatibility alias
ROLE_PERMISSIONS = ROLE_DEFAULTS


def user_can(user, permission: str) -> bool:
    """
    Check if user has a given permission.

    Permission names match ROLE_DEFAULTS keys without 'can_' prefix
    (e.g. 'delete', 'export', 'import', 'view_audit').

    Priority:
      1. Superuser / superadmin role → always True
      2. UserProfile.can_<permission> field if not None
      3. ROLE_DEFAULTS fallback for the user's role
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    try:
        profile = user.profile
        if profile.role == 'superadmin':
            return True
        field_name = f'can_{permission}'
        if hasattr(profile, field_name):
            val = getattr(profile, field_name)
            if val is not None:
                return bool(val)
        defaults = ROLE_DEFAULTS.get(profile.role, {})
        return bool(defaults.get(field_name, False))
    except Exception:
        return False


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
