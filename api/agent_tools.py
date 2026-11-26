"""🤖 Інструменти агента (MCP) звичайним REST — для програм і скриптів, яким не потрібен протокол MCP.

    GET  /agent/tools/          — інструменти, які може цей ключ (як MCP tools/list: name, description, inputSchema,
                                  annotations.readOnlyHint)
    POST /agent/tools/{name}/   — виклик: тіло — аргументи інструмента (JSON-об'єкт, або {"arguments": {…}}) →
                                  {tool, text, is_error, review_url}

Те саме, що MCP `tools/call` (agent/mcp.py `call`): ті самі права людини ключа («🤖 Агент (MCP) — від імені кого»),
🔒 особисте (пошта, календар, робочий час) — лише особистим ключем; читання — `agent:read`, propose_* і upload_* —
`agent:write` (`mcp.key_write`). propose_* нічого не змінює: пропозиції лягають у завдання ключа на сьогодні
(«🧩 Завдання агенту» → ✓ людини), `review_url` — сторінка перевірки. Відповідь — текст інструмента (ДАНІ Minerva).
"""
from rest_framework import status
from rest_framework.exceptions import ParseError
from rest_framework.response import Response
from rest_framework.views import APIView

from .files import _person
from .mcp import MCPKeyAuthentication


class AgentToolsView(APIView):
    """GET — інструменти агента, доступні цьому ключу (як MCP tools/list)."""
    authentication_classes = [MCPKeyAuthentication]
    resource_scope = 'agent'

    def get(self, request):
        from agent import mcp
        key, user, _personal = _person(request)
        return Response({'tools': mcp.tool_defs(user, key)})


class AgentToolCallView(APIView):
    """POST — виклик інструмента агента: тіло — його аргументи; читання — agent:read, propose_* / upload_* — agent:write."""
    authentication_classes = [MCPKeyAuthentication]
    resource_scope = 'agent'
    action_scopes = {'read': 'read', 'write': 'write'}
    action = 'read'

    def initial(self, request, *args, **kwargs):
        from agent.mcp import key_write
        self.action = 'write' if key_write(kwargs.get('name')) else 'read'
        super().initial(request, *args, **kwargs)

    def post(self, request, name):
        from agent import mcp
        key, user, _personal = _person(request)
        try:
            body = request.data
        except ParseError:
            return Response({'error': 'bad_request', 'message': 'тіло — JSON'}, status=status.HTTP_400_BAD_REQUEST)
        if hasattr(body, 'dict') and not isinstance(body, dict):
            body = body.dict()
        args = body.get('arguments', body) if isinstance(body, dict) else body
        if not isinstance(args, dict):
            return Response({'error': 'bad_request', 'message': 'аргументи — JSON-об\'єкт'},
                            status=status.HTTP_400_BAD_REQUEST)
        if name not in mcp.names(user, key):
            return Response({'error': 'unknown_tool', 'message': f'інструмента {name} немає або ключу він недоступний'},
                            status=status.HTTP_404_NOT_FOUND)
        base = request.build_absolute_uri('/').rstrip('/')
        reply = mcp.call(None, {'name': name, 'arguments': args}, key, user, '', base)
        result = reply.get('result') or {}
        content = result.get('content') or []
        text = '\n'.join(c.get('text', '') for c in content if c.get('type') == 'text')
        review = ''
        if mcp.is_write(name) and not result.get('isError'):
            task = mcp.task_for(key, user, '')
            if task is not None:
                review = f'{base}/admin/agent/agenttask/{task.pk}/review/'
        if 'error' in reply:
            return Response({'error': 'tool_error', 'message': reply['error'].get('message', '')},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'tool': name, 'text': text, 'is_error': bool(result.get('isError')),
                         'review_url': review, 'extra': [c for c in content if c.get('type') != 'text']})
