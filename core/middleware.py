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
        if self._should_block(request):
            from django.shortcuts import render
            return render(request, 'core/access_denied.html', status=403)
        return self.get_response(request)

    def _should_block(self, request):
        path = request.path
        if not path.startswith('/admin/'):
            return False
        for prefix in self._SKIP_PREFIXES:
            if path.startswith(prefix):
                return False
        parts = path.strip('/').split('/')
        # parts[0]='admin', parts[1]=app_label
        if len(parts) < 2:
            return False
        app_label = parts[1]
        if not app_label or app_label in self._ALWAYS_ALLOW:
            return False

        # Layer 1: global module flag — blocks everyone
        try:
            from core.models import ModuleRegistry
            if not ModuleRegistry.check_active(app_label):
                return True
        except Exception:
            pass

        # Layer 2+3: per-user allowed modules + denied model URLs
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return False
        try:
            profile = user.profile
            if profile.role == 'superadmin':
                return False
            allowed = profile.get_allowed_modules()
            # Layer 2: app-level
            if allowed != '__all__' and app_label not in allowed:
                return True
            # Layer 3: model-level — /admin/<app>/<model_slug>/
            if len(parts) >= 3 and parts[2]:
                model_slug = parts[2]
                for entry in (profile.denied_models or []):
                    if ':' not in entry:
                        continue
                    ent_app, ent_model = entry.split(':', 1)
                    if ent_app == app_label and ent_model.lower() == model_slug:
                        return True
            return False
        except Exception:
            return False  # fail open — no profile means full access


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
