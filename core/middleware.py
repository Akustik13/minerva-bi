"""core/middleware.py — Audit + Module access middleware."""
# Future: TenantMiddleware will be enabled after multi-tenant migration


class AuditMiddleware:
    """
    Lightweight placeholder. Actual audit logging happens via:
    - AuditableMixin (admin create/update/delete)
    - core.signals (_on_login / _on_logout)
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)


def _url_to_operation(parts: list) -> str:
    """Map URL parts to operation name (view/add/change/delete)."""
    # /admin/<app>/<model>/add/                 → add
    # /admin/<app>/<model>/<id>/change/         → change
    # /admin/<app>/<model>/<id>/delete/         → delete
    # /admin/<app>/<model>/                     → view
    last = parts[-1] if parts else ''
    if last in ('add', 'change', 'delete'):
        return last
    return 'view'


class ModuleAccessMiddleware:
    """
    Block access to disabled modules.
    Checks /admin/<app_label>/ URLs against ModuleRegistry.
    Returns 403 access_denied page if module is inactive.
    """

    _ALWAYS_ALLOW = frozenset({'core', 'auth', 'admin', 'authtoken', 'faq', 'dashboard'})
    _SKIP_PREFIXES = (
        '/admin/login', '/admin/logout', '/admin/jsi18n',
        '/admin/r/', '/static/', '/media/',
        '/admin/password_', '/accounts/', '/onboarding/',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        block, ctx = self._check_access(request)
        if block:
            from django.shortcuts import render
            return render(request, 'core/access_denied.html', ctx, status=403)
        return self.get_response(request)

    def _check_access(self, request):
        """Returns (should_block: bool, context: dict)."""
        path = request.path
        _empty = {}
        if not path.startswith('/admin/'):
            return False, _empty
        for prefix in self._SKIP_PREFIXES:
            if path.startswith(prefix):
                return False, _empty
        parts = path.strip('/').split('/')
        if len(parts) < 2:
            return False, _empty
        app_label = parts[1]
        if not app_label or app_label in self._ALWAYS_ALLOW:
            return False, _empty

        # Layer 1: global module flag — blocks everyone
        try:
            from core.models import ModuleRegistry
            if not ModuleRegistry.check_active(app_label):
                return True, {
                    'app_label': app_label,
                    'module_disabled': True,
                    'reason': None,
                }
        except Exception:
            pass

        # Layer 2+3: per-user allowed modules + denied model URLs
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return False, _empty
        if user.is_superuser:
            return False, _empty
        try:
            profile = user.profile
            if profile.role in ('superadmin', 'admin'):
                return False, _empty
            allowed = profile.get_allowed_modules()
            # Layer 2: app-level
            if allowed != '__all__' and app_label not in allowed:
                return True, {
                    'app_label': app_label,
                    'module_disabled': False,
                    'reason': (
                        f'Ваша роль ({profile.get_role_display()}) '
                        f'не має доступу до розділу «{app_label}».'
                    ),
                }
            # Layer 3: model-level — /admin/<app>/<model_slug>/
            if len(parts) >= 3 and parts[2]:
                model_slug = parts[2]
                for entry in (profile.denied_models or []):
                    if ':' not in entry:
                        continue
                    ent_app, ent_model = entry.split(':', 1)
                    if ent_app == app_label and ent_model.lower() == model_slug:
                        return True, {
                            'app_label': f'{app_label}.{model_slug}',
                            'module_disabled': False,
                            'reason': (
                                f'Ваша роль ({profile.get_role_display()}) '
                                f'не має доступу до цієї моделі.'
                            ),
                        }

            # Layer 4: operation-level (only if explicit module_operations set)
            if profile.module_operations is not None:
                operation = _url_to_operation(parts)
                ops = profile.get_allowed_operations(app_label)
                if ops != '__all__' and operation not in (ops or []):
                    op_labels = {
                        'view': 'перегляд', 'add': 'створення',
                        'change': 'редагування', 'delete': 'видалення',
                        'export': 'експорт', 'import': 'імпорт',
                    }
                    return True, {
                        'app_label': app_label,
                        'module_disabled': False,
                        'reason': (
                            f'Ваша роль ({profile.get_role_display()}) не має права '
                            f'«{op_labels.get(operation, operation)}» у модулі «{app_label}».'
                        ),
                    }

            return False, _empty
        except Exception:
            return False, _empty  # fail open


class TenantMiddleware:
    """
    Stub: future multi-tenant middleware.
    Will attach request.tenant based on subdomain or session.
    Currently a no-op pass-through.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Stub — tenant resolution will be added here
        request.tenant = None
        return self.get_response(request)
