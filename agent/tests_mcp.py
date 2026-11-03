"""🤖 MCP-сервер Minerva (`/api/v1/mcp/`, agent/mcp.py + api/mcp.py): сесія як у справжнього клієнта MCP.

python manage.py test agent.tests_mcp --settings=tabele.settings_test
"""
import json
from decimal import Decimal

from agent.models import AgentProposal, AgentTask
from agent.tests_tasks import Base
from api.models import APIKey
from inventory.models import Product

URL = '/api/v1/mcp/'


class MCPTests(Base):
    def setUp(self):
        super().setUp()
        self.key = APIKey.objects.create(name='Claude Code', scopes=['agent:read', 'agent:write'],
                                         agent_user=self.user)

    def rpc(self, method, params=None, mid=1, key=None, session='', scheme='Bearer', raw=None):
        body = raw if raw is not None else {'jsonrpc': '2.0', 'id': mid, 'method': method, 'params': params or {}}
        if raw is None and mid is None:
            body.pop('id')
        headers = {'HTTP_AUTHORIZATION': f'{scheme} {(key or self.key).key}'}
        if session:
            headers['HTTP_MCP_SESSION_ID'] = session
        return self.client.post(URL, json.dumps(body), content_type='application/json', **headers)

    def start(self):
        r = self.rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                    'clientInfo': {'name': 'claude-code', 'version': '2.1'}})
        self.assertEqual(r.status_code, 200)
        session = r['Mcp-Session-Id']
        self.assertEqual(self.rpc('notifications/initialized', mid=None, session=session).status_code, 202)
        return r.json()['result'], session

    def test_initialize_and_tools(self):
        result, session = self.start()
        self.assertEqual((result['protocolVersion'], result['serverInfo']['name']), ('2025-06-18', 'minerva'))
        self.assertIn('tools', result['capabilities'])
        tools = {t['name']: t for t in self.rpc('tools/list', session=session).json()['result']['tools']}
        self.assertIn('read_datasheet', tools)
        self.assertIn('propose_dk_prices', tools)
        self.assertNotIn('propose_email', tools)
        self.assertNotIn('read_mail', tools)
        self.assertTrue(tools['get_product']['annotations']['readOnlyHint'])
        self.assertFalse(tools['propose_change']['annotations']['readOnlyHint'])
        self.assertIn('properties', tools['propose_change']['inputSchema'])

    def test_read_then_propose_becomes_a_task(self):
        _result, session = self.start()
        r = self.rpc('tools/call', {'name': 'read_datasheet', 'arguments': {'sku': 'AN220213-01C'}}, session=session)
        content = r.json()['result']
        self.assertFalse(content['isError'])
        self.assertIn('Weight: 45 g', content['content'][0]['text'])
        self.assertFalse(AgentTask.objects.exists())                 # читання — без завдання
        r = self.rpc('tools/call', {'name': 'propose_change', 'arguments': {
            'sku': 'AN220213-01C', 'field': 'net_weight_g', 'value': '45', 'evidence': 'Weight: 45 g',
            'source': 'даташит, стор. 1'}}, session=session)
        text = r.json()['result']['content'][0]['text']
        task = AgentTask.objects.get()
        self.assertEqual((task.owner, task.api_key, task.mcp_session, task.status),
                         (self.user, self.key, session, AgentTask.REVIEW))
        self.assertIn('claude-code', task.title)
        self.assertIn(f'/admin/agent/agenttask/{task.pk}/review/', text)
        p = task.proposals.get()
        self.assertEqual((p.kind, p.new_value), (AgentProposal.FIELD, '45'))
        self.assertIsNone(Product.objects.get(pk=self.ant.pk).net_weight_g)   # без ✓ людини — нічого
        # друга пропозиція в тій самій сесії — те саме завдання
        self.rpc('tools/call', {'name': 'propose_change', 'arguments': {
            'sku': 'AN220213-01C', 'field': 'country_of_origin', 'value': 'DE', 'evidence': 'Germany',
            'source': 'даташит'}}, session=session)
        self.assertEqual(AgentTask.objects.count(), 1)
        self.client.force_login(self.user)
        page = self.client.get(f'/admin/agent/agenttask/{task.pk}/review/')
        self.assertContains(page, 'Країна виробника')
        self.assertContains(page, 'Claude Code')                       # назва ключа
        self.assertNotContains(page, '▶ Продовжити')                   # Minerva сама завдання MCP не виконує
        from agent import tasks
        self.assertFalse(tasks.resume(task))
        self.assertEqual(AgentTask.objects.get(pk=task.pk).status, AgentTask.REVIEW)

    def test_tool_errors_and_hidden_tools(self):
        _result, session = self.start()
        r = self.rpc('tools/call', {'name': 'propose_change', 'arguments': {
            'sku': 'AN220213-01C', 'field': 'net_weight_g', 'value': 'abc', 'evidence': 'x', 'source': 'y'}},
            session=session)
        self.assertTrue(r.json()['result']['isError'])
        for name in ('propose_email', 'read_mail', 'web_search'):
            r = self.rpc('tools/call', {'name': name, 'arguments': {}}, session=session)
            self.assertEqual(r.json()['error']['code'], -32602)
        self.assertEqual(self.rpc('nope', session=session).json()['error']['code'], -32601)

    def test_scopes_and_keys(self):
        reader = APIKey.objects.create(name='r', scopes=['agent:read'], agent_user=self.user)
        r = self.rpc('tools/call', {'name': 'get_product', 'arguments': {'sku': 'AN220213-01C'}}, key=reader)
        self.assertEqual(r.status_code, 200)
        r = self.rpc('tools/call', {'name': 'propose_change', 'arguments': {
            'sku': 'AN220213-01C', 'field': 'net_weight_g', 'value': '1', 'evidence': 'x', 'source': 'y'}},
            key=reader)
        self.assertEqual(r.status_code, 403)
        nobody = APIKey.objects.create(name='n', scopes=['agent:read'])
        self.assertEqual(self.rpc('tools/list', key=nobody).status_code, 403)
        from shop.models import Shop
        shop_key = APIKey.objects.create(name='s', scopes=['agent:read'], agent_user=self.user,
                                         shop=Shop.objects.create(name='Web', slug='web'))
        self.assertEqual(self.rpc('tools/list', key=shop_key).status_code, 403)
        other = APIKey.objects.create(name='o', scopes=['products:read'], agent_user=self.user)
        self.assertEqual(self.rpc('tools/list', key=other).status_code, 403)
        self.assertEqual(self.rpc('tools/list', scheme='Token').status_code, 200)
        r = self.client.post(URL, '{}', content_type='application/json')
        self.assertEqual(r.status_code, 401)
        self.assertIn('Bearer', r['WWW-Authenticate'])

    def test_batch_get_delete_and_no_session(self):
        auth = {'HTTP_AUTHORIZATION': f'Bearer {self.key.key}'}
        body = [{'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, {'jsonrpc': '2.0', 'method': 'notifications/x'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
        r = self.client.post(URL, json.dumps(body), content_type='application/json', **auth)
        self.assertEqual([m['id'] for m in r.json()], [1, 2])
        self.assertEqual(self.client.get(URL, **auth).status_code, 405)
        self.assertEqual(self.client.delete(URL, **auth).status_code, 204)
        self.rpc('tools/call', {'name': 'propose_change', 'arguments': {
            'sku': 'AN220213-01C', 'field': 'net_weight_g', 'value': '45', 'evidence': 'x', 'source': 'y'}})
        self.assertTrue(AgentTask.objects.get().mcp_session.startswith('day:'))

    def test_apply_from_mcp_task(self):
        from agent import decide
        _result, session = self.start()
        self.rpc('tools/call', {'name': 'propose_change', 'arguments': {
            'sku': 'AN220213-01C', 'field': 'net_weight_g', 'value': '45', 'evidence': 'Weight: 45 g',
            'source': 'даташит'}}, session=session)
        task = AgentTask.objects.get()
        decide.apply_selected(task, list(task.proposals.values_list('pk', flat=True)), self.user)
        self.assertEqual(Product.objects.get(pk=self.ant.pk).net_weight_g, Decimal('45'))
