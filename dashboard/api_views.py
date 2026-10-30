"""
dashboard/api_views.py — API Overview + Developer Console proxy
"""
import json
import time

from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.utils import is_minerva_admin


# ── API Overview ──────────────────────────────────────────────────────────────

def api_index(request):
    context = admin.site.each_context(request)

    # Internal REST API
    from api.models import APIKey
    api_keys = APIKey.objects.filter(is_active=True)
    api_keys_count = api_keys.count()

    # Shipping carriers
    from shipping.models import Carrier
    jumingo_carrier = Carrier.objects.filter(carrier_type='jumingo', is_active=True).first()
    dhl_carrier     = Carrier.objects.filter(carrier_type='dhl', is_active=True).first()
    dhl_track_carrier = (Carrier.objects
                         .filter(carrier_type='dhl', is_active=True)
                         .exclude(api_key='')
                         .first())
    ups_carrier     = Carrier.objects.filter(carrier_type='ups',   is_active=True).first()
    fedex_carrier   = Carrier.objects.filter(carrier_type='fedex', is_active=True).first()

    # DigiKey
    try:
        from bots.models import DigiKeyConfig
        digikey = DigiKeyConfig.objects.filter(pk=1).first()
        digikey_ok = bool(digikey and digikey.client_id and digikey.client_secret)
    except Exception:
        digikey    = None
        digikey_ok = False

    dhl_track_ok = bool(dhl_track_carrier)

    context.update({
        'api_keys_count':  api_keys_count,
        'api_keys':        api_keys,
        'jumingo_carrier': jumingo_carrier,
        'dhl_carrier':     dhl_carrier,
        'dhl_track_ok':    dhl_track_ok,
        'ups_carrier':     ups_carrier,
        'fedex_carrier':   fedex_carrier,
        'digikey':         digikey,
        'digikey_ok':      digikey_ok,
        'console_ok':      is_minerva_admin(request.user),
    })
    return render(request, 'dashboard/api_index.html', context)

api_index = staff_member_required(api_index)


# ── Документація інтеграції (рендер api/README.md) ────────────────────────────

API_EXAMPLES = {"shop_client.py", "crm_purchases.py", "webhook_receiver.py", "api_tester.html"}


def api_docs(request):
    from pathlib import Path
    from django.conf import settings

    context = admin.site.each_context(request)
    base = request.build_absolute_uri('/').rstrip('/')
    md = (Path(settings.BASE_DIR) / 'api' / 'README.md').read_text(encoding='utf-8')
    # Підставляємо адресу саме цього сервера замість плейсхолдерів
    md = md.replace(' (локально `http://localhost:8000/api/v1/`)', '')
    for placeholder in ('https://<ваш-домен>', 'https://<домен>', 'http://localhost:8000'):
        md = md.replace(placeholder, base)
    md = md.replace('](examples/', '](/dashboard/api/docs/examples/')
    context.update({'readme_md': md, 'api_base': base + '/api/v1/',
                    'console_ok': is_minerva_admin(request.user)})
    return render(request, 'dashboard/api_docs.html', context)

api_docs = staff_member_required(api_docs)


def api_docs_example(request, name):
    from pathlib import Path
    from django.conf import settings
    from django.http import FileResponse, Http404

    if name not in API_EXAMPLES:
        raise Http404
    path = Path(settings.BASE_DIR) / 'api' / 'examples' / name
    resp = FileResponse(open(path, 'rb'), content_type='text/plain; charset=utf-8',
                        as_attachment=request.GET.get('download') == '1', filename=name)
    return resp

api_docs_example = staff_member_required(api_docs_example)


# ── Developer Console ─────────────────────────────────────────────────────────
#
# Лише адміністратор Minerva (core.utils.is_minerva_admin) — і сторінка, і проксі.
# Ключі API в сторінку не потрапляють: браузер бачить назву й замасковані початок / кінець (core.secrets.mask)
# і передає лише id ключа — сервер сам ставить «Authorization: Token …». Секрет DigiKey у консолі не показується.
# Проксі ходить ЛИШЕ в API цієї ж Minerva (/api/v1/ на тому самому хості) і не робить мережевого запиту:
# запит виконується всередині Minerva тим самим ланцюжком middleware, що й зовнішній (журнал API, ключі,
# обмеження IP — за IP адміністратора). Інші хости, внутрішня мережа й loopback — недосяжні.

CONSOLE_PREFIX = '/api/v1/'
CONSOLE_METHODS = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS')
# заголовки, які консоль не пересилає (адресу, IP і сесію задає сервер)
_DROP_HEADERS = {'host', 'content-length', 'connection', 'cookie', 'x-real-ip', 'x-forwarded-for',
                 'x-forwarded-host', 'x-forwarded-proto', 'x-forwarded-port', 'transfer-encoding'}


def _admin_required(view):
    """Сторінка / дія лише для адміністратора Minerva (суперкористувач або роль superadmin / admin)."""
    from functools import wraps
    from django.core.exceptions import PermissionDenied
    from core.utils import is_minerva_admin

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not is_minerva_admin(request.user):
            raise PermissionDenied('Консоль API — лише для адміністратора Minerva')
        return view(request, *args, **kwargs)
    return staff_member_required(wrapped)


def console_keys():
    """Активні ключі для вибору в консолі: id, назва, замаскований ключ — без самого ключа."""
    from api.models import APIKey
    from core.secrets import mask
    return [{'id': k.pk, 'name': k.name, 'masked': mask(k.key)}
            for k in APIKey.objects.filter(is_active=True).order_by('name', 'pk')]


def api_console(request):
    context = admin.site.each_context(request)
    context.update({
        'console_keys': console_keys(),
        'api_prefix':   CONSOLE_PREFIX,
    })
    return render(request, 'dashboard/api_console.html', context)

api_console = _admin_required(api_console)


def own_api_target(request, url):
    """(path, query) для запиту консолі або (None, причина): лише /api/v1/ цієї Minerva.

    Адреса без хоста («/api/v1/ping/») — ця Minerva; з хостом — лише той самий хост і порт, що в адресі,
    з якої відкрито консоль (http/https). Шлях — без «..», «//» і символів керування."""
    from urllib.parse import urlsplit, unquote

    url = (url or '').strip()
    if not url:
        return None, 'URL is required'
    if any(ord(ch) < 32 for ch in url) or '\\' in url:
        return None, 'Недопустимі символи в адресі'
    parts = urlsplit(url)
    if parts.scheme or parts.netloc:
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            return None, 'Лише http(s)-адреси API цієї Minerva'
        if parts.username or parts.password:
            return None, 'Адреса з логіном / паролем не приймається'
        if parts.netloc.lower() != request.get_host().lower():
            return None, (f'Консоль надсилає запити лише до API цієї Minerva '
                          f'({request.get_host()}{CONSOLE_PREFIX}) — інші адреси не відкриваються')
    path = parts.path or '/'
    decoded = unquote(path)
    if (not path.startswith(CONSOLE_PREFIX) or '//' in path or '..' in decoded.split('/')
            or '\\' in decoded or any(ord(ch) < 32 for ch in decoded)):
        return None, f'Лише адреси API Minerva: {CONSOLE_PREFIX}…'
    return (path, parts.query), None


def _dispatch_internal(request, method, path, query, headers, body: bytes):
    """Виконати запит до API цієї Minerva без мережі: той самий ланцюжок middleware, що й зовнішній запит.

    Нічого спільного з сесією адміністратора (cookie не передаються) — лише заголовки запиту консолі.
    IP запиту — IP адміністратора (обмеження «Дозволені IP» ключа перевіряються за ним)."""
    import sys
    from io import BytesIO
    from django.core.handlers.base import BaseHandler
    from django.core.handlers.wsgi import WSGIRequest
    from django.utils import translation
    from api.key_limits import client_ip

    environ = {
        'REQUEST_METHOD':    method,
        'SCRIPT_NAME':       '',
        'PATH_INFO':         path,
        'QUERY_STRING':      query or '',
        'SERVER_NAME':       request.META.get('SERVER_NAME') or 'localhost',
        'SERVER_PORT':       str(request.get_port() or '80'),
        'SERVER_PROTOCOL':   'HTTP/1.1',
        'HTTP_HOST':         request.get_host(),
        'REMOTE_ADDR':       client_ip(request) or '127.0.0.1',
        'CONTENT_LENGTH':    str(len(body)),
        'wsgi.version':      (1, 0),
        'wsgi.url_scheme':   request.scheme,
        'wsgi.input':        BytesIO(body),
        'wsgi.errors':       sys.stderr,
        'wsgi.multithread':  True,
        'wsgi.multiprocess': True,
        'wsgi.run_once':     False,
    }
    for name, value in headers.items():
        key = str(name).strip().upper().replace('-', '_')
        if not key or str(name).strip().lower() in _DROP_HEADERS:
            continue
        if key == 'CONTENT_TYPE':
            environ['CONTENT_TYPE'] = str(value)
        else:
            environ['HTTP_' + key] = str(value)

    lang = translation.get_language()
    try:
        handler = BaseHandler()
        handler.load_middleware()
        response = handler.get_response(WSGIRequest(environ))
    finally:
        if lang:
            translation.activate(lang)
    content = b''.join(response.streaming_content) if response.streaming else response.content
    return response, content


@_admin_required
@require_POST
def api_proxy(request):
    """Запит консолі до API цієї Minerva (див. вище). Ключ — за id зі списку, сервер ставить його сам."""
    from api.models import APIKey
    from core.secrets import mask

    try:
        payload = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': 'Invalid JSON payload'}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({'error': 'Invalid JSON payload'}, status=400)

    method   = str(payload.get('method') or 'GET').upper()
    headers  = payload.get('headers') or {}
    body     = payload.get('body') or ''
    auth_cfg = payload.get('auth') or {}
    if not isinstance(headers, dict) or not isinstance(auth_cfg, dict) or not isinstance(body, str):
        return JsonResponse({'error': 'Invalid JSON payload'}, status=400)
    if method not in CONSOLE_METHODS:
        return JsonResponse({'error': f'Invalid method: {method}'}, status=400)
    target, problem = own_api_target(request, payload.get('url'))
    if problem:
        return JsonResponse({'error': problem}, status=400)
    path, query = target

    headers = {str(k): str(v) for k, v in headers.items() if str(k).strip()}
    auth_type, auth_label = auth_cfg.get('type', ''), 'none'
    if auth_type == 'key':
        key = APIKey.objects.filter(pk=auth_cfg.get('key_id') or 0, is_active=True).first()
        if key is None:
            return JsonResponse({'error': 'Ключ не знайдено або вимкнено — оновіть сторінку консолі'}, status=400)
        headers = {k: v for k, v in headers.items() if k.lower() != 'authorization'}
        headers['Authorization'] = f'Token {key.key}'
        auth_label = f'key: {key.name} ({mask(key.key)})'
    elif auth_type in ('token', 'bearer'):
        token = str(auth_cfg.get('token', '')).strip()
        headers = {k: v for k, v in headers.items() if k.lower() != 'authorization'}
        headers['Authorization'] = f"{'Bearer' if auth_type == 'bearer' else 'Token'} {token}"
        auth_label = f'{auth_type}: {mask(token)}'

    req_data = body.encode('utf-8') if body and method not in ('GET', 'HEAD') else b''
    request_info = {
        'url':     path + (f'?{query}' if query else ''),
        'method':  method,
        'headers': {k: v for k, v in headers.items() if k.lower() != 'authorization'},
        'auth':    auth_label,
        'body':    body or None,
    }

    t0 = time.time()
    try:
        resp, content = _dispatch_internal(request, method, path, query, headers, req_data)
    except Exception as e:
        return JsonResponse({'error': f'{type(e).__name__}: {str(e)[:300]}'})
    duration_ms = int((time.time() - t0) * 1000)

    text = content.decode(resp.charset or 'utf-8', 'replace')
    try:
        body_str = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        is_json  = True
    except Exception:
        body_str = text[:100_000]
        is_json  = False
    return JsonResponse({
        'ok':          200 <= resp.status_code < 400,
        'status':      resp.status_code,
        'status_text': resp.reason_phrase,
        'headers':     dict(resp.items()),
        'body':        body_str,
        'is_json':     is_json,
        'duration_ms': duration_ms,
        'request':     request_info,
    })
