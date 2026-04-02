"""
core/auth_backend.py — Login by username OR email.
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
