from rest_framework.exceptions import Throttled
from rest_framework.throttling import BaseThrottle, SimpleRateThrottle

from .models import APIKey


class APIKeyRateThrottle(SimpleRateThrottle):
    """Ліміт запитів на один API-ключ за хвилину: свій у ключа («Запитів за хвилину»)
    або загальний (rate: REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']['apikey'])."""

    scope = 'apikey'

    def allow_request(self, request, view):
        from .key_limits import is_preview
        if is_preview(request):
            return True                          # «👁 Приклад даних» — не рахується в ліміт ключа
        key = request.auth if isinstance(request.auth, APIKey) else None
        if key is not None and key.limit_minute:
            self.num_requests, self.duration = key.limit_minute, 60
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        if isinstance(request.auth, APIKey):
            return self.cache_format % {'scope': self.scope, 'ident': request.auth.pk}
        return None  # сесія адміна — без ліміту


class APIKeyQuotaThrottle(BaseThrottle):
    """«Запитів за годину / добу» ключа (api/key_limits.check_quota) — 429 із зрозумілою причиною."""

    def allow_request(self, request, view):
        key = request.auth if isinstance(request.auth, APIKey) else None
        if key is None or not (key.limit_hour or key.limit_day):
            return True
        from .key_limits import check_quota, is_preview
        if is_preview(request):
            return True
        ok, wait, text = check_quota(key)
        if not ok:
            getattr(request, '_request', request).api_log_note = text
            raise Throttled(wait=wait, detail=f"{text} Повторіть через {wait // 60 + 1} хв.")
        return True
