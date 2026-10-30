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
    Session auth → тільки безпечні методи GET/HEAD/OPTIONS (browsable API): адміністратор — усе, інші — моделі,
                   які вони бачать (`has_perm view_<модель>` = ролі Minerva, core/permissions.py)
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

        # Session auth (browsable API) — лише читання і лише те, що людина бачить в адмінці
        user = request.user
        if not (user and user.is_authenticated and user.is_active and user.is_staff
                and request.method in SAFE_METHODS):
            return False
        from core.utils import is_minerva_admin
        if is_minerva_admin(user):
            return True
        model = _model_of(view)
        if model is None:
            return False
        return user.has_perm(f"{model._meta.app_label}.view_{model._meta.model_name}")


def _model_of(view):
    queryset = getattr(view, "queryset", None)
    if queryset is None:
        try:
            queryset = view.get_queryset()
        except Exception:  # noqa: BLE001 — представлення без моделі
            return None
    return getattr(queryset, "model", None)
