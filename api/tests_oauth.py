"""🔌 OAuth для MCP-сервера (api/oauth.py, api/oauth_views.py): метадані, реєстрація програми, «Дозволити», токени,
MCP з токеном, ротація й відкликання — як підключається Claude.ai («Custom connector»).

python manage.py test api.tests_oauth --settings=tabele.settings_test
"""
import base64
import hashlib
import json
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from api.models import APIKey, OAuthClient, OAuthToken

CALLBACK = 'https://claude.ai/api/mcp/auth_callback'
MCP = '/api/v1/mcp/'
RESOURCE = 'http://testserver/api/v1/mcp/'


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return verifier, challenge


class OAuthTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        self.user = User.objects.create_superuser('slava', 's@x.y', 'p')

    # ── помічники: як робить програма ──
    def register(self, **extra):
        body = {'client_name': 'Claude', 'redirect_uris': [CALLBACK], **extra}
        return self.client.post('/oauth/register/', json.dumps(body), content_type='application/json')

    def authorize_params(self, client_id, challenge, **extra):
        return {'response_type': 'code', 'client_id': client_id, 'redirect_uri': CALLBACK, 'state': 'st-1',
                'code_challenge': challenge, 'code_challenge_method': 'S256', 'scope': 'agent:read agent:write',
                'resource': RESOURCE, **extra}

    def allow(self, client_id, challenge, write=True, personal=False):
        self.client.force_login(self.user)
        data = {**self.authorize_params(client_id, challenge), 'decision': 'allow'}
        if write:
            data['write'] = '1'
        if personal:
            data['personal'] = '1'
        r = self.client.post('/oauth/authorize/', data)
        self.assertEqual(r.status_code, 302, r.content[:300])
        q = parse_qs(urlsplit(r['Location']).query)
        self.assertEqual((q['state'], q['iss']), (['st-1'], ['http://testserver']))
        self.client.logout()
        return q['code'][0]

    def token(self, **data):
        return self.client.post('/oauth/token/', data)

    def connect(self, write=True, personal=False):
        client_id = self.register().json()['client_id']
        verifier, challenge = pkce()
        code = self.allow(client_id, challenge, write=write, personal=personal)
        r = self.token(grant_type='authorization_code', code=code, redirect_uri=CALLBACK, client_id=client_id,
                       code_verifier=verifier, resource=RESOURCE)
        self.assertEqual(r.status_code, 200, r.content)
        return client_id, r.json()

    def rpc(self, token, method, params=None, session=''):
        headers = {'HTTP_AUTHORIZATION': f'Bearer {token}'}
        if session:
            headers['HTTP_MCP_SESSION_ID'] = session
        return self.client.post(MCP, json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}),
                                content_type='application/json', **headers)

    def tools(self, token):
        r = self.rpc(token, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                           'clientInfo': {'name': 'claude-ai', 'version': '1'}})
        self.assertEqual(r.status_code, 200, r.content)
        return {t['name'] for t in self.rpc(token, 'tools/list', session=r['Mcp-Session-Id']).json()['result']['tools']}

    # ── тести ──
    def test_discovery_without_login(self):
        r = self.client.post(MCP, '{}', content_type='application/json')
        self.assertEqual(r.status_code, 401)
        self.assertIn('resource_metadata="http://testserver/.well-known/oauth-protected-resource/api/v1/mcp/"',
                      r['WWW-Authenticate'])
        prm = self.client.get('/.well-known/oauth-protected-resource/api/v1/mcp/').json()
        self.assertEqual((prm['resource'], prm['authorization_servers']), (RESOURCE, ['http://testserver']))
        self.assertEqual(self.client.get('/.well-known/oauth-protected-resource/api/v1/mcp').json()['resource'],
                         'http://testserver/api/v1/mcp')
        self.assertEqual(self.client.get('/.well-known/oauth-protected-resource').json()['resource'], RESOURCE)
        asm = self.client.get('/.well-known/oauth-authorization-server')
        self.assertEqual(asm['Access-Control-Allow-Origin'], '*')
        asm = asm.json()
        self.assertEqual(asm['issuer'], 'http://testserver')
        self.assertEqual(asm['code_challenge_methods_supported'], ['S256'])
        self.assertTrue(asm['registration_endpoint'].endswith('/oauth/register/'))
        self.assertEqual(self.client.options('/oauth/token/', HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST').status_code,
                         204)
        with override_settings(MCP_OAUTH=False):
            self.assertEqual(self.client.get('/.well-known/oauth-authorization-server').status_code, 404)
            self.assertNotIn('resource_metadata', self.client.post(MCP, '{}', content_type='application/json')
                             ['WWW-Authenticate'])

    def test_register_checks_redirects(self):
        r = self.register()
        self.assertEqual(r.status_code, 201)
        self.assertEqual((r.json()['token_endpoint_auth_method'], 'client_secret' in r.json()), ('none', False))
        for bad in (['http://evil.example/cb'], ['javascript:alert(1)'], [], ['https://x.example/cb#f']):
            self.assertEqual(self.register(redirect_uris=bad).status_code, 400, bad)
        ok = self.register(redirect_uris=['http://localhost:33418/callback', 'cursor://anysphere.cursor/oauth'])
        self.assertEqual(ok.status_code, 201, ok.content)
        secret = self.register(token_endpoint_auth_method='client_secret_post').json()
        self.assertTrue(secret['client_secret'])
        self.assertNotEqual(OAuthClient.objects.get(client_id=secret['client_id']).secret_hash,
                            secret['client_secret'])                   # у базі — лише SHA-256

    def test_consent_needs_login_and_right(self):
        client_id = self.register().json()['client_id']
        _v, challenge = pkce()
        params = self.authorize_params(client_id, challenge)
        r = self.client.get('/oauth/authorize/', params)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r['Location'].startswith('/admin/login/?next=%2Foauth%2Fauthorize%2F'))
        nxt = parse_qs(urlsplit(r['Location']).query)['next'][0]
        r = self.client.post('/admin/login/', {'username': 'slava', 'password': 'p', 'next': nxt})
        self.assertEqual(r['Location'], nxt)                  # після входу — знову у вікно «Дозволити»
        self.client.logout()
        # невідома програма / чужа адреса повернення — сторінка помилки, без переходу
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/oauth/authorize/', {**params, 'client_id': 'nope'}).status_code, 400)
        self.assertEqual(self.client.get('/oauth/authorize/', {**params, 'redirect_uri': 'https://evil.example/cb'})
                         .status_code, 400)
        # без PKCE / чужий resource — назад у програму з помилкою
        r = self.client.get('/oauth/authorize/', {**params, 'code_challenge_method': 'plain'})
        self.assertEqual(r.status_code, 302)
        self.assertIn('error=invalid_request', r['Location'])
        r = self.client.get('/oauth/authorize/', {**params, 'resource': 'https://other.example/mcp/'})
        self.assertIn('error=invalid_target', r['Location'])
        # незнайома адреса повернення — помилка сторінкою, а не переходом на чужий сайт
        other = self.register(redirect_uris=['https://hub.example.com/cb']).json()['client_id']
        r = self.client.get('/oauth/authorize/', {**params, 'client_id': other, 'redirect_uri': 'https://hub.example.com/cb',
                                                  'code_challenge_method': 'plain'})
        self.assertEqual(r.status_code, 400)
        page = self.client.get('/oauth/authorize/', {**params, 'client_id': other,
                                                     'redirect_uri': 'https://hub.example.com/cb'})
        self.assertContains(page, 'Цієї адреси Minerva не знає')
        page = self.client.get('/oauth/authorize/', params)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Claude')
        self.assertContains(page, 'claude.ai')
        r = self.client.post('/oauth/authorize/', {**params, 'decision': 'deny'})
        self.assertIn('error=access_denied', r['Location'])
        self.assertIn('state=st-1', r['Location'])
        # без права на завдання агенту — не можна
        clerk = User.objects.create_user('clerk', 'c@x.y', 'p', is_staff=True)
        clerk.profile.role, clerk.profile.allowed_modules = 'readonly', []
        clerk.profile.save()
        self.client.force_login(clerk)
        self.assertEqual(self.client.get('/oauth/authorize/', params).status_code, 403)
        r = self.client.post('/oauth/authorize/', {**params, 'decision': 'allow'})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(OAuthToken.objects.exists())

    def test_full_flow_mcp_refresh_and_revoke(self):
        client_id, tokens = self.connect()
        self.assertEqual((tokens['token_type'], tokens['scope']), ('Bearer', 'agent:read agent:write'))
        key = APIKey.objects.get(oauth_client__client_id=client_id)
        self.assertEqual((key.owner, key.agent_user, key.private, key.scopes),
                         (self.user, self.user, True, ['agent:read', 'agent:write']))
        self.assertFalse(OAuthToken.objects.filter(token_hash=tokens['access_token']).exists())   # лише SHA-256
        names = self.tools(tokens['access_token'])
        self.assertIn('read_datasheet', names)
        self.assertNotIn('calendar_events', names)          # пошта й календар — лише з галочкою «🔒»
        # сам ключ підключення не діє
        r = self.client.post(MCP, '{}', content_type='application/json', HTTP_AUTHORIZATION=f'Token {key.key}')
        self.assertEqual(r.status_code, 401)
        # оновлення: новий токен діє, старий токен оновлення — ні (і повтор відкликає все підключення)
        r = self.token(grant_type='refresh_token', refresh_token=tokens['refresh_token'], client_id=client_id)
        self.assertEqual(r.status_code, 200, r.content)
        fresh = r.json()
        self.assertEqual(self.rpc(fresh['access_token'], 'ping').status_code, 200)
        r = self.token(grant_type='refresh_token', refresh_token=tokens['refresh_token'], client_id=client_id)
        self.assertEqual(r.json()['error'], 'invalid_grant')
        key.refresh_from_db()
        self.assertFalse(key.is_active)
        r = self.rpc(fresh['access_token'], 'ping')
        self.assertEqual(r.status_code, 401)
        self.assertIn('error="invalid_token"', r['WWW-Authenticate'])

    def test_code_once_pkce_and_expiry(self):
        client_id = self.register().json()['client_id']
        verifier, challenge = pkce()
        code = self.allow(client_id, challenge)
        bad = self.token(grant_type='authorization_code', code=code, redirect_uri=CALLBACK, client_id=client_id,
                         code_verifier=pkce()[0])
        self.assertEqual(bad.json()['error'], 'invalid_grant')
        # невдала спроба — код більше не діє
        again = self.token(grant_type='authorization_code', code=code, redirect_uri=CALLBACK, client_id=client_id,
                           code_verifier=verifier)
        self.assertEqual(again.status_code, 400)
        self.assertFalse(APIKey.objects.exists())
        # прострочений токен доступу — 401 invalid_token
        _cid, tokens = self.connect()
        OAuthToken.objects.filter(kind=OAuthToken.ACCESS).update(expires_at=timezone.now() - timedelta(seconds=1))
        r = self.rpc(tokens['access_token'], 'ping')
        self.assertEqual(r.status_code, 401)
        self.assertIn('error="invalid_token"', r['WWW-Authenticate'])
        # вимкнули ключ у «🔑 API ключі» — оновлення не діє
        APIKey.objects.update(is_active=False)
        r = self.token(grant_type='refresh_token', refresh_token=tokens['refresh_token'], client_id=_cid)
        self.assertEqual(r.json()['error'], 'invalid_grant')

    def test_read_only_and_personal_consent(self):
        _cid, tokens = self.connect(write=False, personal=True)
        key = APIKey.objects.get()
        self.assertEqual(key.scopes, ['agent:read'])
        self.assertIn('calendar_events', self.tools(tokens['access_token']))
        r = self.rpc(tokens['access_token'], 'tools/call', {'name': 'propose_change', 'arguments': {}})
        self.assertEqual(r.status_code, 403)                  # пропонувати зміни не дозволено

    def test_confidential_client_and_revoke(self):
        reg = self.register(token_endpoint_auth_method='client_secret_basic').json()
        verifier, challenge = pkce()
        code = self.allow(reg['client_id'], challenge)
        r = self.token(grant_type='authorization_code', code=code, redirect_uri=CALLBACK,
                       client_id=reg['client_id'], code_verifier=verifier)
        self.assertEqual((r.status_code, r.json()['error']), (401, 'invalid_client'))
        basic = base64.b64encode(f'{reg["client_id"]}:{reg["client_secret"]}'.encode()).decode()
        r = self.client.post('/oauth/token/', {'grant_type': 'authorization_code', 'code': code,
                                               'redirect_uri': CALLBACK, 'code_verifier': verifier},
                             HTTP_AUTHORIZATION=f'Basic {basic}')
        self.assertEqual(r.status_code, 200, r.content)
        r = self.client.post('/oauth/revoke/', {'token': r.json()['refresh_token']}, HTTP_AUTHORIZATION=f'Basic {basic}')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(APIKey.objects.get().is_active)
        self.assertFalse(OAuthToken.objects.exclude(kind=OAuthToken.CODE).exists())

    def test_admin_shows_connection_locked(self):
        self.connect()
        key = APIKey.objects.get()
        self.client.force_login(self.user)
        page = self.client.get(f'/admin/api/apikey/{key.pk}/change/')
        self.assertContains(page, '🔌 Підключення OAuth')
        self.assertContains(page, 'підключення діє')
        self.assertNotContains(page, key.key)
        self.assertEqual(self.client.get('/admin/api/oauthclient/').status_code, 200)
