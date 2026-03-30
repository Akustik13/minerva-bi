"""core/middleware.py — Audit + Module access middleware."""


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

    _ALWAYS_ALLOW = frozenset({'core', 'auth', 'admin', 'authtoken'})
    _SKIP_PREFIXES = (
        '/admin/login', '/admin/logout', '/admin/jsi18n',
        '/admin/r/', '/static/', '/media/',
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
        try:
            from core.models import ModuleRegistry
            return not ModuleRegistry.check_active(app_label)
        except Exception:
            return False  # fail open
