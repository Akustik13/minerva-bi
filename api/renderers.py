from rest_framework.renderers import JSONRenderer

from .models import APIKey


class KeyJSONRenderer(JSONRenderer):
    """JSON з обмеженнями ключа: «Лише ці поля», «Різних записів за добу» (api/key_limits.shape)."""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        ctx = renderer_context or {}
        request, response = ctx.get("request"), ctx.get("response")
        key = getattr(request, "auth", None)
        if isinstance(key, APIKey) and response is not None and response.status_code < 400 \
                and (key.fields or key.items_per_day):
            from .key_limits import shape
            data = shape(key, data, request, response)
        return super().render(data, accepted_media_type, renderer_context)
