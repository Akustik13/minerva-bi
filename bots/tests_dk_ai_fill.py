"""🤖 AI-заповнення картки DigiKey (bots/services/dk_ai_fill.py) і на сторінці «🔷 На DigiKey зі складу»:
Claude підмінено сценарієм, DigiKey — як у tests_from_stock.

python manage.py test bots.tests_dk_ai_fill --settings=tabele.settings_test
"""
import json
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse

from bots.models import DigiKeyListing
from bots.services import dk_ai_fill
from bots.tests_from_stock import FromStockBase
from inventory.services import ai_fill
from inventory.tests_ai_fill import FakeClient, resp, submit, tool


class DkFillBase(FromStockBase):
    def setUp(self):
        super().setUp()
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
        self.url = reverse('admin:bots_digikeylisting_ai_fill')

    def go(self, script, **post):
        fake = FakeClient(script)
        with mock.patch('inventory.services.ai_fill._client', return_value=fake):
            r = self.client.post(self.url, {'web': '0', 'model': 'claude-sonnet-5-5', **post})
        return r, fake


class DigiKeyFillTests(DkFillBase):
    def test_category_tools_and_clean(self):
        r, fake = self.go([
            resp('tool_use', tool('t1', 'dk_find_category', query='coaxial adapter')),
            resp('tool_use', tool('t2', 'dk_category_fields', category_id='374')),
            resp('tool_use', submit(
                fields=[{'key': 'dk_title', 'value': 'SMA female to N male adapter, straight, 50 Ohm, 6 GHz, brass',
                         'source': 'PDF'},
                        {'key': 'dk_category_id', 'value': '374', 'source': 'каталог'},
                        {'key': 'dk_packaging', 'value': 'bulk', 'source': 'як наші'},
                        {'key': 'dk_lifecycle_status', 'value': 'Forever', 'source': '?'},
                        {'key': 'dk_image_url', 'value': 'not a url', 'source': '?'},
                        {'key': 'category_type', 'value': 'connector', 'source': 'вид'}],
                attributes=[{'code': 'rohsStatus', 'value': 'rohs3 compliant', 'source': 'PDF'},
                            {'code': '2083', 'value': 'SMA', 'source': 'PDF'},
                            {'code': '555', 'value': 'x', 'source': '?'}]))],
            pn='AD-SMAF-NM', text='adapter SMA female / N male')
        job = r.json()
        self.assertEqual(job['status'], 'done', job)
        found = fake.calls[1]['messages'][-1]['content'][0]['content']
        self.assertIn('ID 374', found)                                           # пошук у каталозі
        fields_text = fake.calls[2]['messages'][-1]['content'][0]['content']
        self.assertIn('rohsStatus', fields_text)
        self.assertIn('лише одне з: ROHS3 Compliant; Non-RoHS', fields_text)
        res = job['result']
        f = {x['key']: x for x in res['fields']}
        self.assertLessEqual(len(f['dk_title']['value']), 50)
        self.assertEqual(f['dk_category_name']['value'], 'Coaxial Connector (RF) Adapters')
        self.assertEqual(f['dk_category_name']['follow'], 'dk_category_id')
        self.assertEqual(f['dk_packaging']['value'], 'Bulk')
        self.assertNotIn('dk_lifecycle_status', f)
        self.assertNotIn('dk_image_url', f)
        attrs = {a['key']: a['value'] for a in res['attributes']}
        self.assertEqual(attrs, {'rohsStatus': 'ROHS3 Compliant', '2083': 'SMA'})
        self.assertTrue(any('555' in d for d in res['dropped']))

    def test_unknown_category_and_no_category_attributes(self):
        r, _ = self.go([resp('tool_use', submit(
            fields=[{'key': 'dk_category_id', 'value': '99999', 'source': '?'}],
            attributes=[{'code': '2083', 'value': 'SMA', 'source': 'PDF'}]))], pn='X')
        res = r.json()['result']
        self.assertEqual(res['fields'], [])
        self.assertEqual(res['attributes'], [])
        self.assertTrue(any('без категорії' in d for d in res['dropped']))

    def test_listing_context_and_category_from_listing(self):
        listing = DigiKeyListing.objects.get(product__sku='FT220401')
        listing.dk_category_id = '374'
        listing.save()
        r, fake = self.go([resp('tool_use', submit(attributes=[{'code': '2083', 'value': 'N-Type', 'source': 'x'}]))],
                          listing_id=str(listing.pk))
        self.assertIn('КАРТКА DIGIKEY ЗАРАЗ', fake.calls[0]['messages'][0]['content'][-1]['text'])
        self.assertEqual(r.json()['result']['attributes'][0]['value'], 'N-Type')

    def test_pages_show_panel(self):
        add = self.client.get(reverse('admin:bots_digikeylisting_add')).content.decode()
        self.assertIn('mvaf-config', add)
        page = self.client.get(reverse('admin:bots_digikeylisting_from_stock') + f'?ids={self.product.pk}')
        html = page.content.decode()
        self.assertIn(f'data-pk="{self.product.pk}"', html)
        self.assertIn(f'name="p{self.product.pk}-aisrc"', html)

    def test_from_stock_preview_marks_ai_values(self):
        pk = self.product.pk
        src = {'rohsStatus': {'value': 'ROHS3 Compliant', 'source': 'PDF стор. 2'},
               'packaging': {'value': 'Bulk', 'source': 'змінено'}}
        r = self.client.post(reverse('admin:bots_digikeylisting_from_stock'), {
            'ids': str(pk), 'action': 'preview', f'p{pk}-on': '1', f'p{pk}-cat': 'Filters',
            f'p{pk}-f-rohsStatus': 'ROHS3 Compliant', f'p{pk}-f-packaging': 'Tape & Reel (TR)',
            f'p{pk}-aisrc': json.dumps(src)})
        self.assertEqual(r.status_code, 200)
        card = r.context['cards'][0]
        self.assertEqual(card['draft']['sources']['rohsStatus'], '🤖 AI · PDF стор. 2')
        self.assertEqual(card['draft']['sources']['packaging'], 'вписано вручну')  # людина змінила після AI
        self.assertIn('ROHS3 Compliant', r.content.decode())

    def test_find_category_without_catalog(self):
        with mock.patch('bots.services.dk_ai_fill._catalog', return_value={}):
            text = dk_ai_fill.find_category('filter')
        self.assertIn('ID Filters', text)                                         # наші категорії лістингів
