"""🤖 MCP-сервер Minerva: ті самі інструменти, що в агента завдань, для зовнішніх агентів (Claude Code, Claude Desktop…).

HTTP — api/mcp.py (`/api/v1/mcp/`, ключ API з `agent:read` / `agent:write` і «🤖 Агент (MCP) — від імені кого»).
Протокол — JSON-RPC 2.0 у Streamable HTTP без SSE (кожна відповідь — JSON): initialize (→ Mcp-Session-Id),
notifications/* (→ 202), ping, tools/list, tools/call.

• Інструменти — `TOOL_DEFS`: читати товари, даташити, локації, магазини й DigiKey і пропонувати (propose_*) зміни
  товару, прихід, новий товар, подію, магазин, ціни / строк DigiKey. Листів (read_mail, propose_email) і пошуку в
  інтернеті немає: пошта — лише її власника, а адресатів людина тут не називала. Викликати можна лише ці
  (`call` перевіряє назву, хоч що надішле клієнт).
• Пропозиції стають завданням людини ключа (`AgentTask`: `api_key`, `mcp_session` — одне на сесію, без сесії — одне
  на добу) у стані «👀 Чекає вашої перевірки»: застосовує вона ✓ на сторінці завдання (agent/decide.py). Відповідь
  інструмента містить адресу цієї сторінки.
"""
import json
import logging
import re
import uuid

from django.utils import timezone

from agent.models import AgentTask

logger = logging.getLogger(__name__)

PROTOCOLS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
SERVER = {'name': 'minerva', 'title': 'Minerva', 'version': '1.0'}
INSTRUCTIONS = (
    'Minerva — облік складу й продажів. Інструменти читають товари, даташити, локації, магазини й DigiKey; '
    'propose_* лише ПРОПОНУЮТЬ зміни — людина перевіряє їх і застосовує ✓ у Minerva («🧩 Завдання агенту»). '
    'Кожна пропозиція — з підставою (точна цитата) і джерелом. Ціни на DigiKey — лише за правилами Minerva.')
SESSION_RE = re.compile(r'^[A-Za-z0-9-]{8,80}$')
CLIENT_TTL = 24 * 3600


def tool_defs() -> list:
    from agent import channels, tools
    base = [tools.FIND, tools.GET, tools.READ, tools.CHANGE, tools.ATTR, tools.LOCATIONS, tools.STOCK_IN,
            tools.NEW_PRODUCT, tools.EVENT] + channels.TOOLS
    return [{'name': t['name'], 'description': t['description'], 'inputSchema': t['input_schema'],
             'annotations': {'readOnlyHint': not is_write(t['name']), 'destructiveHint': False}} for t in base]


def names() -> set:
    return {t['name'] for t in tool_defs()}


def is_write(name) -> bool:
    return str(name or '').startswith('propose_')


def needs_write(payload) -> bool:
    """Чи є в запиті виклик propose_* (тоді ключу потрібне agent:write, інакше вистачить agent:read)."""
    msgs = payload if isinstance(payload, list) else [payload]
    return any(isinstance(m, dict) and m.get('method') == 'tools/call'
               and is_write((m.get('params') or {}).get('name')) for m in msgs)


# ── Сесія → завдання ──────────────────────────────────────────────────────────

def _client_key(session) -> str:
    return f'agent-mcp-client:{session}'


def remember_client(session, info):
    from core.shared_cache import cache
    name = ' '.join(str((info or {}).get('name') or 'MCP-клієнт').split())[:60]
    cache.set(_client_key(session), name, CLIENT_TTL)
    return name


def client_name(session) -> str:
    from core.shared_cache import cache
    return (cache.get(_client_key(session)) if session else None) or 'MCP-клієнт'


def task_for(key, user, session, create=False):
    """Завдання для пропозицій цієї сесії (без сесії — одне на добу); create=False — лише якщо вже є."""
    slot = session if session else f'day:{timezone.localdate():%Y-%m-%d}'
    task = AgentTask.objects.filter(owner=user, api_key=key, mcp_session=slot).first()
    if task is None and create:
        client = client_name(session)
        task = AgentTask.objects.create(
            owner=user, api_key=key, mcp_session=slot, status=AgentTask.REVIEW,
            title=f'MCP · {client} · {timezone.localtime():%d.%m %H:%M}'[:200],
            instruction=f'Пропозиції зовнішнього агента через MCP: клієнт «{client}», ключ API «{key.name}». '
                        f'Перевірте кожну й застосуйте ✓.')
    return task


# ── JSON-RPC ─────────────────────────────────────────────────────────────────

def _error(mid, code, message):
    return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': code, 'message': message}}


def _result(mid, result):
    return {'jsonrpc': '2.0', 'id': mid, 'result': result}


def handle(payload, *, key, user, session='', review_base=''):
    """Тіло POST → (відповідь або None для лише сповіщень, новий Mcp-Session-Id або '')."""
    if isinstance(payload, list):
        if not payload:
            return _error(None, -32600, 'порожній пакет'), ''
        out, new = [], ''
        for msg in payload:
            reply, created = _one(msg, key, user, session, review_base)
            new = new or created
            if reply is not None:
                out.append(reply)
        return (out or None), new
    return _one(payload, key, user, session, review_base)


def _one(msg, key, user, session, review_base):
    if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or not isinstance(msg.get('method'), str):
        return _error(msg.get('id') if isinstance(msg, dict) else None, -32600, 'не JSON-RPC 2.0'), ''
    method, mid, params = msg['method'], msg.get('id'), msg.get('params') or {}
    if 'id' not in msg:                                   # сповіщення (notifications/initialized…) — без відповіді
        return None, ''
    if method == 'initialize':
        asked = str(params.get('protocolVersion') or '')
        new = uuid.uuid4().hex
        remember_client(new, params.get('clientInfo'))
        return _result(mid, {'protocolVersion': asked if asked in PROTOCOLS else PROTOCOLS[1],
                             'capabilities': {'tools': {'listChanged': False}},
                             'serverInfo': SERVER, 'instructions': INSTRUCTIONS}), new
    if method == 'ping':
        return _result(mid, {}), ''
    if method == 'tools/list':
        return _result(mid, {'tools': tool_defs()}), ''
    if method == 'tools/call':
        return call(mid, params, key, user, session, review_base), ''
    return _error(mid, -32601, f'невідомий метод {method}'), ''


def call(mid, params, key, user, session, review_base):
    from agent import tools
    name = str(params.get('name') or '')
    args = params.get('arguments') or {}
    if name not in names():
        return _error(mid, -32602, f'невідомий інструмент {name}')
    if not isinstance(args, dict):
        return _error(mid, -32602, 'arguments — об\'єкт')
    task = task_for(key, user, session, create=is_write(name))
    if task is None:
        task = AgentTask(owner=user)                      # лише читання — завдання не потрібне
    before = task.proposals.count() if task.pk else 0
    text, is_error = tools.run(tools.Context(task), name, args)
    if task.pk:
        if task.proposals.count() > before and task.status != AgentTask.REVIEW:
            AgentTask.objects.filter(pk=task.pk).update(status=AgentTask.REVIEW)
        if is_write(name) and not is_error:
            text += f'\nЛюдина перевірить і застосує тут: {review_base}/admin/agent/agenttask/{task.pk}/review/'
        _log(task, name, args, text, is_error)
    return _result(mid, {'content': [{'type': 'text', 'text': text}], 'isError': bool(is_error)})


def _log(task, name, args, text, is_error):
    from agent.tasks import _brief, _log as log
    log(task, f'{"⚠️" if is_error else "🔌"} {name}({_brief(args)}) → {text[:200]}')


def valid_session(value) -> str:
    value = str(value or '').strip()
    return value if SESSION_RE.match(value) else ''


def dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False)
