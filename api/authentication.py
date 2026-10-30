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

        target = getattr(request, '_request', request)
        target.api_key_seen = api_key          # для журналу — навіть якщо далі відмова
        if api_key.expires_at and api_key.expires_at < timezone.now().date():
            target.api_log_note = 'ключ прострочений'
            raise AuthenticationFailed('Токен прострочений.')

        from .key_limits import client_ip, ip_allowed, is_preview
        if is_preview(request):
            # «👁 Приклад даних» з картки ключа: запит іде з самої Minerva — адреса програми й last_used ні до чого
            return (api_key, api_key)
        ip = client_ip(request)
        if not ip_allowed(api_key, ip):
            target.api_log_note = 'адреса не в «Дозволених IP» ключа'
            raise AuthenticationFailed(f'Цей ключ не дозволено використовувати з адреси {ip or "?"}.')

        # Оновлення last_used без зайвого SELECT
        APIKey.objects.filter(pk=api_key.pk).update(last_used=timezone.now())

        return (api_key, api_key)

    def authenticate_header(self, request):
        # Без цього DRF повертає 403 замість 401 для запитів без/з невірним ключем
        return 'Token'

