from rest_framework.permissions import BasePermission, SAFE_METHODS
from .models import APIKey


def required_scope(request, view):
    """
    {resource}:read для GET/HEAD/OPTIONS, {resource}:write для решти.
    view.action_scopes = {'check': 'read'} — перевизначення для окремих дій
    (напр. POST /stock/check/ лише читає залишки).
    """
    resource = getattr(view, 'resource_scope', None)
    if not resource:
        return None
    mode = (getattr(view, 'action_scopes', None) or {}).get(getattr(view, 'action', None))
    if mode is None:
        mode = 'read' if request.method in SAFE_METHODS else 'write'
    return f"{resource}:{mode}"


class HasAPIKeyScope(BasePermission):
    """
    APIKey auth  → перевіряє scopes: {resource}:read / {resource}:write
    Session auth → тільки безпечні методи GET/HEAD/OPTIONS (browsable API)
    """

    def has_permission(self, request, view):
        auth = request.auth

        if isinstance(auth, APIKey):
            scope = required_scope(request, view)
            if not scope:
                return True
            if auth.has_scope(scope):
                return True
            self.message = f"Ключ не має права «{scope}». Додайте його в Адмін → REST API → API Ключі."
            return False

        # Session auth (browsable API) — read only for staff
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.is_staff
            and request.method in SAFE_METHODS
        )
