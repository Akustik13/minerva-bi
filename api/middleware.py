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
