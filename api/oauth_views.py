"""🔌 OAuth для MCP — адреси й вікно «Дозволити» (логіка — api/oauth.py)."""
import functools
import json
import logging
from urllib.parse import quote, urlsplit

from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from . import oauth

logger = logging.getLogger(__name__)

CORS = {'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
        'Access-Control-Allow-Headers': 'Authorization, Content-Type, MCP-Protocol-Version',
        'Access-Control-Max-Age': '86400'}


def _json(data, status=200):
    response = JsonResponse(data, status=status, json_dumps_params={'ensure_ascii': False})
    for k, v in CORS.items():
        response[k] = v
    response['Cache-Control'] = 'no-store'
    response['Pragma'] = 'no-cache'
    return response


def _error(e):
    logger.warning('oauth: %s — %s', e.error, e.description)   # причина відмови — у журналі сервера (docker logs web)
    response = _json({'error': e.error, 'error_description': e.description}, status=e.status)
    if e.status == 401 and e.error == 'invalid_client':
        response['WWW-Authenticate'] = 'Basic realm="minerva"'
    return response


def _api(methods):
    """CORS, preflight OPTIONS і дозволені методи — для адрес, які викликає програма, а не людина."""
    def wrap(view):
        @csrf_exempt
        @functools.wraps(view)
        def inner(request, *args, **kwargs):
            if request.method == 'OPTIONS':
                response = HttpResponse(status=204)
                for k, v in CORS.items():
                    response[k] = v
                return response
            if request.method not in methods:
                response = _json({'error': 'invalid_request', 'error_description': 'метод не підтримується'}, 405)
                response['Allow'] = ', '.join(methods)
                return response
            return view(request, *args, **kwargs)
        return inner
    return wrap


def _body(request) -> dict:
    if request.content_type == 'application/json':
        try:
            data = json.loads(request.body or b'{}')
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return request.POST.dict()


@_api(('GET', 'HEAD'))
def protected_resource(request, path=''):
    if not oauth.enabled():
        return _json({'error': 'not_found'}, 404)
    return _json(oauth.resource_metadata(request, path))


@_api(('GET', 'HEAD'))
def authorization_server(request):
    if not oauth.enabled():
        return _json({'error': 'not_found'}, 404)
    return _json(oauth.server_metadata(request))


@_api(('POST',))
def register_view(request):
    from core.utils import client_ip
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return _error(oauth.OAuthError('invalid_client_metadata', 'тіло запиту — JSON'))
    try:
        return _json(oauth.register(data, client_ip(request)), status=201)
    except oauth.OAuthError as e:
        return _error(e)


@_api(('POST',))
def token_view(request):
    try:
        return _json(oauth.exchange(request, _body(request)))
    except oauth.OAuthError as e:
        return _error(e)


@_api(('POST',))
def revoke_view(request):
    try:
        oauth.revoke(request, _body(request))
    except oauth.OAuthError as e:
        return _error(e)
    return _json({})


def _redirect(url):
    """Назад у програму — і на власну схему програми на комп'ютері (cursor://…), її перевірила реєстрація."""
    class Back(HttpResponseRedirect):
        allowed_schemes = ['http', 'https', urlsplit(url).scheme.lower()]
    response = Back(url)
    response['Cache-Control'] = 'no-store'
    return response


def _page(request, context, status=200):
    response = render(request, 'oauth/authorize.html', context, status=status)
    response['Cache-Control'] = 'no-store'
    return response


def authorize_view(request):
    """Вікно «Дозволити»: людина входить у Minerva й дає програмі доступ до MCP від свого імені."""
    if request.method not in ('GET', 'POST'):
        return HttpResponse(status=405)
    params = request.POST if request.method == 'POST' else request.GET
    try:
        a = oauth.parse_authorize(params, request)
    except oauth.OAuthError as e:
        # помилку — назад у програму лише на знайому адресу: реєстрація відкрита, тож інакше будь-хто зробив би з
        # Minerva «трамплін» на свій сайт без жодного кліку людини
        if getattr(e, 'auth', None) is not None and e.auth.known:
            return _redirect(oauth.error_redirect(e.auth, e.error, e.description, request))
        return _page(request, {'error': e.description or e.error}, status=400)
    if not request.user.is_authenticated:
        return HttpResponseRedirect('/admin/login/?next=' + quote(request.get_full_path(), safe=''))
    if not oauth.can_authorize(request.user):
        return _page(request, {'error': 'У вас немає права на завдання агенту — попросіть адміністратора Minerva.'},
                     status=403)
    if request.method == 'POST':
        if request.POST.get('decision') != 'allow':
            return _redirect(oauth.error_redirect(a, 'access_denied', 'людина не дозволила доступ', request))
        try:
            url = oauth.approve(a, request.user, write=bool(request.POST.get('write')),
                                personal=bool(request.POST.get('personal')), request=request)
        except oauth.OAuthError as e:
            return _page(request, {'error': e.description or e.error}, status=403)
        return _redirect(url)
    return _page(request, {'a': a, 'params': a.params(), 'write': 'agent:write' in a.scopes,
                           'who': request.user.get_full_name() or request.user.get_username()})
