"""core/utils.py — Role permissions + AI prompt builder + thread-local user."""
import threading as _threading

_current_user_local = _threading.local()


def set_current_user(user):
    """Store current HTTP request user for use in signals/async code."""
    _current_user_local.user = user


def get_current_user():
    """Return the user stored by set_current_user(), or None."""
    return getattr(_current_user_local, 'user', None)


def clear_current_user():
    _current_user_local.user = None


def is_minerva_admin(user) -> bool:
    """
    True if user is Django superuser OR has superadmin/admin Minerva role.
    Use instead of bare `user.is_superuser` checks in admin permission methods.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    try:
        return user.profile.role in ('superadmin', 'admin')
    except Exception:
        return False


ROLE_DEFAULTS = {
    'superadmin': {
        'modules': '__all__',
        'can_delete': True,
        'can_export': True,
        'can_import': True,
        'can_view_audit': True,
        'can_manage_users': True,
    },
    'admin': {
        'modules': '__all__',
        'can_delete': True,
        'can_export': True,
        'can_import': True,
        'can_view_audit': True,
        'can_manage_users': True,
    },
    'manager': {
        'modules': ['crm', 'strategy', 'sales', 'shipping', 'dashboard', 'tasks',
                    'email_assistant', 'calendar_app', 'faq', 'labels_app'],
        'can_delete': False,
        'can_export': True,
        'can_import': False,
        'can_view_audit': False,
        'can_manage_users': False,
    },
    'warehouse': {
        'modules': ['inventory', 'shipping', 'labels_app', 'dashboard', 'tasks',
                    'calendar_app', 'faq'],
        'can_delete': False,
        'can_export': False,
        'can_import': True,
        'can_view_audit': False,
        'can_manage_users': False,
    },
    'accountant': {
        'modules': ['sales', 'accounting', 'inventory', 'dashboard', 'faq'],
        'can_delete': False,
        'can_export': True,
        'can_import': False,
        'can_view_audit': False,
        'can_manage_users': False,
    },
    'ai': {
        'modules': ['crm', 'strategy', 'dashboard', 'faq'],
        'can_delete': False,
        'can_export': False,
        'can_import': False,
        'can_view_audit': False,
        'can_manage_users': False,
    },
    'readonly': {
        'modules': ['dashboard', 'faq'],
        'can_delete': False,
        'can_export': False,
        'can_import': False,
        'can_view_audit': False,
        'can_manage_users': False,
    },
}

# Backward-compatibility alias
ROLE_PERMISSIONS = ROLE_DEFAULTS

# ── Granular operations per role ───────────────────────────────────────────────
# Operations: view, add, change, delete, export, import
ALL_OPS = ['view', 'add', 'change', 'delete', 'export', 'import']

OP_LABELS = {
    'view':   'Перегляд',
    'add':    'Створення',
    'change': 'Редагування',
    'delete': 'Видалення',
    'export': 'Експорт',
    'import': 'Імпорт',
}

ROLE_OPERATIONS = {
    'superadmin': '__all__',
    'admin':      '__all__',
    'manager': {
        'crm':        ['view', 'add', 'change', 'export'],
        'sales':      ['view', 'add', 'change', 'export'],
        'shipping':   ['view', 'add', 'change'],
        'tasks':      ['view', 'add', 'change', 'delete'],
        'strategy':   ['view', 'add', 'change'],
        'dashboard':  ['view'],
        'faq':        ['view'],
        'labels_app': ['view', 'add'],
    },
    'warehouse': {
        'inventory':  ['view', 'add', 'change', 'import'],
        'shipping':   ['view', 'add', 'change'],
        'labels_app': ['view', 'add'],
        'dashboard':  ['view'],
        'faq':        ['view'],
    },
    'accountant': {
        'sales':      ['view', 'export'],
        'accounting': ['view', 'add', 'change', 'export'],
        'inventory':  ['view', 'export'],
        'dashboard':  ['view'],
        'faq':        ['view'],
    },
    'ai': {
        'crm':       ['view'],
        'strategy':  ['view'],
        'dashboard': ['view'],
        'faq':       ['view'],
    },
    'readonly': {
        'dashboard': ['view'],
        'faq':       ['view'],
    },
}


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
        if profile.role in ('superadmin', 'admin'):
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


def user_has_operation(user, app_label: str, operation: str) -> bool:
    """
    Check if user has a given operation for an app module.
    Operations: view, add, change, delete, export, import
    Returns True if allowed, False if denied.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    try:
        profile = user.profile
        if profile.role in ('superadmin', 'admin'):
            return True
        ops = profile.get_allowed_operations(app_label)
        if ops == '__all__':
            return True
        return operation in (ops or [])
    except Exception:
        return True  # fail open


def apply_role_defaults(profile) -> None:
    """Reset allowed_modules to None so role/bundle defaults apply automatically."""
    profile.allowed_modules = None


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
