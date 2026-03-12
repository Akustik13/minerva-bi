from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed


class APIKeyAuthentication(BaseAuthentication):
    """Аутентифікація через власну модель APIKey (замість rest_framework.authtoken)."""

    def authenticate(self, request):
        auth = request.META.get('HTTP_AUTHORIZATION', '')
        if not auth.startswith('Token '):
            return None
        key = auth[6:].strip()

        from .models import APIKey
        try:
            api_key = APIKey.objects.get(key=key, is_active=True)
        except APIKey.DoesNotExist:
            raise AuthenticationFailed('Невірний або неактивний токен.')

        if api_key.expires_at and api_key.expires_at < timezone.now().date():
            raise AuthenticationFailed('Токен прострочений.')

        # Оновлення last_used без зайвого SELECT
        APIKey.objects.filter(pk=api_key.pk).update(last_used=timezone.now())

        return (api_key, api_key)
