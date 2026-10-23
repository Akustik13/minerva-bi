"""
Фото й даташити товарів для сайту (логіка — shop/media.py).

  GET  shop/media/<sha256>/   перевірена копія файлу; лише товари, видимі в магазині ключа    (products:read)
  POST shop/media/status/     {have: [sha256…], failed: {sha256: текст}, sync_url}               (products:read)
                              — що сайт уже зберіг у себе; Minerva показує це на сторінці «🖼 Фото й даташити»

Що забирати — поле media у GET /shop/products/ ({image, datasheet}: sha256, url, …; null — вимкнено).
"""
from django.core.files.storage import default_storage
from django.http import FileResponse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import APIKey


def _shop(request):
    from shop.services import shop_for_key
    return shop_for_key(request.auth if isinstance(request.auth, APIKey) else None)


class MediaFileView(APIView):
    resource_scope = "products"

    def get(self, request, sha):
        from shop import media
        shop = _shop(request)
        row = media.file_for(shop, sha) if shop else None
        if row is None:
            return Response({"detail": "Файл не знайдено.", "code": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        response = FileResponse(default_storage.open(row.stored, "rb"), content_type=row.content_type)
        response["X-Content-SHA256"] = row.sha256
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, max-age=86400, immutable"   # адреса = SHA-256 вмісту
        return response


class MediaStatusView(APIView):
    resource_scope = "products"
    action = "report"
    action_scopes = {"report": "read"}   # сайт звітує про власний кеш — дані Minerva не змінюються

    def post(self, request):
        from shop import media
        shop = _shop(request)
        if shop is None:
            return Response({"detail": "Для ключа не налаштовано магазин.", "code": "no_shop"},
                            status=status.HTTP_404_NOT_FOUND)
        if not isinstance(request.data, dict):
            return Response({"detail": "Очікується JSON-об'єкт.", "code": "invalid"}, status=status.HTTP_400_BAD_REQUEST)
        return Response(media.record_report(shop, request.data))
