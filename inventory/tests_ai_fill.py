"""🤖 AI-заповнення картки товару (inventory/services/ai_fill.py, inventory/admin_ai_fill.py): Claude підмінено
сценарієм, мережі немає.

python manage.py test inventory.tests_ai_fill --settings=tabele.settings_test
"""
from types import SimpleNamespace as NS
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from core.shared_cache import cache
from email_assistant.tests_attachment_links import pdf_bytes
from inventory.models import Product, ProductCategory
from inventory.services import ai_fill

PDF = pdf_bytes('AN220213 Antenna 2.4 GHz', 'Gain: 3 dBi', 'Weight: 45 g', 'Made in Germany')


def tool(id_, name, **inp):
    return NS(type='tool_use', id=id_, name=name, input=inp)


def server(name, **inp):
    return NS(type='server_tool_use', id='s1', name=name, input=inp)


def text(t):
    return NS(type='text', text=t)


def resp(stop, *blocks, searches=0):
    return NS(stop_reason=stop, content=list(blocks), usage=NS(
        input_tokens=2000, output_tokens=300, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        server_tool_use=NS(web_search_requests=searches, web_fetch_requests=0) if searches else None))


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.messages = self

    def create(self, **kw):
        self.calls.append({**kw, 'messages': list(kw['messages'])})
        return self.script.pop(0) if self.script else resp('end_turn', text('…'))


def submit(**kw):
    return tool('fin', 'submit_card', **{'summary': 'Антена 2,4 ГГц', 'fields': [], 'attributes': [], **kw})


class Base(TestCase):
    def setUp(self):
        cache.clear()
        from config.models import SystemSettings
        from strategy.models import AISettings
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        s = AISettings.get()
        s.anthropic_api_key = 'sk-test-x'
        s.save()
        old, ai_fill.BACKGROUND = ai_fill.BACKGROUND, False
        self.addCleanup(setattr, ai_fill, 'BACKGROUND', old)
        self.user = User.objects.create_superuser('slava', 's@x.y', 'p')
        self.client.force_login(self.user)
        ProductCategory.objects.create(slug='antennas', name='Антени')
        Product.objects.create(sku='AN220214-01A', name='Антена 5 ГГц', name_export='Antenna 5 GHz SMA',
                               category='antennas', tech_attributes={'Frequency Range': '5150 ~ 5850 MHz'})
        self.url = reverse('admin:inventory_product_ai_fill')

    def go(self, script, **post):
        fake = FakeClient(script)
        data = {'web': '0', 'model': 'claude-opus-5-5', **post}
        with mock.patch('inventory.services.ai_fill._client', return_value=fake):
            r = self.client.post(self.url, data)
        return r, fake


class ProductFillTests(Base):
    def test_datasheet_to_fields_with_checks(self):
        r, fake = self.go([
            resp('tool_use', tool('t1', 'similar_products', query='antenna SMA')),
            resp('tool_use', submit(
                fields=[{'key': 'sku', 'value': 'AN220213-01C', 'source': 'текст людини'},
                        {'key': 'name_export', 'value': 'Antenna  2.4 GHz SMA', 'source': 'PDF стор. 1'},
                        {'key': 'category', 'value': 'Антени', 'source': 'як схожі'},
                        {'key': 'hs_code', 'value': '8529.10.20', 'source': 'PDF', 'confidence': 'low'},
                        {'key': 'net_weight_g', 'value': '45', 'source': 'PDF стор. 1'},
                        {'key': 'country_of_origin', 'value': 'Germany', 'source': 'PDF'},
                        {'key': 'lifecycle_status', 'value': 'active', 'source': 'PDF'}],
                attributes=[{'name': 'Gain', 'value': '3 dBi', 'source': 'PDF'},
                            {'name': 'rohsStatus', 'value': 'ok', 'source': 'PDF'}],
                doubts=['Країну вказано словом'], links=[{'url': 'javascript:alert(1)'},
                                                         {'url': 'https://example.com/an', 'title': 'AN'}]))],
            pn='AN220213-01C', text='антена для роутера',
            files=SimpleUploadedFile('AN220213.pdf', PDF, content_type='application/pdf'))
        self.assertEqual(r.status_code, 200, r.content)
        job = r.json()
        self.assertEqual(job['status'], 'done', job)
        res = job['result']
        fields = {f['key']: f for f in res['fields']}
        self.assertEqual(fields['hs_code']['value'], '85291020')               # agent.products.clean
        self.assertEqual(fields['hs_code']['confidence'], 'low')
        self.assertEqual(fields['category']['value'], 'antennas')               # назва → slug довідника
        self.assertEqual(fields['name_export']['value'], 'Antenna 2.4 GHz SMA')
        self.assertNotIn('country_of_origin', fields)                          # «Germany» — не ISO-2
        self.assertTrue(any('Країна' in d for d in res['dropped']))
        self.assertEqual([a['key'] for a in res['attributes']], ['Gain'])       # службовий код — ні
        self.assertEqual(res['links'], [{'url': 'https://example.com/an', 'title': 'AN'}])
        first = fake.calls[0]
        content = first['messages'][0]['content']
        self.assertEqual(content[0]['type'], 'document')                        # PDF — документом для Claude
        self.assertEqual(content[0]['source']['media_type'], 'application/pdf')
        self.assertIn('AN220213-01C', content[-1]['text'])
        self.assertNotIn('web_search', [t.get('name') for t in first['tools']])  # 🌐 вимкнено
        tool_result = fake.calls[1]['messages'][-1]['content'][0]
        self.assertIn('AN220214-01A', tool_result['content'])                   # схожий товар складу
        self.assertGreater(job['cost_usd'], 0)
        self.assertFalse(Product.objects.filter(sku='AN220213-01C').exists())  # нічого не збережено

    def test_existing_sku_warns(self):
        r, _ = self.go([resp('tool_use', submit(fields=[{'key': 'sku', 'value': 'an220214-01a', 'source': 'x'}]))],
                       pn='AN220214-01A')
        self.assertEqual(r.json()['result']['exists']['sku'], 'AN220214-01A')

    def test_web_tools_by_model(self):
        _, fake = self.go([resp('pause_turn', server('web_search', query='AN220213 datasheet'), searches=1),
                           resp('tool_use', submit())], pn='AN220213', web='1')
        types = {t.get('name'): t.get('type') for t in fake.calls[0]['tools']}
        self.assertEqual(types['web_search'], 'web_search_20260209')
        self.assertEqual(types['web_fetch'], 'web_fetch_20260209')
        self.assertEqual(len(fake.calls), 2)                                    # pause_turn — далі
        _, fake = self.go([resp('tool_use', submit())], pn='AN220213', web='1', model='claude-haiku-5-5')
        types = {t.get('name'): t.get('type') for t in fake.calls[0]['tools']}
        self.assertEqual(types['web_search'], 'web_search_20250305')

    def test_nudge_then_submit_and_refusal(self):
        r, fake = self.go([resp('end_turn', text('Ось картка…')), resp('tool_use', submit())], pn='X1')
        self.assertEqual(r.json()['status'], 'done')
        self.assertIn('submit_card', fake.calls[1]['messages'][-1]['content'])
        r, _ = self.go([resp('refusal')], pn='X1')
        self.assertEqual(r.json()['status'], 'error')
        self.assertIn('відмовився', r.json()['error'])

    def test_links_fetched_by_minerva(self):
        from rag_assistant.knowledge_items import ItemError

        def download(url):
            if 'bad' in url:
                raise ItemError('Адреса локальної мережі')
            return url, b'<html><body><h1>AN220213</h1><p>Gain 3 dBi</p></body></html>', 'text/html'

        with mock.patch('rag_assistant.knowledge_items._download', side_effect=download):
            r, fake = self.go([resp('tool_use', submit())],
                              links='https://maker.example/an220213\nhttp://bad.local/x ftp://nope')
        res = r.json()['result']
        prompt = fake.calls[0]['messages'][0]['content'][-1]['text']
        self.assertIn('Gain 3 dBi', prompt)
        self.assertEqual(res['unread'][0]['url'], 'http://bad.local/x')
        self.assertEqual([x['kind'] for x in res['read']], ['text'])

    def test_refusals_without_data_key_or_right(self):
        r, _ = self.go([])
        self.assertEqual(r.status_code, 400)
        self.assertIn('немає даних', r.json()['error'])
        self.assertEqual(self.client.get(self.url).status_code, 405)
        r, _ = self.go([], files=SimpleUploadedFile('virus.exe', b'MZ\x00\x00binary', content_type='x/y'))
        self.assertEqual(r.status_code, 400)
        from strategy.models import AISettings
        AISettings.objects.update(anthropic_api_key='')
        r, _ = self.go([], pn='X1')
        self.assertIn('ключа AI', r.json()['error'])

    def test_rights_and_owner_only(self):
        r, _ = self.go([resp('tool_use', submit())], pn='X1')
        status = reverse('admin:inventory_product_ai_fill_status', args=[r.json()['id']])
        self.assertEqual(self.client.get(status).json()['status'], 'done')
        self.client.force_login(User.objects.create_superuser('olena', 'o@x.y', 'p'))
        self.assertEqual(self.client.get(status).status_code, 404)              # чуже — не видно й адміністратору
        viewer = User.objects.create_user('ivan', 'i@x.y', 'p', is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename='view_product'))
        self.client.force_login(viewer)
        r, _ = self.go([], pn='X1')
        self.assertEqual(r.status_code, 403)

    def test_change_page_has_panel_with_datasheet_option(self):
        p = Product.objects.get(sku='AN220214-01A')
        p.datasheet_url = 'https://example.com/an.pdf'
        p.save()
        page = self.client.get(reverse('admin:inventory_product_change', args=[p.pk])).content.decode()
        self.assertIn('id="mvaf-host"', page)
        self.assertIn('"has_datasheet": true', page)
        add = self.client.get(reverse('admin:inventory_product_add')).content.decode()
        self.assertIn('mv_ai_fill.js', add)

    def test_product_datasheet_used_when_asked(self):
        p = Product.objects.get(sku='AN220214-01A')
        with mock.patch('agent.tools._datasheet_bytes', return_value=(PDF, 'файл даташита в Minerva')):
            _, fake = self.go([resp('tool_use', submit())], product_id=str(p.pk), datasheet='1')
        content = fake.calls[0]['messages'][0]['content']
        self.assertEqual(content[0]['type'], 'document')
        self.assertIn('AN220214-01A', content[-1]['text'])                      # дані картки — у запиті


class StaleJobTests(TestCase):
    def test_running_without_beat_becomes_error(self):
        import datetime

        from django.utils import timezone
        user = User.objects.create_user('ivan', 'i@x.y', 'p')
        old = (timezone.now() - datetime.timedelta(minutes=30)).isoformat()
        cache.set(ai_fill.KEY.format('j1'), {'id': 'j1', 'owner': user.pk, 'status': 'running', 'started': old,
                                             'beat': old, 'steps': []})
        job = ai_fill.state('j1', user)
        self.assertEqual(job['status'], 'error')
        self.assertIn('перервано', job['error'])


class FilePartTests(TestCase):
    def test_kinds(self):
        self.assertEqual(ai_fill.file_part('a.pdf', PDF)['kind'], 'pdf')
        part = ai_fill.file_part('notes.txt', 'Частота 2,4 ГГц'.encode())
        self.assertEqual(part['text'], 'Частота 2,4 ГГц')
        html = ai_fill.file_part('page.html', b'<html><body><p>VSWR 1.5</p><script>x()</script></body></html>')
        self.assertIn('VSWR 1.5', html['text'])
        with self.assertRaises(ai_fill.FillError):
            ai_fill.file_part('empty.txt', b'')
        self.assertEqual(ai_fill.parse_links('a https://x.y/1, https://x.y/1 <http://z.w/2> ftp://q'),
                         ['https://x.y/1', 'http://z.w/2'])
