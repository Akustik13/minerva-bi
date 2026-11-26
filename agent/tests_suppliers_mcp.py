"""🏭 Постачальники, 🪪 візитки, 📨 новий запит і список запитів через MCP і REST API (agent/tools_suppliers.py,
tools_suppliers_mail.py, tools_cards.py, tools_rfq.py; api/suppliers.py, api/agent_tools.py): фільтри як у списках,
пропозиції (новий постачальник, зміни, контакти, дії для вибраних, візитки, копія запиту з файлами) → ✓ людини тими
самими функціями, що сторінки; приватне — лише своє. AI імітується.

python manage.py test agent.tests_suppliers_mcp --settings=tabele.settings_test
"""
import base64
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Permission
from django.core.files.base import ContentFile

from agent import files, tools, toolsets
from agent.models import AgentFile, AgentProposal
from agent.tests_tools_rfq import RFQBase
from api.models import APIKey
from inventory.models import Supplier
from suppliers.models import (SupplierAnalysis, SupplierContact, SupplierEvent, SupplierMedia, SupplierRequest,
                              SupplierRequestFile)

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')
URL = '/api/v1/mcp/'


class SupBase(RFQBase):
    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        self.addCleanup(cache.clear)
        self.key = APIKey.objects.create(name='Claude Code', scopes=['agent:read', 'agent:write'],
                                         agent_user=self.user, owner=self.user, private=True)
        self.shared = APIKey.objects.create(name='Спільний', scopes=['agent:read', 'agent:write'],
                                            agent_user=self.user)
        self.reader = APIKey.objects.create(name='Читання', scopes=['agent:read'], agent_user=self.user)
        self.card = self.make_card(extracted={'name': 'Coax Connectors Ltd', 'email': 'info@coax.example',
                                              'contact_name': 'Lee Wang', 'addr_country': 'CN'},
                                   event='Electronica 2026', marks=['fair'])

    def call(self, tool, ctx=None, **args):           # аргумент інструмента «name» — не плутати з назвою
        out, err = tools.run(ctx or self.ctx(), tool, args)
        self.assertFalse(err, out)
        return out

    def refused(self, tool, ctx=None, **args):
        out, err = tools.run(ctx or self.ctx(), tool, args)
        self.assertTrue(err, out)
        return out

    def make_card(self, user=None, visibility=SupplierMedia.TEAM, **kw):
        m = SupplierMedia(created_by=user or self.user, visibility=visibility, **kw)
        m.image.save('card.png', ContentFile(PNG), save=False)
        m.save()
        return m

    def rpc(self, method, params=None, key=None):
        body = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}
        return self.client.post(URL, json.dumps(body), content_type='application/json',
                                HTTP_AUTHORIZATION=f'Bearer {(key or self.key).key}')

    def tool_names(self, key):
        return {t['name'] for t in self.rpc('tools/list', key=key).json()['result']['tools']}

    def api(self, path, key=None, method='get', data=None):
        auth = {'HTTP_AUTHORIZATION': f'Token {(key or self.key).key}'}
        if method == 'post':
            return self.client.post(path, json.dumps(data or {}), content_type='application/json', **auth)
        return self.client.get(path, data or {}, **auth)


class ToolListTests(SupBase):
    def test_new_tools_and_personal_mail_tools(self):
        names = self.tool_names(self.key)
        for n in ('find_suppliers', 'supplier_filters', 'propose_supplier', 'propose_supplier_contact',
                  'propose_supplier_check', 'find_cards', 'get_card', 'card_stats', 'propose_card',
                  'propose_new_card', 'supplier_request_form', 'preview_rfq_letter', 'find_suppliers_in_mail',
                  'supplier_from_mail'):
            self.assertIn(n, names)
        shared = self.tool_names(self.shared)
        self.assertNotIn('find_suppliers_in_mail', shared)          # пошта — лише 🔒 особистий ключ
        self.assertIn('find_cards', shared)

    def test_viewer_without_change_gets_no_propose(self):
        viewer = self.person('olya', 'inventory', 'suppliers')
        names = {t['name'] for t in toolsets.tools_for(viewer)}
        self.assertIn('find_suppliers', names)
        self.assertIn('find_cards', names)
        self.assertNotIn('propose_supplier_contact', names)
        self.assertNotIn('propose_new_card', names)


class SupplierFindTests(SupBase):
    def test_filters_like_the_list(self):
        self.idle.status = Supplier.STATUS_PAUSED
        self.idle.addr_country = 'DE'
        self.idle.save()
        fair = Supplier.objects.create(name='Coax Fair Co', source='card')
        self.card.supplier = fair
        self.card.save()
        Supplier.objects.create(name='No Cat Ltd')
        out = self.call('find_suppliers', status='paused')
        self.assertIn('Idle Boards', out)
        self.assertNotIn('JLCPCB', out)
        self.assertIn('No Cat Ltd', self.call('find_suppliers', category='none'))
        self.assertNotIn('No Cat Ltd', self.call('find_suppliers', category=f'#{self.pcb.pk}'))
        out = self.call('find_suppliers', source='fair')
        self.assertIn('Coax Fair Co', out)
        self.assertIn('🎪', out)
        self.assertIn('Coax Fair Co', self.call('find_suppliers', where='electronica 2026'))
        self.assertIn('Idle Boards', self.call('find_suppliers', country='de'))
        self.refused('find_suppliers', status='nope')

    def test_filter_values(self):
        self.card.supplier = self.jlc
        self.card.save()
        out = self.call('supplier_filters')
        self.assertIn(f'#{self.pcb.pk}', out)
        self.assertIn('постачальників 3', out)
        self.assertIn('Electronica 2026', out)


class ProposeSupplierTests(SupBase):
    def test_new_supplier_with_contact_and_categories(self):
        ctx = self.ctx()
        out = self.call('propose_supplier', ctx, name='Rosenberger Hochfrequenztechnik', email='info@rosenberger.example',
                        website='rosenberger.example', status='potential', rating=4, categories=[self.pcb.name],
                        contacts=[{'name': 'Anna Huber', 'email': 'anna@rosenberger.example', 'position': 'Sales'}],
                        history_note='Зустрічали на Electronica', evidence='візитка')
        self.assertIn('Rosenberger', out)
        self.assertFalse(Supplier.objects.filter(name__startswith='Rosenberger').exists())   # лише пропозиція
        p, res = self.apply_last(ctx)
        self.assertEqual(p.status, AgentProposal.APPLIED, (p.error, res))
        s = Supplier.objects.get(name='Rosenberger Hochfrequenztechnik')
        self.assertEqual((s.status, s.rating, s.source, s.website), ('potential', 4, 'ai', 'https://rosenberger.example'))
        self.assertEqual(list(s.categories.all()), [self.pcb])
        self.assertTrue(s.contacts.filter(email='anna@rosenberger.example', position='Sales').exists())
        self.assertTrue(SupplierEvent.objects.filter(supplier=s, kind=SupplierEvent.CREATED,
                                                     text__startswith='🤖 Створено').exists())
        self.assertTrue(SupplierEvent.objects.filter(supplier=s, text='🤖 Зустрічали на Electronica').exists())
        self.assertEqual(p.result_url, f'/admin/inventory/supplier/{s.pk}/change/')

    def test_duplicate_refused(self):
        out = self.refused('propose_supplier', name='PCBWay Europe', email='eu@pcbway.com')
        self.assertIn(f'#{self.supplier.pk}', out)                 # той самий домен — уже є
        self.refused('propose_supplier', name=self.supplier.name.upper())
        self.call('propose_supplier', name='PCBWay Europe', email='eu@pcbway.com', allow_duplicate=True)

    def test_edit_fields_categories_note_and_stale(self):
        ctx = self.ctx()
        self.call('propose_supplier', ctx, supplier=f'#{self.idle.pk}', rating=2, payment_terms='Prepayment',
                  remove_categories=[self.pcb.name], history_note='Ціни зросли на 20 %')
        p, _ = self.apply_last(ctx)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        self.idle.refresh_from_db()
        self.assertEqual((self.idle.rating, self.idle.payment_terms), (2, 'Prepayment'))
        self.assertFalse(self.idle.categories.exists())
        self.assertTrue(SupplierEvent.objects.filter(supplier=self.idle, text__contains='Ціни зросли').exists())
        ctx = self.ctx()
        self.call('propose_supplier', ctx, supplier=f'#{self.idle.pk}', rating=5)
        Supplier.objects.filter(pk=self.idle.pk).update(rating=3)       # людина змінила раніше за ✓
        p, _ = self.apply_last(ctx)
        self.assertEqual(p.status, AgentProposal.FAILED)
        self.assertIn('після пропозиції', p.error)

    def test_from_mail_only_with_personal(self):
        msg = self.mail(frm='lee@newfab.example', subject='Our quotation', name='Lee Fab',
                        body='Dear Slava,\nprice 2 USD\n\nBest regards\nLee Chen\nNewFab Electronics Ltd\n+86 755 1234')
        self.refused('propose_supplier', self.ctx(personal=False), message=msg.pk, name='NewFab')
        other_msg = self.mail(account=self.other_account, frm='x@other.example')
        self.refused('propose_supplier', message=other_msg.pk, name='Other')           # чужа скринька
        out = self.call('supplier_from_mail', message=msg.pk)
        self.assertIn('lee@newfab.example', out)
        ctx = self.ctx()
        self.call('propose_supplier', ctx, message=msg.pk, name='NewFab Electronics')
        p, _ = self.apply_last(ctx)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        s = Supplier.objects.get(name='NewFab Electronics')
        self.assertEqual((s.source, s.email), ('mail', 'lee@newfab.example'))
        ev = SupplierEvent.objects.get(supplier=s, kind=SupplierEvent.CREATED)
        self.assertEqual(ev.message_id, msg.pk)

    def test_contact_new_and_primary(self):
        ctx = self.ctx()
        self.call('propose_supplier_contact', ctx, supplier='JLCPCB', name='Tom', email='tom@jlcpcb.com',
                  position='Engineer')
        p, _ = self.apply_last(ctx)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        tom = SupplierContact.objects.get(email='tom@jlcpcb.com')
        self.assertEqual(tom.source, 'ai')
        self.refused('propose_supplier_contact', supplier='JLCPCB', name='Tom 2', email='tom@jlcpcb.com')
        ctx = self.ctx()
        self.call('propose_supplier_contact', ctx, supplier='JLCPCB', contact='tom@jlcpcb.com', is_primary=True)
        p, _ = self.apply_last(ctx)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        self.mia.refresh_from_db()
        tom.refresh_from_db()
        self.assertTrue(tom.is_primary)
        self.assertFalse(self.mia.is_primary)

    def test_review_page_shows_supplier_and_card_proposals(self):
        ctx = self.ctx()
        self.call('propose_supplier', ctx, supplier='JLCPCB', rating=5)
        self.call('propose_supplier_contact', ctx, supplier='JLCPCB', name='Tom', email='tom@jlcpcb.com')
        self.call('propose_card', ctx, card=self.card.pk, action='to_supplier')
        r = self.client.get(f'/admin/agent/agenttask/{ctx.task.pk}/review/')
        self.assertEqual(r.status_code, 200)
        page = r.content.decode()
        for text in ('🏭 Постачальник', '🏭 Контактна особа постачальника', '🪪 🏭 У постачальники'):
            self.assertIn(text, page)

    def test_check_research_in_background_and_history_needs_personal(self):
        self.refused('propose_supplier_check', self.ctx(personal=False), action='history', suppliers=['JLCPCB'])
        ctx = self.ctx()
        self.call('propose_supplier_check', ctx, action='research', suppliers=['JLCPCB', f'#{self.idle.pk}'])
        answer = json.dumps({'summary': 'Великий виробник плат', 'sources': []})
        with mock.patch('suppliers.services.ai.call', return_value=(answer, Decimal('0.01'))):
            p, _ = self.apply_last(ctx)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        self.assertEqual(SupplierAnalysis.objects.filter(kind=SupplierAnalysis.WEB).count(), 2)


class CardTests(SupBase):
    def test_find_get_and_privacy(self):
        mine_private = self.make_card(visibility=SupplierMedia.PRIVATE, extracted={'name': 'Secret Fab'})
        theirs_private = self.make_card(user=self.other, visibility=SupplierMedia.PRIVATE,
                                        extracted={'name': 'Hidden Fab'})
        out = self.call('find_cards', show='new')
        self.assertIn('Coax Connectors Ltd', out)
        self.assertIn('Secret Fab', out)
        self.assertNotIn('Hidden Fab', out)                       # чужа 🔒 — не видно нікому
        self.refused('get_card', card=theirs_private.pk)
        self.assertIn('Electronica 2026', self.call('find_cards', event='electronica 2026'))
        out = self.call('get_card', card=self.card.pk)
        self.assertIn(f'card:{self.card.pk}', out)
        self.assertIn(f'vcard:{self.card.pk}', out)
        name, data, ctype = files.open_file(self.user, f'card:{self.card.pk}')
        self.assertEqual((data, ctype), (PNG, 'image/png'))
        self.assertIn(b'BEGIN:VCARD', files.open_file(self.user, f'vcard:{self.card.pk}')[1])
        with self.assertRaises(files.FileError):
            files.open_file(self.user, f'card:{theirs_private.pk}')
        self.assertIn('Electronica 2026', self.call('card_stats'))
        self.assertTrue(mine_private.pk)

    def test_edit_with_data_read_by_agent(self):
        ctx = self.ctx()
        self.call('propose_card', ctx, card=self.card.pk, action='edit', role='supplier', note='Стенд B4',
                  data={'website': 'www.coax.example', 'contact_position': 'Sales Manager'},
                  categories=[self.pcb.name])
        p, _ = self.apply_last(ctx)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        self.card.refresh_from_db()
        self.assertEqual((self.card.role, self.card.note), ('supplier', 'Стенд B4'))
        self.assertEqual(self.card.extracted['contact_position'], 'Sales Manager')
        self.assertIn('website', self.card.extracted['from_agent'])
        self.assertEqual(list(self.card.categories.all()), [self.pcb])

    def test_to_supplier_creates_and_other_users_card_refused(self):
        ctx = self.ctx()
        self.call('propose_card', ctx, card=self.card.pk, action='to_supplier')
        p, _ = self.apply_last(ctx)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        s = Supplier.objects.get(name='Coax Connectors Ltd')
        self.card.refresh_from_db()
        self.assertEqual(self.card.supplier_id, s.pk)
        self.assertTrue(s.contacts.filter(name='Lee Wang').exists())
        adder = self.person('ivan', 'inventory', 'suppliers', ops={'inventory': ['view', 'add']})
        foreign = self.make_card(extracted={'name': 'Foreign'})
        self.refused('propose_card', self.ctx(owner=adder), card=foreign.pk, action='delete')

    def test_new_card_from_uploaded_photo(self):
        f = files.save(self.user, 'visit.png', PNG, 'image/png')
        ctx = self.ctx()
        out = self.call('propose_new_card', ctx, image=f.ref, event='Productronica', marks=['fair'],
                        data={'name': 'Würth Elektronik', 'email': 'info@we.example'})
        self.assertIn('visit.png', out)
        before = SupplierMedia.objects.count()
        p, _ = self.apply_last(ctx)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        m = SupplierMedia.objects.order_by('-pk').first()
        self.assertEqual(SupplierMedia.objects.count(), before + 1)
        self.assertEqual((m.event, m.marks, m.extracted['name'], m.source),
                         ('Productronica', ['fair'], 'Würth Elektronik', SupplierMedia.S_OTHER))
        txt = files.save(self.user, 'a.txt', b'hello', 'text/plain')
        self.refused('propose_new_card', image=txt.ref)            # не картинка


class RFQFormTests(SupBase):
    def test_form_and_preview(self):
        out = self.call('supplier_request_form')
        self.assertIn('pcb', out)
        self.assertIn('layers', out)
        self.assertIn('{greeting}', out)
        out = self.call('supplier_request_form', template='cable')
        self.assertNotIn('layers', out)
        out = self.call('preview_rfq_letter', request=self.req.code, supplier='JLCPCB')
        self.assertIn('Кому: mia@jlcpcb.com', out)
        self.assertIn('Please quote 4-layer PCB.', out)
        out = self.call('preview_rfq_letter', supplier='Idle Boards', title='Кабель RG316', template='cable',
                        language='de')
        self.assertIn('info@idle.example', out)
        self.assertIn('Kabel RG316', out.replace('Кабель', 'Kabel'))
        self.assertEqual(SupplierRequest.objects.count(), 1)       # нічого не збережено

    def test_copy_with_files_and_review(self):
        SupplierRequestFile.objects.create(request=self.req, name='stackup.pdf', size=4,
                                           file=ContentFile(b'%PDF', name='stackup.pdf'))
        up = files.save(self.user, 'gerber.zip', b'PK\x03\x04zip', 'application/zip')
        ctx = self.ctx()
        out = self.call('propose_supplier_request', ctx, copy_from=self.req.code, quantities='1000',
                        files=[up.ref])
        self.assertIn(f'📋 з {self.req.code}', ctx.task.proposals.get().new_value)
        p, _ = self.apply_last(ctx)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        new = SupplierRequest.objects.exclude(pk=self.req.pk).get()
        self.assertEqual((new.copied_from_id, new.template, new.quantities, new.specs.get('layers')),
                         (self.req.pk, 'pcb', '1000', '4'))
        self.assertEqual(sorted(new.files.values_list('name', flat=True)), ['gerber.zip', 'stackup.pdf'])
        self.assertEqual(set(new.recipients.values_list('supplier_id', flat=True)), {self.supplier.pk, self.jlc.pk})
        self.assertTrue(new.review)                                 # 📋 що перевірити — як після «Копіювати»
        self.assertNotIn('quantities', new.review)                  # змінене — уже не на перевірці
        out = self.call('find_supplier_requests', status='draft')
        self.assertIn(f'📋 з {self.req.code}', out)
        self.assertIn('⚠️ перевірити', out)

    def test_edit_adds_and_removes_files(self):
        old = SupplierRequestFile.objects.create(request=self.req, name='old.pdf', size=4,
                                                 file=ContentFile(b'%PDF', name='old.pdf'))
        up = files.save(self.user, 'bom.csv', b'ref;qty', 'text/csv')
        ctx = self.ctx()
        self.call('propose_supplier_request', ctx, request=self.req.code, files=[up.ref], remove_files=[old.pk])
        p, _ = self.apply_last(ctx)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        self.assertEqual(list(self.req.files.values_list('name', flat=True)), ['bom.csv'])
        self.refused('propose_supplier_request', request=self.req.code, remove_files=[99999])

    def test_find_by_group_and_dates(self):
        SupplierRequest.objects.filter(pk=self.req.pk).update(group='Антени 2026')
        self.assertIn(self.req.code, self.call('find_supplier_requests', status='all', group='Антени'))
        self.assertNotIn(self.req.code, self.call('find_supplier_requests', status='all', created_from='2099-01-01'))


class RestTests(SupBase):
    def test_suppliers_cards_requests_json(self):
        r = self.api('/api/v1/suppliers/', data={'category': f'#{self.pcb.pk}', 'limit': 2})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['count'], 3)
        self.assertEqual(len(r.json()['results']), 2)
        r = self.api(f'/api/v1/suppliers/{self.jlc.pk}/')
        self.assertEqual(r.json()['contacts'][0]['email'], 'mia@jlcpcb.com')
        self.assertEqual(r.json()['requests'][0]['code'], self.req.code)
        self.assertIn('categories', self.api('/api/v1/suppliers/filters/').json())
        r = self.api('/api/v1/suppliers/cards/', data={'show': 'new'})
        self.assertEqual(r.json()['results'][0]['company'], 'Coax Connectors Ltd')
        r = self.api(f'/api/v1/suppliers/cards/{self.card.pk}/')
        self.assertIn(f'card:{self.card.pk}', [f['ref'] for f in r.json()['files']])
        self.assertEqual(self.api('/api/v1/suppliers/cards/stats/').json()['total']['cards'], 1)
        r = self.api('/api/v1/supplier-requests/', data={'status': 'all'})
        self.assertEqual(r.json()['results'][0]['code'], self.req.code)
        r = self.api(f'/api/v1/supplier-requests/{self.req.code}/')
        rec = {x['supplier']['name']: x for x in r.json()['recipients_list']}
        self.assertEqual(rec['Shenzhen PCBWay Co., Ltd']['quote']['tiers'][0]['qty'], 100)
        self.assertEqual(self.api('/api/v1/supplier-requests/RFQ-0000-000/').status_code, 404)
        forms = self.api('/api/v1/supplier-requests/form/').json()
        self.assertIn('pcb', [t['key'] for t in forms['templates']])

    def test_rights_of_the_key_person(self):
        nobody = self.person('nadia', 'sales')
        key = APIKey.objects.create(name='Без складу', scopes=['agent:read'], agent_user=nobody)
        nobody.user_permissions.add(*Permission.objects.filter(codename='add_agenttask'))
        self.assertEqual(self.api('/api/v1/suppliers/', key=key).status_code, 403)

    def test_tool_bridge_read_write(self):
        r = self.api('/api/v1/agent/tools/', key=self.reader)
        self.assertIn('find_suppliers', [t['name'] for t in r.json()['tools']])
        r = self.api('/api/v1/agent/tools/find_suppliers/', key=self.reader, method='post',
                     data={'query': 'JLC'})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn('JLCPCB', r.json()['text'])
        r = self.api('/api/v1/agent/tools/propose_supplier/', key=self.reader, method='post',
                     data={'name': 'Bridge Fab'})
        self.assertEqual(r.status_code, 403)                          # propose_* — лише agent:write
        r = self.api('/api/v1/agent/tools/propose_supplier/', method='post',
                     data={'arguments': {'name': 'Bridge Fab', 'evidence': 'сайт'}})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.json()['is_error'], r.json())
        self.assertIn('/review/', r.json()['review_url'])
        self.assertEqual(AgentProposal.objects.filter(kind=AgentProposal.SUPPLIER).count(), 1)
        self.assertEqual(self.api('/api/v1/agent/tools/nope/', method='post').status_code, 404)
        r = self.api('/api/v1/agent/tools/find_suppliers_in_mail/', key=self.shared, method='post')
        self.assertEqual(r.status_code, 404)                          # пошта — лише 🔒 особистий ключ

    def test_file_list_for_card(self):
        r = self.api('/api/v1/files/', data={'card': self.card.pk})
        self.assertEqual(r.status_code, 200, r.content)
        refs = [x['ref'] for x in r.json()['files']]
        self.assertIn(f'card:{self.card.pk}', refs)
        self.assertFalse(AgentFile.objects.exists())
