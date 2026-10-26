"""API для адмінки сайту магазину (/admin/ на сайті) — логіка в shop/site_admin.py.

POST /shop/staff/login/   {login, password, site}        — вхід персоналу (ключ сайту, прив'язаний до магазину)
POST /shop/site-events/   {user, action, fields[], site} — журнал змін на сайті (лише назви полів)
POST /shop/pair/          {code, site_url}               — код підключення → ключ API й секрет вебхука (без ключа)
GET  /shop/site-mail/                                    — пошта сайту з Minerva (shop/site_mail.py) + звіт сайту
"""
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from .models import APIKey


def _bound_shop(request):
    from shop.assistant import key_is_bound
    from shop.services import shop_for_key
    key = request.auth if isinstance(request.auth, APIKey) else None
    shop = shop_for_key(key)
    return shop if shop is not None and key_is_bound(shop, key) else None


def _error(e):
    return Response({"ok": False, "code": e.code, "message": e.message}, status=e.status)


class StaffLoginView(APIView):
    resource_scope = "products"
    action = "login"
    action_scopes = {"login": "read"}   # перевірка входу — дані Minerva не змінюються

    def post(self, request):
        from shop import site_admin
        shop = _bound_shop(request)
        if shop is None:
            return Response({"ok": False, "code": "no_shop", "message": "Ключ сайту не прив'язаний до магазину."},
                            status=status.HTTP_403_FORBIDDEN)
        d = request.data if isinstance(request.data, dict) else {}
        try:
            return Response(site_admin.staff_login(shop, str(d.get("login") or ""), str(d.get("password") or ""),
                                                   str(d.get("site") or "")))
        except site_admin.SiteAdminError as e:
            return _error(e)


class SiteEventView(APIView):
    resource_scope = "products"
    action = "event"
    action_scopes = {"event": "read"}   # журнал сайту — у Minerva нічого, крім запису журналу, не змінюється

    def post(self, request):
        from shop import site_admin
        shop = _bound_shop(request)
        if shop is None:
            return Response({"ok": False, "code": "no_shop"}, status=status.HTTP_403_FORBIDDEN)
        d = request.data if isinstance(request.data, dict) else {}
        fields = [str(f)[:60] for f in (d.get("fields") or []) if isinstance(f, str)][:60]
        site_admin.record_event(shop, str(d.get("site") or ""), str(d.get("user") or ""),
                                str(d.get("action") or "settings"), {"fields": fields} if fields else {})
        return Response({"ok": True}, status=status.HTTP_201_CREATED)


class SiteMailView(APIView):
    """
    GET /shop/site-mail/ — пошта сайту з Minerva (shop/site_mail.py): {managed, smtp{host, port, security, user,
    password}, from, from_name, reply_to, bcc[], notify[], confirmation}; managed=false — сайт лишається на своїх
    налаштуваннях. Параметри mail_* — звіт сайту про власні налаштування пошти (без пароля).
    """
    resource_scope = "products"

    def get(self, request):
        from shop import site_mail
        from shop.services import shop_for_key
        key = request.auth if isinstance(request.auth, APIKey) else None
        shop = shop_for_key(key)
        if shop is None:
            return Response({"detail": "Для ключа не налаштовано магазин.", "code": "no_shop"},
                            status=status.HTTP_404_NOT_FOUND)
        bound = _bound_shop(request) is not None
        if bound and request.query_params.get("mail_site"):
            site_mail.record_report(shop, request.query_params)
        return Response(site_mail.public_config(shop, key))


class PairThrottle(SimpleRateThrottle):
    """Підбір кодів: 10 спроб на годину з однієї адреси."""
    scope = "shop_pair"
    rate = "10/hour"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class PairView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [PairThrottle]

    def post(self, request):
        from shop import site_admin
        d = request.data if isinstance(request.data, dict) else {}
        try:
            return Response(site_admin.pair(str(d.get("code") or ""), str(d.get("site_url") or ""),
                                            request.build_absolute_uri("/")))
        except site_admin.SiteAdminError as e:
            return _error(e)
