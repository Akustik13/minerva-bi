"""📨 Запити постачальникам (RFQ) у MCP (agent/tools_rfq.py, tools_rfq_mail.py): читання з правами людини,
порівняння, файли; пропозиції (новий запит, зміни, отримувачі, ціни, стан, рішення, закупівля, надсилання,
нагадування) → ✓ людини тими самими функціями, що сторінка запиту; листування — лише свої скриньки й лише
🔒 особистий ключ. SMTP імітується.

python manage.py test agent.tests_tools_rfq --settings=tabele.settings_test
"""
import datetime
import json
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.utils import timezone

from agent import decide, mcp, tools, tools_rfq, tools_rfq_mail, toolsets
from agent.models import AgentProposal, AgentTask
from api.models import APIKey
from inventory.models import PurchaseOrder, Supplier
from suppliers.models import (SupplierContact, SupplierEvent, SupplierReplyFile, SupplierRequest,
                              SupplierRequestFile, SupplierRequestRecipient as R)
from suppliers.tests import Base

URL = '/api/v1/mcp/'


_SENT = [0]


def ok_send(sent):
    def fake(client, to_emails, subject, body_text, **kw):
        sent.append((list(to_emails), subject, body_text, kw.get('cc_emails')))
        _SENT[0] += 1
        return {'ok': True, 'message_id': f'<rfq-test-{_SENT[0]}@sevskiy.de>'}
    return fake


class RFQBase(Base):
    def setUp(self):
        super().setUp()
        self.supplier.categories.add(self.pcb)
        self.jlc = Supplier.objects.create(name='JLCPCB', email='sales@jlcpcb.com')
        self.jlc.categories.add(self.pcb)
        self.mia = SupplierContact.objects.create(supplier=self.jlc, name='Mia', email='mia@jlcpcb.com',
                                                  is_primary=True)
        self.idle = Supplier.objects.create(name='Idle Boards', email='info@idle.example')
        self.idle.categories.add(self.pcb)
        self.req = SupplierRequest.objects.create(
            title='PCB TM270402', template='pcb', category=self.pcb, quantities='100; 500', owner=self.user,
            account=self.account, status=SupplierRequest.SENT, sent_at=timezone.now() - datetime.timedelta(days=3),
            reply_by=timezone.localdate() - datetime.timedelta(days=1), specs={'layers': '4', 'finish': 'ENIG'},
            subject='[RFQ] PCB', body='{greeting}\n\nPlease quote 4-layer PCB.')
        self.r1 = R.objects.create(request=self.req, supplier=self.supplier, email='sales@pcbway.com',
                                   state=R.QUOTED, currency='USD', sent_at=self.req.sent_at,
                                   replied_at=timezone.now() - datetime.timedelta(days=1),
                                   tiers=[{'qty': 100, 'unit_price': '1.2000'}, {'qty': 500, 'unit_price': '0.9000'}],
                                   lead_days=10, ai={'summary': 'Quoted 1.20 / 0.90 USD'})
        self.r2 = R.objects.create(request=self.req, supplier=self.jlc, contact=self.mia, email='mia@jlcpcb.com',
                                   state=R.SENT, sent_at=self.req.sent_at)
        self.ctx_task = None

    def ctx(self, owner=None, personal=True):
        task = AgentTask.objects.create(owner=owner or self.user, title='t', instruction='t')
        c = tools.Context(task)
        c.personal = personal
        return c

    def call(self, name, ctx=None, **args):
        out, err = tools.run(ctx or self.ctx(), name, args)
        self.assertFalse(err, out)
        return out

    def refused(self, name, ctx=None, **args):
        out, err = tools.run(ctx or self.ctx(), name, args)
        self.assertTrue(err, out)
        return out

    def person(self, name, *modules, ops=None):
        u = User.objects.create_user(name, f'{name}@x.y', 'p', is_staff=True)
        u.profile.role, u.profile.allowed_modules, u.profile.module_operations = 'readonly', list(modules), ops
        u.profile.save()
        return User.objects.get(pk=u.pk)

    def apply_last(self, ctx, user=None):
        p = ctx.task.proposals.order_by('-pk').first()
        out = decide.apply_selected(ctx.task, [p.pk], user or self.user)
        p.refresh_from_db()
        return p, out


class RightsTests(RFQBase):
    def names(self, user, personal=False):
        return {t['name'] for t in toolsets.tools_for(user, personal=personal)}

    def test_tools_by_role_and_personal_key(self):
        everything = self.names(self.user, personal=True)
        for n in ('find_supplier_requests', 'get_supplier_request', 'read_rfq_file', 'propose_supplier_request',
                  'propose_rfq_recipients', 'propose_rfq_quote', 'propose_rfq_state', 'propose_rfq_decision',
                  'propose_rfq_status', 'propose_rfq_send', 'propose_rfq_purchase', 'rfq_conversation',
                  'find_rfq_missed_replies', 'propose_rfq_reply', 'propose_rfq_link_reply'):
            self.assertIn(n, everything)
        shared = self.names(self.user)
        self.assertIn('get_supplier_request', shared)
        self.assertNotIn('rfq_conversation', shared)               # листування — лише 🔒 особистий ключ
        self.assertNotIn('propose_rfq_reply', shared)
        viewer = self.person('viewer', 'suppliers', 'inventory')
        names = self.names(viewer, personal=True)
        self.assertIn('get_supplier_request', names)
        self.assertIn('rfq_conversation', names)
        self.assertFalse({n for n in names if n.startswith('propose_rfq') or n == 'propose_supplier_request'})
        self.assertNotIn('get_supplier_request', self.names(self.person('nobody', 'sales')))
        out, err = tools.run(self.ctx(owner=viewer), 'propose_rfq_state', {'request': self.req.code,
                                                                            'supplier': self.jlc.name,
                                                                            'state': 'declined'})
        self.assertTrue(err)                                       # інструмент перевіряє право ще раз

    def test_mcp_lists_rfq_tools_and_write_annotations(self):
        key = APIKey.objects.create(name='Claude', scopes=['agent:read', 'agent:write'], agent_user=self.user,
                                    owner=self.user, private=True)
        defs = {t['name']: t for t in mcp.tool_defs(self.user, key)}
        self.assertTrue(defs['get_supplier_request']['annotations']['readOnlyHint'])
        self.assertFalse(defs['propose_rfq_quote']['annotations']['readOnlyHint'])
        self.assertIn('rfq_conversation', defs)
        shared = APIKey.objects.create(name='Team', scopes=['agent:read'], agent_user=self.user, owner=self.other)
        self.assertNotIn('rfq_conversation', mcp.names(self.user, shared))
        self.assertIn('find_supplier_requests', mcp.names(self.user, shared))
        self.assertIs(toolsets.module_for_kind(AgentProposal.RFQ), tools_rfq)


class ReadTests(RFQBase):
    def test_find_filters_and_counts(self):
        SupplierRequest.objects.create(title='Old cable', template='cable', status=SupplierRequest.CLOSED)
        out = self.call('find_supplier_requests')
        self.assertIn(self.req.code, out)
        self.assertNotIn('Old cable', out)                          # за замовчуванням — відкриті
        self.assertIn('відповіли 1 з 2', out)
        self.assertIn('прострочено', out)
        out = self.call('find_supplier_requests', supplier=f'#{self.jlc.pk}')
        self.assertIn('відповіли 1 з 2', out)                       # фільтр за постачальником не ріже підрахунок
        self.assertIn('Old cable', self.call('find_supplier_requests', status='all'))
        self.assertIn(self.req.code, self.call('find_supplier_requests', overdue=True))
        self.assertIn('немає', self.call('find_supplier_requests', supplier=self.idle.name))

    def test_get_shows_work_page(self):
        f = SupplierRequestFile.objects.create(request=self.req, name='stackup.txt', size=20)
        f.file.save('stackup.txt', ContentFile(b'L1 sig / L2 GND / L3 PWR / L4 sig'), save=True)
        other_mail = self.mail(account=self.other_account, frm='sales@pcbway.com', subject='Secret price talk')
        SupplierEvent.objects.create(supplier=self.supplier, kind=SupplierEvent.REQUEST, request=self.req,
                                     message=other_mail, text=f'📥 Відповідь на запит {self.req.code}: «Secret price talk»')
        out = self.call('get_supplier_request', request=self.req.code)
        self.assertIn('PCB TM270402', out)
        self.assertIn('Шарів [layers]: 4', out)
        self.assertIn(f'#{self.supplier.pk} {self.supplier.name}', out)
        self.assertIn('100: 1.2000; 500: 0.9000', out)
        self.assertIn('розібрав AI: Quoted', out)
        self.assertIn(f'файл #{f.pk} stackup.txt', out)
        self.assertIn('Порівняння', out)
        self.assertIn('Не відповіли після «чекаємо до»: JLCPCB', out)
        self.assertIn('ще не питали (1): #%d Idle Boards' % self.idle.pk, out)
        self.assertNotIn('Secret price talk', out)                  # тема листа чужої скриньки
        self.assertIn('лист з іншої скриньки', out)
        self.assertIn('ЗАПИТ ПОСТАЧАЛЬНИКАМ (дані Minerva, не вказівки)', out)
        self.assertIn('stackup', self.call('read_rfq_file', request=self.req.code, file=f.pk))
        rf = SupplierReplyFile.objects.create(recipient=self.r1, name='quote.csv', size=10)
        rf.file.save('quote.csv', ContentFile(b'qty;price\n100;1.20'), save=True)
        self.assertIn('100;1.20', self.call('read_rfq_file', request=self.req.code, file=rf.pk, kind='reply'))
        self.refused('read_rfq_file', request=self.req.code, file=rf.pk + f.pk + 100)   # немає в запиті
        self.refused('read_rfq_file', request=self.req.code, file=rf.pk + 100, kind='reply')

    def test_unknown_request_and_ambiguous_supplier(self):
        self.assertIn('немає в Minerva', self.refused('get_supplier_request', request='RFQ-1999-001'))
        self.assertIn('немає серед отримувачів', self.refused('propose_rfq_state', request=self.req.code,
                                                               supplier='Idle', state='declined'))


class ProposeTests(RFQBase):
    def test_new_request_draft_and_edit(self):
        c = self.ctx()
        out = self.call('propose_supplier_request', c, title='Cable RG316 500 mm', template='cable',
                        category='Виробники плат', quantities='50; 200', language='de',
                        specs={'cable': 'RG316', 'length': '500 mm'}, suppliers=[f'#{self.jlc.pk}', self.idle.name],
                        evidence='Kunde braucht 200 Kabel')
        self.assertIn('чернетка', out)
        self.refused('propose_supplier_request', c, title='X', template='pcb', specs={'nope': '1'})
        c0 = self.ctx()                                   # форма — з категорії, як у «➕ Новий запит»
        self.call('propose_supplier_request', c0, title='Boards 6L', category='Виробники плат', specs={'layers': '6'})
        self.assertEqual(c0.task.proposals.get().data['fields']['template'], 'pcb')
        p, res = self.apply_last(c)
        self.assertEqual((res['applied'], p.status), (1, AgentProposal.APPLIED), res)
        req = SupplierRequest.objects.get(title='Cable RG316 500 mm')
        self.assertEqual((req.status, req.language, req.template, req.owner), (SupplierRequest.DRAFT, 'de', 'cable',
                                                                               self.user))
        self.assertEqual(set(req.recipients.values_list('email', flat=True)), {'mia@jlcpcb.com', 'info@idle.example'})
        self.assertIn('RG316', req.body)
        self.assertTrue(req.subject.startswith(f'[{req.code}]'))
        self.assertIn(req.code, p.new_value)
        self.assertEqual(p.result_url, f'/admin/suppliers/supplierrequest/{req.pk}/work/')
        # зміна полів: лише змінене; змінили після пропозиції — відмова
        c2 = self.ctx()
        self.call('propose_supplier_request', c2, request=req.code, group='Kabel 2026',
                  reply_by=str(timezone.localdate() + datetime.timedelta(days=20)))
        prop = c2.task.proposals.get()
        self.assertEqual(set(prop.data['fields']), {'reply_by', 'group'})
        SupplierRequest.objects.filter(pk=req.pk).update(group='Інше')
        p, res = self.apply_last(c2)
        self.assertEqual(p.status, AgentProposal.FAILED)
        self.assertIn('змінили group', p.error)
        c3 = self.ctx()
        self.call('propose_supplier_request', c3, request=req.code, reply_by=str(timezone.localdate()
                                                                               + datetime.timedelta(days=10)))
        p, res = self.apply_last(c3)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        self.assertEqual(SupplierRequest.objects.get(pk=req.pk).reply_by,
                         timezone.localdate() + datetime.timedelta(days=10))

    def test_quote_from_letter_and_stale_quote(self):
        c = self.ctx()
        self.call('propose_rfq_quote', c, request=self.req.code, supplier='mia@jlcpcb.com',
                  tiers=[{'qty': 100, 'unit_price': 1.05}, {'qty': 500, 'unit_price': '0,80', 'lead_days': 12}],
                  one_time='Test jig 150', includes=['pcb'], currency='usd', lead_days=12,
                  valid_until='2030-02-01', quote_note='EXW Shenzhen, 30 % prepayment',
                  evidence='100 pcs 1.05 USD, 500 pcs 0.80 USD')
        self.refused('propose_rfq_quote', c, request=self.req.code, supplier=self.jlc.name, price=1)   # без цитати
        p = c.task.proposals.get()
        self.assertEqual(decide.describe(p)[0], '💶 Пропозиція постачальника')
        p, res = self.apply_last(c)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        self.r2.refresh_from_db()
        self.assertEqual((self.r2.state, self.r2.currency, self.r2.lead_days), (R.QUOTED, 'USD', 12))
        self.assertEqual([t['qty'] for t in self.r2.tiers], [100, 500])
        self.assertEqual(self.r2.one_time[0]['name'], 'Test jig')
        self.assertEqual(self.r2.includes, ['pcb'])
        self.assertEqual(self.r2.quote_note, 'EXW Shenzhen, 30 % prepayment')
        self.assertEqual(SupplierRequest.objects.get(pk=self.req.pk).status, SupplierRequest.ANSWERED)
        # лише строк — ціни PCBWay лишаються; змінили вручну до ✓ — відмова
        c2 = self.ctx()
        self.call('propose_rfq_quote', c2, request=self.req.code, supplier=self.supplier.name, lead_days=7,
                  evidence='lead time 7 days')
        R.objects.filter(pk=self.r1.pk).update(lead_days=9)
        p, _res = self.apply_last(c2)
        self.assertEqual(p.status, AgentProposal.FAILED)
        c3 = self.ctx()
        self.call('propose_rfq_quote', c3, request=self.req.code, supplier=self.supplier.name, lead_days=7,
                  evidence='lead time 7 days')
        p, _res = self.apply_last(c3)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        self.r1.refresh_from_db()
        self.assertEqual((self.r1.lead_days, len(self.r1.tiers), self.r1.state), (7, 2, R.QUOTED))

    def test_same_proposal_updates_instead_of_duplicating(self):
        c = self.ctx()
        self.call('propose_rfq_state', c, request=self.req.code, supplier=self.jlc.name, state='declined')
        self.call('propose_rfq_state', c, request=self.req.code, supplier=self.jlc.name, state='silent')
        self.assertEqual(c.task.proposals.count(), 1)
        p, _res = self.apply_last(c)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        self.assertEqual(R.objects.get(pk=self.r2.pk).state, R.SILENT)

    def test_decision_status_and_purchase(self):
        c = self.ctx()
        self.call('propose_rfq_decision', c, request=self.req.code, supplier=self.supplier.name, action='award',
                  text='партія 1 · 500 шт')
        self.refused('propose_rfq_decision', c, request=self.req.code, supplier=self.jlc.name, action='reject')
        self.call('propose_rfq_decision', c, request=self.req.code, supplier=self.jlc.name, action='reject',
                  text='не відповіли вчасно')
        self.call('propose_rfq_purchase', c, request=self.req.code, supplier=self.supplier.name)
        out = decide.apply_selected(c.task, list(c.task.proposals.values_list('pk', flat=True)), self.user)
        self.assertEqual(out['applied'], 3, out)
        self.r1.refresh_from_db()
        self.r2.refresh_from_db()
        self.assertEqual((self.r1.chosen, self.r1.award), (True, 'партія 1 · 500 шт'))
        self.assertEqual((self.r2.chosen, self.r2.reject_reason), (False, 'не відповіли вчасно'))
        po = PurchaseOrder.objects.get(supplier=self.supplier)
        self.assertEqual(po.status, PurchaseOrder.Status.DRAFT)
        self.assertEqual(po.lines.get().qty_ordered, 100)
        purchase = c.task.proposals.get(data__action='purchase')
        self.assertEqual(purchase.result_url, f'/admin/inventory/purchaseorder/{po.pk}/change/')
        c2 = self.ctx()
        self.call('propose_rfq_status', c2, request=self.req.code, action='close', decision='PCBWay — партія 1')
        p, _res = self.apply_last(c2)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        req = SupplierRequest.objects.get(pk=self.req.pk)
        self.assertEqual((req.status, req.decision), (SupplierRequest.CLOSED, 'PCBWay — партія 1'))
        self.assertEqual(R.objects.get(pk=self.r2.pk).state, R.SILENT)        # хто мовчав — 🔕
        self.assertIn('уже закрито', self.call('propose_rfq_status', request=self.req.code, action='close'))
        c3 = self.ctx()
        self.call('propose_rfq_status', c3, request=self.req.code, action='reopen')
        p, _res = self.apply_last(c3)
        self.assertEqual(SupplierRequest.objects.get(pk=self.req.pk).status, SupplierRequest.ANSWERED)

    def test_recipients_send_and_remind(self):
        c = self.ctx()
        self.call('propose_rfq_recipients', c, request=self.req.code, add=[f'#{self.idle.pk}'])
        p, _res = self.apply_last(c)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        r3 = self.req.recipients.get(supplier=self.idle)
        self.assertEqual(r3.state, R.PLANNED)
        self.assertIn('уже надіслано', self.refused('propose_rfq_recipients', request=self.req.code,
                                                    remove=[self.jlc.name]))
        sent = []
        c2 = self.ctx()
        self.call('propose_rfq_send', c2, request=self.req.code, action='send')
        with mock.patch('email_assistant.smtp_client.SMTPClient.send', ok_send(sent)):
            p, _res = self.apply_last(c2)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        self.assertEqual([s[0] for s in sent], [['info@idle.example']])        # лише ще не надісланому
        self.assertEqual(R.objects.get(pk=r3.pk).state, R.SENT)
        self.assertIn('надіслано: 1', p.new_value)
        self.assertIn('нема кому', self.call('propose_rfq_send', request=self.req.code, action='send'))
        sent.clear()
        c3 = self.ctx()
        self.call('propose_rfq_send', c3, request=self.req.code, action='remind', suppliers=[self.jlc.name])
        self.refused('propose_rfq_send', c3, request=self.req.code, action='remind', suppliers=[self.supplier.name])
        with mock.patch('email_assistant.smtp_client.SMTPClient.send', ok_send(sent)):
            p, _res = self.apply_last(c3)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        self.assertEqual(sent[0][0], ['mia@jlcpcb.com'])
        self.assertTrue(sent[0][1].startswith('Re:'))
        self.assertIsNotNone(R.objects.get(pk=self.r2.pk).reminded_at)

    def test_viewer_cannot_apply(self):
        c = self.ctx()
        self.call('propose_rfq_state', c, request=self.req.code, supplier=self.jlc.name, state='declined')
        viewer = self.person('viewer2', 'suppliers', 'agent')
        p = c.task.proposals.get()
        self.assertIn('немає права', tools_rfq.apply(p, viewer))
        self.assertEqual(R.objects.get(pk=self.r2.pk).state, R.SENT)


class MailTests(RFQBase):
    def setUp(self):
        super().setUp()
        self.sent_msg = self.mail(frm='slava@sevskiy.de', to=['mia@jlcpcb.com'], subject=f'[{self.req.code}] PCB',
                                  folder='sent', name='Slava')
        thread = self.sent_msg.thread
        if thread is None:
            from email_assistant.models import EmailThread
            thread = EmailThread.objects.create(account=self.account, subject=self.sent_msg.subject)
            self.sent_msg.thread = thread
            self.sent_msg.save(update_fields=['thread'])
        self.reply_msg = self.mail(frm='mia@jlcpcb.com', name='Mia', subject=f'Re: [{self.req.code}] PCB',
                                   body='Dear Slava,\n100 pcs 1.10 USD, lead time 9 days', thread=thread,
                                   in_reply_to=self.sent_msg.message_id)
        self.colleague = self.mail(account=self.other_account, frm='mia@jlcpcb.com', subject='Private to Olga',
                                   body='Olga only', thread=thread)
        R.objects.filter(pk=self.r2.pk).update(thread=thread, sent_message=self.sent_msg)

    def test_conversation_only_own_mailboxes(self):
        out = self.call('rfq_conversation', request=self.req.code, supplier=self.jlc.name)
        self.assertIn('100 pcs 1.10 USD', out)
        self.assertNotIn('Olga only', out)
        self.assertNotIn('Private to Olga', out)
        self.assertIn('у скриньці колеги', out)
        self.assertIn(f'лист id {self.reply_msg.pk}', out)
        self.assertIn('Відповідати: кому mia@jlcpcb.com', out)

    def test_reply_proposal_sent_by_owner_only(self):
        c = self.ctx()
        self.refused('propose_rfq_reply', c, request=self.req.code, supplier=self.jlc.name, body='Hi',
                     reply_to_id=self.colleague.pk)                    # чужий лист — ні
        self.call('propose_rfq_reply', c, request=self.req.code, supplier=self.jlc.name,
                  body='Dear Mia,\nthank you — could you confirm ENIG?', reply_to_id=self.reply_msg.pk)
        p = c.task.proposals.get()
        self.assertEqual(p.data['to'], 'mia@jlcpcb.com')
        self.assertTrue(p.data['subject'].startswith('Re:'))
        self.assertIn('немає права', tools_rfq.apply(p, self.person('viewer3', 'suppliers')))
        boss = User.objects.create_superuser('boss', 'boss@x.y', 'p')
        self.assertIn('власник завдання', tools_rfq.apply(p, boss))
        sent = []
        with mock.patch('email_assistant.smtp_client.SMTPClient.send', ok_send(sent)):
            p, _res = self.apply_last(c)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        self.assertEqual(sent[0][0], ['mia@jlcpcb.com'])
        self.assertIn('confirm ENIG', sent[0][2])
        self.assertTrue(SupplierEvent.objects.filter(request=self.req, kind=SupplierEvent.MAIL).exists())

    def test_missed_reply_link_and_skip(self):
        loose = self.mail(frm='bob@idle.example', subject=f'Your inquiry {self.req.code}', body='See attached')
        self.req.recipients.create(supplier=self.idle, email='info@idle.example', state=R.SENT,
                                   sent_at=self.req.sent_at)
        out = self.call('find_rfq_missed_replies', request=self.req.code)
        self.assertIn(f'лист id {loose.pk}', out)
        c = self.ctx()
        self.refused('propose_rfq_link_reply', c, request=self.req.code, message_id=self.colleague.pk,
                     supplier=self.idle.name, action='link')            # не своя скринька
        self.call('propose_rfq_link_reply', c, request=self.req.code, message_id=loose.pk,
                  supplier=self.idle.name, action='link')
        p, _res = self.apply_last(c)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        r3 = self.req.recipients.get(supplier=self.idle)
        self.assertEqual((r3.state, r3.reply_message_id), (R.REPLIED, loose.pk))
        spam = self.mail(frm='sales@pcbway.com', subject=f'Newsletter {self.req.code}', body='promo')
        c2 = self.ctx()
        self.call('propose_rfq_link_reply', c2, request=self.req.code, message_id=spam.pk, action='skip')
        p, _res = self.apply_last(c2)
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        self.assertIn(spam.pk, SupplierRequest.objects.get(pk=self.req.pk).missed_skip)

    def test_mcp_call_with_personal_key(self):
        key = APIKey.objects.create(name='Claude', scopes=['agent:read', 'agent:write'], agent_user=self.user,
                                    owner=self.user, private=True)
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'rfq_conversation', 'arguments': {'request': self.req.code,
                                                                     'supplier': self.jlc.name}}}
        r = self.client.post(URL, json.dumps(body), content_type='application/json',
                             HTTP_AUTHORIZATION=f'Bearer {key.key}')
        self.assertEqual(r.status_code, 200, r.content)
        res = r.json()['result']
        self.assertFalse(res['isError'], res)
        self.assertIn('1.10 USD', res['content'][0]['text'])
