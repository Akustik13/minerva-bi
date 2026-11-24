"""🤖 MCP-сервер Minerva — `/api/v1/mcp/` (протокол і інструменти — agent/mcp.py).

Підключення: Claude Code `claude mcp add --transport http minerva https://<minerva>/api/v1/mcp/ --header
"Authorization: Bearer <ключ>"` (працює й «Token <ключ>»). Ключ — «REST API → API Ключі»: права `agent:read` (читання)
і `agent:write` (пропозиції), «🤖 Агент (MCP) — від імені кого» — людина, яка перевіряє й застосовує пропозиції.
Ключ сайту магазину — ні. Обмеження ключа (IP, ліміти) і журнал запитів — як для решти API.
🔌 Або OAuth (api/oauth.py) — для програм без поля для ключа (Claude.ai «Custom connector», ChatGPT…): без
Authorization — 401 з WWW-Authenticate resource_metadata, далі програма сама реєструється й просить «Дозволити»;
токен «Bearer mnv_at_…» → ключ підключення.
"""
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from rest_framework.exceptions import ParseError, PermissionDenied
from rest_framework.renderers import JSONRenderer
from rest_framework.views import APIView

from .authentication import APIKeyAuthentication
from .models import APIKey


class MCPKeyAuthentication(APIKeyAuthentication):
    """Клієнти MCP шлють «Authorization: Bearer <ключ>» — приймаємо його нарівні з «Token <ключ>»."""

    def authenticate(self, request):
        from rest_framework.exceptions import AuthenticationFailed
        from . import oauth
        auth = request.META.get('HTTP_AUTHORIZATION', '')
        if auth.startswith('Bearer ' + oauth.ACCESS_PREFIX):
            try:
                key = oauth.authenticate(request, auth[7:].strip())
            except oauth.OAuthError as e:
                request._oauth_error = e.error
                raise AuthenticationFailed(e.description or e.error)
            from .key_limits import client_ip, ip_allowed
            ip = client_ip(request)
            if not ip_allowed(key, ip):
                raise AuthenticationFailed(f'Це підключення не дозволено використовувати з адреси {ip or "?"}.')
            APIKey.objects.filter(pk=key.pk).update(last_used=timezone.now())
            return (key, key)
        if auth.startswith('Bearer '):
            request.META['HTTP_AUTHORIZATION'] = 'Token ' + auth[7:]
        return super().authenticate(request)

    def authenticate_header(self, request):
        """401 → де метадані OAuth (RFC 9728): програма без ключа сама знайде, куди йти по «Дозволити»."""
        from . import oauth
        return oauth.www_authenticate(request, getattr(request, '_oauth_error', ''))


class MCPView(APIView):
    """MCP-сервер для зовнішніх агентів (Claude Code, Claude Desktop): JSON-RPC 2.0 — initialize, tools/list,
    tools/call. Читання — `agent:read`, пропозиції (propose_*) — `agent:write`; застосовує людина ключа ✓."""
    authentication_classes = [MCPKeyAuthentication]
    renderer_classes = [JSONRenderer]
    resource_scope = 'agent'
    action_scopes = {'read': 'read', 'write': 'write'}
    action = 'read'

    def initial(self, request, *args, **kwargs):
        from agent.mcp import needs_write
        if request.method == 'POST':
            try:
                self.action = 'write' if needs_write(request.data) else 'read'
            except ParseError:
                self.action = 'read'
        super().initial(request, *args, **kwargs)

    def _key_user(self, request):
        key = request.auth
        if not isinstance(key, APIKey):
            raise PermissionDenied('Потрібен ключ API (Authorization: Bearer <ключ>).')
        if key.shop_id:
            raise PermissionDenied('Ключ сайту магазину не підходить для MCP.')
        user = key.agent_user
        if user is None or not user.is_active:
            raise PermissionDenied('У ключа не вказано «🤖 Агент (MCP) — від імені кого».')
        if not user.has_perm('agent.add_agenttask'):
            raise PermissionDenied('Людина ключа не має права на завдання агенту.')
        return key, user

    def post(self, request):
        from agent import mcp
        key, user = self._key_user(request)
        session = mcp.valid_session(request.headers.get('Mcp-Session-Id'))
        base = request.build_absolute_uri('/').rstrip('/')
        reply, new_session = mcp.handle(request.data, key=key, user=user, session=session, review_base=base)
        if reply is None:
            return HttpResponse(status=202)
        response = JsonResponse(reply, safe=False, json_dumps_params={'ensure_ascii': False})
        if new_session:
            response['Mcp-Session-Id'] = new_session
        return response

    def get(self, request):
        """Потоку подій (SSE) немає — кожна відповідь приходить на свій POST."""
        self._key_user(request)
        response = HttpResponse(status=405)
        response['Allow'] = 'POST, DELETE'
        return response

    def delete(self, request):
        """Кінець сесії — завдання з пропозиціями лишається для перевірки."""
        self._key_user(request)
        return HttpResponse(status=204)
