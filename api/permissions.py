from rest_framework.permissions import BasePermission, SAFE_METHODS
from .models import APIKey


class HasAPIKeyScope(BasePermission):
    """
    APIKey auth  → перевіряє scopes: {resource}:read / {resource}:write
    Session auth → тільки безпечні методи GET/HEAD/OPTIONS (browsable API)
    """

    def has_permission(self, request, view):
        auth = request.auth

        if isinstance(auth, APIKey):
            resource = getattr(view, 'resource_scope', None)
            if not resource:
                return True
            scope = f"{resource}:{'read' if request.method in SAFE_METHODS else 'write'}"
            return auth.has_scope(scope)

        # Session auth (browsable API) — read only for staff
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.is_staff
            and request.method in SAFE_METHODS
        )
