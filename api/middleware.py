import time

from django.http import HttpResponse

API_PREFIX = "/api/v1/"


class APICorsMiddleware:
    """
    CORS лише для /api/v1/: дозволяє викликати API з будь-якого сайту чи локального
    HTML-файлу (api/examples/api_tester.html). Авторизація там — ключ у заголовку
    Authorization; cookie не дозволені (без Allow-Credentials), тож сесія адмінки
    іншому сайту недоступна.
    """

    HEADERS = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PATCH, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept",
        "Access-Control-Expose-Headers": "Retry-After",
        "Access-Control-Max-Age": "86400",
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(API_PREFIX):
            return self.get_response(request)
        if request.method == "OPTIONS" and "HTTP_ACCESS_CONTROL_REQUEST_METHOD" in request.META:
            response = HttpResponse(status=204)  # preflight — без авторизації
        else:
            response = self.get_response(request)
        for k, v in self.HEADERS.items():
            response[k] = v
        return response


class APIRequestLogMiddleware:
    """Журнал запитів до /api/v1/ з ключем (і спроб із невідомим ключем): хто, звідки, що, чим відповіли.
    Що записувати — «Журнал запитів» ключа; логіка — api/key_limits.record."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(API_PREFIX) or request.method == "OPTIONS":
            return self.get_response(request)
        started = time.monotonic()
        response = self.get_response(request)
        from .key_limits import is_preview
        if is_preview(request):
            return response                       # «👁 Приклад даних» з картки ключа — не дія програми, не пишемо
        auth = request.META.get("HTTP_AUTHORIZATION", "")
        key = getattr(request, "api_key_seen", None)
        if key is None and not auth.startswith("Token "):
            return response                       # без ключа (вебхук Stripe, сесія адмінки) — не пишемо
        hint = ""
        if key is None:
            from core.secrets import mask
            hint = mask(auth[6:].strip()) if auth[6:].strip() else "порожній"
        from .key_limits import record
        record(request, response, started, key=key, key_hint=hint)
        return response
