from rest_framework.throttling import SimpleRateThrottle

from .models import APIKey


class APIKeyRateThrottle(SimpleRateThrottle):
    """Ліміт запитів на один API-ключ (rate: REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']['apikey'])."""

    scope = 'apikey'

    def get_cache_key(self, request, view):
        if isinstance(request.auth, APIKey):
            return self.cache_format % {'scope': self.scope, 'ident': request.auth.pk}
        return None  # сесія адміна — без ліміту
