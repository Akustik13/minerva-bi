"""Стиснення сторінок gzip (settings.MIDDLEWARE, першим).

HTML пошти й списку замовлень — 0,7–2 МБ: через інтернет до NAS це секунди лише на передачу; gzip стискає їх у
10–15 разів. Стискаються лише готові текстові відповіді (HTML, JSON, JS, CSS, текст). Потокові відповіді (NDJSON
«синхронізувати всю пошту» — прогрес має йти одразу), файли, картинки й PDF — без змін. Захист від BREACH —
у самому Django (GZipMiddleware додає випадкові байти до 4.2+).
"""
from django.middleware.gzip import GZipMiddleware

COMPRESSIBLE = {
    "text/html", "text/plain", "text/css", "text/javascript", "text/csv", "text/xml",
    "application/javascript", "application/json", "application/xml", "image/svg+xml",
}


class PageGZipMiddleware(GZipMiddleware):
    def process_response(self, request, response):
        if response.streaming:
            return response
        ctype = response.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype not in COMPRESSIBLE:
            return response
        return super().process_response(request, response)
