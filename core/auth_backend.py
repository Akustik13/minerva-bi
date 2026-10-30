"""
core/auth_backend.py — Login by username OR email; has_perm — за ролями Minerva (core/permissions.py).
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

User = get_user_model()


class EmailOrUsernameBackend(ModelBackend):
    """
    Authenticate with either username or email (case-insensitive email).
    Falls back to standard ModelBackend behaviour for everything else.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None

        # Try exact username first
        user = self._get_by_username(username)

        # If not found — try email (case-insensitive)
        if user is None:
            user = self._get_by_email(username)

        if user is None:
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

    def has_perm(self, user_obj, perm, obj=None):
        """Стандартні права Django (групи, права людини) — як завжди; інакше — модель доступу Minerva."""
        if super().has_perm(user_obj, perm, obj):
            return True
        from core.permissions import has_perm
        return has_perm(user_obj, perm)

    def has_module_perms(self, user_obj, app_label):
        if super().has_module_perms(user_obj, app_label):
            return True
        from core.permissions import has_module_perms
        return has_module_perms(user_obj, app_label)

    @staticmethod
    def _get_by_username(username):
        try:
            return User.objects.get(username=username)
        except User.DoesNotExist:
            return None

    @staticmethod
    def _get_by_email(email):
        try:
            return User.objects.get(email__iexact=email)
        except (User.DoesNotExist, User.MultipleObjectsReturned):
            return None
