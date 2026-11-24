"""🔌 OAuth 2.1 для MCP-сервера Minerva (`/api/v1/mcp/`) — для програм, які не приймають ключ API: Claude.ai
(«Custom connector»), Claude Code (`/mcp` → Authenticate), ChatGPT, інші клієнти MCP.

Як у специфікації MCP (Authorization):
- метадані ресурсу RFC 9728 — `/.well-known/oauth-protected-resource[/api/v1/mcp/]`, посилання на них — у
  WWW-Authenticate відповіді 401 MCP-сервера (`www_authenticate`);
- метадані сервера RFC 8414 — `/.well-known/oauth-authorization-server`;
- реєстрація програми RFC 7591 — `POST /oauth/register/` (`register`: адреси повернення https, loopback http,
  власні схеми програм; `REGISTER_PER_HOUR` з адреси);
- код дозволу + PKCE S256 — `/oauth/authorize/` (людина входить у Minerva й натискає «Дозволити», `approve`);
- токени — `POST /oauth/token/` (`exchange`: authorization_code, refresh_token з ротацією; повторне використання
  коду чи токена оновлення відкликає все підключення — `revoke_key`), відкликання RFC 7009 — `POST /oauth/revoke/`;
- resource RFC 8707 — токен діє лише на MCP цього сервера (`same_resource`).

Кожне підключення — 🔒 ключ API (`APIKey.oauth_client`; власник = агент = людина, права — що вона дозволила:
agent:read, agent:write): журнал, обмеження, «Активний» — як у ключів; сам ключ не діє, лише токен
(`APIKeyAuthentication`). Пошта, календар і робочий час — лише з галочкою у вікні (`OAuthToken.personal` →
`APIKey.personal_ok`). У базі — лише SHA-256 кодів і токенів. Вимкнути OAuth зовсім — `MINERVA_MCP_OAUTH=0`.
"""
import base64
import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone

SCOPES = ('agent:read', 'agent:write')
CODE_TTL = timedelta(minutes=10)
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=60)
REDIRECTS_MAX = 10
REDIRECT_LEN = 500
REGISTER_PER_HOUR = 30
ACCESS_PREFIX, REFRESH_PREFIX, CODE_PREFIX = 'mnv_at_', 'mnv_rt_', 'mnv_ac_'
MCP_PATH = '/api/v1/mcp/'
AUTH_METHODS = ('none', 'client_secret_post', 'client_secret_basic')
LOOPBACK = ('localhost', '127.0.0.1', '::1')
BAD_SCHEMES = ('javascript', 'data', 'file', 'vbscript', 'about', 'blob', 'ftp', 'ws', 'wss')
# знайомі програми — у вікні «Дозволити» назва за адресою повернення (назву в реєстрації може написати будь-хто)
KNOWN_HOSTS = {'claude.ai': 'Claude', 'claude.com': 'Claude', 'chatgpt.com': 'ChatGPT',
               'chat.openai.com': 'ChatGPT'}


class OAuthError(Exception):
    """Помилка OAuth: `error` — код зі специфікації, `redirect` — чи можна повернути її програмі переходом."""

    def __init__(self, error, description='', status=400, redirect=True):
        super().__init__(description or error)
        self.error, self.description, self.status, self.redirect = error, description, status, redirect


def enabled() -> bool:
    return bool(getattr(settings, 'MCP_OAUTH', True))


def digest(token) -> str:
    return hashlib.sha256(str(token or '').encode()).hexdigest()


def base_url(request) -> str:
    return request.build_absolute_uri('/').rstrip('/')


def resource_url(request) -> str:
    """Адреса MCP без «/» у кінці: клієнти MCP перевіряють, що адреса сервера починається з `resource` метаданих —
    так підходить і `…/mcp`, і `…/mcp/` (порівняння токенів — `same_resource`, без огляду на «/»)."""
    return base_url(request) + MCP_PATH.rstrip('/')


def same_resource(a, b) -> bool:
    def norm(u):
        p = urlsplit(str(u or '').strip())
        return p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/')
    return norm(a) == norm(b)


def metadata_url(request) -> str:
    """Метадані для тієї адреси MCP, на яку прийшов запит (зі «/» у кінці чи без): Claude порівнює `resource` з
    адресою конектора — `…/mcp` з метаданими `…/mcp/` він відкидав («Autorisierung fehlgeschlagen»)."""
    path = getattr(request, 'path', '') or ''
    if path.rstrip('/') != MCP_PATH.rstrip('/'):
        path = MCP_PATH.rstrip('/')
    return base_url(request) + '/.well-known/oauth-protected-resource' + path


def www_authenticate(request, error='') -> str:
    """Заголовок 401 MCP-сервера: де метадані ресурсу (клієнт сам знайде, куди йти по дозвіл)."""
    parts = ['Bearer realm="minerva"']
    if enabled():
        parts.append(f'resource_metadata="{metadata_url(request)}"')
        parts.append(f'scope="{" ".join(SCOPES)}"')
    if error:
        parts.append(f'error="{error}"')
    return ', '.join(parts)


def resource_metadata(request, path='') -> dict:
    """RFC 9728. `resource` — точно та адреса, з якої клієнт її шукав (зі «/» у кінці чи без)."""
    resource = base_url(request) + (path if path else MCP_PATH.rstrip('/'))
    return {'resource': resource, 'authorization_servers': [base_url(request)], 'scopes_supported': list(SCOPES),
            'bearer_methods_supported': ['header'], 'resource_name': 'Minerva MCP'}


def server_metadata(request) -> dict:
    base = base_url(request)
    return {'issuer': base, 'authorization_endpoint': f'{base}/oauth/authorize/',
            'token_endpoint': f'{base}/oauth/token/', 'registration_endpoint': f'{base}/oauth/register/',
            'revocation_endpoint': f'{base}/oauth/revoke/', 'scopes_supported': list(SCOPES),
            'response_types_supported': ['code'], 'response_modes_supported': ['query'],
            'grant_types_supported': ['authorization_code', 'refresh_token'],
            'token_endpoint_auth_methods_supported': list(AUTH_METHODS),
            'revocation_endpoint_auth_methods_supported': list(AUTH_METHODS),
            'code_challenge_methods_supported': ['S256'],
            'authorization_response_iss_parameter_supported': True}


# ── реєстрація програми (RFC 7591) ───────────────────────────────────────────

def check_redirect(uri) -> str:
    """Адреса повернення: https; http — лише на цей комп'ютер (localhost); власна схема програми (cursor://…)."""
    uri = str(uri or '').strip()
    if not uri or len(uri) > REDIRECT_LEN:
        raise OAuthError('invalid_redirect_uri', 'адреса повернення порожня або задовга')
    p = urlsplit(uri)
    scheme, host = p.scheme.lower(), (p.hostname or '').lower()
    if p.fragment or not scheme or not re.match(r'^[a-z][a-z0-9+.-]*$', scheme):
        raise OAuthError('invalid_redirect_uri', f'неправильна адреса повернення: {uri}')
    if scheme == 'https' and host:
        return uri
    if scheme == 'http' and host in LOOPBACK:
        return uri
    if scheme not in ('http', 'https') and scheme not in BAD_SCHEMES:
        return uri                                  # програма на комп'ютері (RFC 8252: власна схема)
    raise OAuthError('invalid_redirect_uri', f'адреса повернення — лише https (http — лише localhost): {uri}')


def _too_many(ip) -> bool:
    from core.shared_cache import cache
    key = f'oauth-register:{ip or "unknown"}'
    try:
        if cache.add(key, 1, 3600):
            return False
        return cache.incr(key) > REGISTER_PER_HOUR
    except Exception:  # noqa: BLE001 — кеш недоступний: не блокуємо
        return False


def _text(value, limit) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:limit]


def register(data, ip='') -> dict:
    from .models import OAuthClient
    if not enabled():
        raise OAuthError('access_denied', 'OAuth у цій Minerva вимкнено', status=403)
    if not isinstance(data, dict):
        raise OAuthError('invalid_client_metadata', 'очікується JSON-об’єкт')
    uris = data.get('redirect_uris')
    if not isinstance(uris, list) or not uris or len(uris) > REDIRECTS_MAX:
        raise OAuthError('invalid_redirect_uri', f'redirect_uris — список з 1–{REDIRECTS_MAX} адрес')
    uris = list(dict.fromkeys(check_redirect(u) for u in uris))
    method = data.get('token_endpoint_auth_method') or 'none'
    if method not in AUTH_METHODS:
        raise OAuthError('invalid_client_metadata', 'token_endpoint_auth_method — ' + ' / '.join(AUTH_METHODS))
    grants = data.get('grant_types') or ['authorization_code', 'refresh_token']
    if (not isinstance(grants, list) or 'authorization_code' not in grants
            or not set(grants) <= {'authorization_code', 'refresh_token'}):
        raise OAuthError('invalid_client_metadata', 'grant_types — authorization_code, refresh_token')
    if not set(data.get('response_types') or ['code']) <= {'code'}:
        raise OAuthError('invalid_client_metadata', 'response_types — лише code')
    if _too_many(ip):
        raise OAuthError('temporarily_unavailable', 'забагато реєстрацій з цієї адреси — спробуйте за годину',
                         status=429)
    secret = secrets.token_urlsafe(32) if method != 'none' else ''
    client_uri = _text(data.get('client_uri'), 300)
    client = OAuthClient.objects.create(
        client_id=secrets.token_urlsafe(24), secret_hash=digest(secret) if secret else '',
        name=_text(data.get('client_name'), 100) or urlsplit(uris[0]).hostname or uris[0][:100],
        redirect_uris=uris, client_uri=client_uri if client_uri.startswith('https://') else '',
        created_ip=str(ip or '')[:45])
    out = {'client_id': client.client_id, 'client_id_issued_at': int(client.created_at.timestamp()),
           'client_name': client.name, 'redirect_uris': uris, 'grant_types': ['authorization_code', 'refresh_token'],
           'response_types': ['code'], 'token_endpoint_auth_method': method}
    if secret:
        out.update(client_secret=secret, client_secret_expires_at=0)
    return out


# ── дозвіл людини (код дозволу + PKCE) ───────────────────────────────────────

class AuthRequest:
    """Перевірений запит /oauth/authorize/."""

    def __init__(self, client, redirect_uri, state):
        self.client, self.redirect_uri, self.state = client, redirect_uri, state
        self.challenge, self.scopes, self.resource = '', list(SCOPES), ''

    @property
    def host(self) -> str:
        p = urlsplit(self.redirect_uri)
        return (p.hostname or p.scheme or '').lower()

    @property
    def known(self) -> str:
        """Назва знайомої програми за адресою повернення ('' — невідома)."""
        h = self.host
        if h in LOOPBACK:
            return 'програма на цьому комп’ютері (Claude Code, Claude Desktop…)'
        return next((name for d, name in KNOWN_HOSTS.items() if h == d or h.endswith('.' + d)), '')

    def params(self) -> dict:
        return {'client_id': self.client.client_id, 'redirect_uri': self.redirect_uri, 'state': self.state,
                'response_type': 'code', 'code_challenge': self.challenge, 'code_challenge_method': 'S256',
                'scope': ' '.join(self.scopes), 'resource': self.resource}


def parse_authorize(params, request) -> AuthRequest:
    """Помилки програми й адреси повернення — без переходу (сторінка помилки), решта — переходом назад."""
    from .models import OAuthClient
    if not enabled():
        raise OAuthError('access_denied', 'OAuth у цій Minerva вимкнено', redirect=False)
    client = OAuthClient.objects.filter(client_id=str(params.get('client_id') or '')[:64]).first()
    if client is None:
        raise OAuthError('invalid_client', 'Невідома програма — підключіть її в програмі знову.', redirect=False)
    redirect = str(params.get('redirect_uri') or '')
    if redirect:
        if redirect not in client.redirect_uris:
            raise OAuthError('invalid_request', 'Адреса повернення не та, що програма зареєструвала.',
                             redirect=False)
    elif len(client.redirect_uris) == 1:
        redirect = client.redirect_uris[0]
    else:
        raise OAuthError('invalid_request', 'Не вказано адресу повернення.', redirect=False)
    a = AuthRequest(client, redirect, str(params.get('state') or '')[:500])
    try:
        _check_authorize(a, params, request)
    except OAuthError as e:
        e.auth = a                                  # помилку — переходом на адресу повернення програми
        raise
    return a


def _check_authorize(a, params, request):
    if params.get('response_type') != 'code':
        raise OAuthError('unsupported_response_type', 'лише response_type=code')
    challenge = str(params.get('code_challenge') or '')
    if params.get('code_challenge_method') != 'S256' or not re.match(r'^[A-Za-z0-9._~-]{43,128}$', challenge):
        raise OAuthError('invalid_request', 'потрібен PKCE: code_challenge з code_challenge_method=S256')
    a.challenge = challenge
    asked = str(params.get('scope') or '').split()
    known = [s for s in SCOPES if s in asked]
    if asked and not known:
        raise OAuthError('invalid_scope', 'доступні права: ' + ' '.join(SCOPES))
    a.scopes = known or list(SCOPES)
    resource = str(params.get('resource') or '')
    if resource and not same_resource(resource, resource_url(request)):
        raise OAuthError('invalid_target', f'цей сервер дає доступ лише до свого MCP: {resource_url(request)}')
    a.resource = resource_url(request)


def can_authorize(user) -> bool:
    return bool(user is not None and user.is_active and user.is_staff and user.has_perm('agent.add_agenttask'))


def with_params(uri, params) -> str:
    p = urlsplit(uri)
    query = parse_qsl(p.query, keep_blank_values=True) + [(k, v) for k, v in params.items() if v not in (None, '')]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ''))


def error_redirect(a: AuthRequest, error, description, request) -> str:
    return with_params(a.redirect_uri, {'error': error, 'error_description': description, 'state': a.state,
                                        'iss': base_url(request)})


def approve(a: AuthRequest, user, write: bool, personal: bool, request) -> str:
    """«Дозволити» → одноразовий код (10 хв) → адреса повернення програми з кодом."""
    from .models import OAuthToken
    if not can_authorize(user):
        raise OAuthError('access_denied', 'немає права на завдання агенту', redirect=False)
    scopes = [s for s in a.scopes if s == 'agent:read' or (s == 'agent:write' and write)] or ['agent:read']
    if 'agent:read' not in scopes:
        scopes.insert(0, 'agent:read')
    code = CODE_PREFIX + secrets.token_urlsafe(32)
    OAuthToken.objects.create(kind=OAuthToken.CODE, token_hash=digest(code), client=a.client, user=user,
                              scopes=scopes, personal=bool(personal), redirect_uri=a.redirect_uri,
                              code_challenge=a.challenge, resource=a.resource,
                              expires_at=timezone.now() + CODE_TTL)
    return with_params(a.redirect_uri, {'code': code, 'state': a.state, 'iss': base_url(request)})


# ── токени ───────────────────────────────────────────────────────────────────

def client_from(request, data):
    """Програма з Basic-заголовка або client_id / client_secret у тілі; з секретом — лише з правильним."""
    from .models import OAuthClient
    cid, secret = data.get('client_id'), data.get('client_secret')
    header = request.META.get('HTTP_AUTHORIZATION', '')
    if header.startswith('Basic '):
        try:
            raw = base64.b64decode(header[6:].strip()).decode('utf-8')
            cid, secret = (unquote(x) for x in raw.split(':', 1))
        except Exception:  # noqa: BLE001
            raise OAuthError('invalid_client', 'неправильний заголовок Basic', status=401)
    client = OAuthClient.objects.filter(client_id=str(cid or '')[:64]).first()
    if client is None:
        raise OAuthError('invalid_client', 'невідомий client_id', status=401)
    if client.secret_hash and not (secret and hmac.compare_digest(digest(secret), client.secret_hash)):
        raise OAuthError('invalid_client', 'неправильний client_secret', status=401)
    return client


def _pkce_ok(verifier, challenge) -> bool:
    if not re.match(r'^[A-Za-z0-9._~-]{43,128}$', str(verifier or '')):
        return False
    calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return hmac.compare_digest(calc, challenge)


def _key_name(client, scopes, personal) -> str:
    what = 'пропонує зміни' if 'agent:write' in scopes else 'лише читання'
    return f'🔌 {client.name[:60]} (OAuth · {what}{" · 🔒 пошта" if personal else ""})'[:100]


def issue(key, client, user, scopes, personal, resource) -> dict:
    from .models import OAuthToken
    access, refresh = ACCESS_PREFIX + secrets.token_urlsafe(32), REFRESH_PREFIX + secrets.token_urlsafe(32)
    now = timezone.now()
    common = dict(client=client, user=user, api_key=key, scopes=list(scopes), personal=personal, resource=resource)
    OAuthToken.objects.create(kind=OAuthToken.ACCESS, token_hash=digest(access), expires_at=now + ACCESS_TTL, **common)
    OAuthToken.objects.create(kind=OAuthToken.REFRESH, token_hash=digest(refresh), expires_at=now + REFRESH_TTL,
                              **common)
    return {'access_token': access, 'token_type': 'Bearer', 'expires_in': int(ACCESS_TTL.total_seconds()),
            'refresh_token': refresh, 'scope': ' '.join(scopes)}


def revoke_key(key):
    """Відкликати підключення: усі токени — геть, ключ вимкнено (видно в «🔑 API ключі»)."""
    from .models import APIKey, OAuthToken
    if key is None:
        return
    OAuthToken.objects.filter(api_key=key).exclude(kind=OAuthToken.CODE).delete()
    APIKey.objects.filter(pk=key.pk).update(is_active=False)


def _key_alive(key) -> bool:
    return bool(key is not None and key.is_active and not (key.expires_at and key.expires_at < timezone.localdate()))


def exchange(request, data) -> dict:
    """POST /oauth/token/ → токени (authorization_code / refresh_token)."""
    if not enabled():
        raise OAuthError('access_denied', 'OAuth у цій Minerva вимкнено', status=403)
    client = client_from(request, data)
    grant = data.get('grant_type')
    if grant == 'authorization_code':
        return _from_code(request, client, data)
    if grant == 'refresh_token':
        return _from_refresh(client, data)
    raise OAuthError('unsupported_grant_type', 'grant_type — authorization_code або refresh_token')


def _from_code(request, client, data) -> dict:
    from .models import APIKey, OAuthToken
    now, failed, replay = timezone.now(), '', None
    with transaction.atomic():
        tok = (OAuthToken.objects.select_for_update().select_related('api_key', 'user')
               .filter(token_hash=digest(data.get('code')), kind=OAuthToken.CODE).first())
        if tok is None or tok.client_id != client.pk:
            failed = 'невідомий код дозволу'
        elif tok.used_at:
            failed, replay = 'код дозволу вже використано — підключення відкликано', tok.api_key
        elif tok.expires_at < now:
            failed = 'код дозволу прострочено — дозвольте знову'
        elif data.get('redirect_uri') and data['redirect_uri'] != tok.redirect_uri:
            failed = 'redirect_uri не той, що в запиті дозволу'
        elif not _pkce_ok(data.get('code_verifier'), tok.code_challenge):
            failed = 'code_verifier не відповідає code_challenge'
        elif data.get('resource') and not same_resource(data['resource'], tok.resource):
            failed = 'resource не той, на який дано дозвіл'
        elif not can_authorize(tok.user):
            failed = 'у людини вже немає права на завдання агенту'
        if failed:
            if tok is not None and tok.client_id == client.pk and not tok.used_at:
                tok.used_at = now                       # невдала спроба з кодом — більше він не діє
                tok.save(update_fields=['used_at'])
        else:
            user = tok.user
            key = APIKey.objects.create(name=_key_name(client, tok.scopes, tok.personal), scopes=list(tok.scopes),
                                        owner=user, agent_user=user, private=True, oauth_client=client)
            tok.used_at, tok.api_key = now, key
            tok.save(update_fields=['used_at', 'api_key'])
            out = issue(key, client, user, tok.scopes, tok.personal, tok.resource)
    if failed:
        revoke_key(replay)
        raise OAuthError('invalid_grant', failed)
    return out


def _from_refresh(client, data) -> dict:
    from .models import OAuthToken
    now, failed, replay = timezone.now(), '', None
    with transaction.atomic():
        tok = (OAuthToken.objects.select_for_update().select_related('api_key', 'user')
               .filter(token_hash=digest(data.get('refresh_token')), kind=OAuthToken.REFRESH).first())
        if tok is None or tok.client_id != client.pk:
            failed = 'невідомий токен оновлення'
        elif tok.used_at:
            failed, replay = 'токен оновлення вже використано — підключення відкликано, дозвольте знову', tok.api_key
        elif tok.expires_at < now or not _key_alive(tok.api_key):
            failed = 'підключення вимкнено або прострочено — дозвольте знову'
        elif not can_authorize(tok.user):
            failed = 'у людини вже немає права на завдання агенту'
        scopes = list(tok.scopes) if tok is not None else []
        asked = str(data.get('scope') or '').split()
        if not failed and asked:
            if not set(asked) <= set(scopes):
                failed = 'scope ширший, ніж дозволено'
            else:
                scopes = [s for s in scopes if s in asked]
        if not failed:
            tok.used_at = now
            tok.save(update_fields=['used_at'])
            out = issue(tok.api_key, client, tok.user, scopes, tok.personal, tok.resource)
    if failed:
        revoke_key(replay)
        raise OAuthError('invalid_scope' if 'scope' in failed else 'invalid_grant', failed)
    return out


def revoke(request, data):
    """RFC 7009: токен оновлення — усе підключення (ключ вимкнено), токен доступу — лише він. Невідомий — теж 200."""
    from .models import OAuthToken
    client = client_from(request, data)
    tok = (OAuthToken.objects.select_related('api_key').filter(token_hash=digest(data.get('token')),
                                                               client=client).exclude(kind=OAuthToken.CODE).first())
    if tok is None:
        return
    if tok.kind == OAuthToken.REFRESH:
        revoke_key(tok.api_key)
    else:
        tok.delete()


def authenticate(request, token):
    """Токен доступу MCP → ключ підключення (з `oauth_personal`), інакше OAuthError (401 invalid_token)."""
    from .models import OAuthToken
    if not enabled():
        raise OAuthError('invalid_token', 'OAuth у цій Minerva вимкнено', status=401)
    tok = (OAuthToken.objects.select_related('api_key', 'api_key__agent_user')
           .filter(token_hash=digest(token), kind=OAuthToken.ACCESS).first())
    if tok is None or tok.api_key is None:
        raise OAuthError('invalid_token', 'невідомий токен OAuth', status=401)
    target = getattr(request, '_request', request)
    target.api_key_seen = tok.api_key                   # журнал запитів — під ключем підключення
    if tok.expires_at < timezone.now():
        target.api_log_note = 'токен OAuth прострочено'
        raise OAuthError('invalid_token', 'токен прострочено — оновіть його', status=401)
    if not _key_alive(tok.api_key):
        target.api_log_note = 'підключення OAuth вимкнено'
        raise OAuthError('invalid_token', 'підключення вимкнено в «🔑 API ключі»', status=401)
    if tok.resource and not same_resource(tok.resource, resource_url(request)):
        raise OAuthError('invalid_token', 'токен видано для іншого сервера', status=401)
    key = tok.api_key
    key.oauth_personal = tok.personal
    return key


def cleanup(days=1) -> int:
    """Прострочені коди й токени (з cron `api_log_cleanup`)."""
    from .models import OAuthToken
    n, _ = OAuthToken.objects.filter(expires_at__lt=timezone.now() - timedelta(days=days)).delete()
    return n
