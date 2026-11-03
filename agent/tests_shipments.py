"""🤖 Агент і відправлення (agent/shipments.py): читає замовлення, пропонує чернетку відправлення й зміни замовлення —
застосовує людина (✓), оформлення в перевізника — лише людина. Claude підмінено сценарієм.

python manage.py test agent.tests_shipments --settings=tabele.settings_test
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone

from agent import decide, mcp, shipments, tools
from agent.models import AgentProposal, AgentTask
from agent.tests_tasks import Base, resp, text, tool
from core.models import AuditLog
from sales.models import SalesOrder, SalesOrderLine
from shipping.models import Carrier, Shipment


class ShipmentTests(Base):
    def setUp(self):
        super().setUp()
        self.carrier = Carrier.objects.create(name='UPS', carrier_type='ups', is_active=True, is_default=True,
                                              sender_company='Sevskiy GmbH', sender_street='Hauptstr. 1',
                                              sender_city='Berlin', sender_zip='10115', sender_country='DE',
                                              sender_phone='+49 30 1', sender_email='ship@sevskiy.de')
        self.order = self.make_order('102497269', country='US', deadline=-1)

    def make_order(self, number, country='AT', deadline=2, qty=5, source='digikey'):
        o = SalesOrder.objects.create(
            source=source, order_number=number, client='ACME Inc', ship_name='John Doe', ship_company='ACME Inc',
            addr_street='1 Main St', addr_city='Boston', addr_zip='02110', addr_country=country,
            ship_phone='+1 555 0100', email='john@acme.example', currency='EUR',
            order_date=timezone.localdate(), shipping_deadline=timezone.localdate() + timedelta(days=deadline))
        SalesOrderLine.objects.create(order=o, product=self.ant, sku_raw=self.ant.sku, qty=Decimal(qty),
                                      unit_price=Decimal('10'), total_price=Decimal(10 * qty), currency='EUR')
        return o

    def ctx(self, task=None):
        return tools.Context(task or self.task('Підготуй відправлення.', products=[]))

    def call(self, name, ctx=None, **args):
        out, err = tools.run(ctx or self.ctx(), name, args)
        self.assertFalse(err, out)
        return out

    # ── читання ──
    def test_orders_to_ship_and_problems(self):
        self.make_order('5001', deadline=10)
        data = json.loads(self.call('orders_to_ship', overdue_only=True))
        self.assertEqual([o['order'] for o in data['orders']], ['102497269'])
        row = data['orders'][0]
        self.assertTrue(row['overdue'])
        self.assertEqual(row['country'], 'US')
        self.assertTrue(any('бракує AN220213-01C' in p for p in row['problems']))
        self.assertTrue(any('HS-коду' in p for p in row['problems']))
        self.assertEqual(json.loads(self.call('orders_to_ship', due_days=30))['total'], 2)

    def test_get_order(self):
        data = json.loads(self.call('get_order', order='102497269'))
        self.assertEqual(data['ship_to']['company'], 'ACME Inc')
        self.assertEqual(data['ship_to']['country'], 'US')
        self.assertTrue(data['customs_needed'])
        self.assertTrue(data['lines'][0]['short'])
        self.assertEqual(data['days_left'], -1)
        out, err = tools.run(self.ctx(), 'get_order', {'order': 'nope'})
        self.assertTrue(err)

    def test_no_sales_right_no_orders(self):
        clerk = User.objects.create_user('clerk', 'c@x.y', 'p')
        task = AgentTask.objects.create(owner=clerk, title='t', instruction='t')
        self.assertNotIn('get_order', [t['name'] for t in tools.definitions(task)])
        out, err = tools.run(tools.Context(task), 'get_order', {'order': '102497269'})
        self.assertTrue(err)
        self.assertIn('немає права', out)

    def test_tools_only_outside_each_product(self):
        names = [t['name'] for t in tools.definitions(self.task(each_product=True))]
        self.assertNotIn('propose_shipment', names)
        self.assertIn('propose_shipment', [t['name'] for t in tools.definitions(self.task())])
        self.assertIn('orders_to_ship', mcp.names())

    # ── чернетка відправлення ──
    def test_draft_shipment_on_approve(self):
        task = self.task('Підготуй відправлення 102497269 через UPS.', products=[])
        ctx = tools.Context(task)
        self.call('propose_shipment', ctx, order='102497269', carrier='UPS', evidence='«через UPS»')
        p = task.proposals.get()
        self.assertEqual(p.kind, AgentProposal.SHIPMENT)
        self.assertFalse(Shipment.objects.exists())                    # без ✓ — нічого
        out = decide.apply_selected(task, [p.pk], self.user)
        self.assertEqual(out['applied'], 1, out)
        s = Shipment.objects.get()
        self.assertEqual((s.status, s.order, s.carrier), (Shipment.Status.DRAFT, self.order, self.carrier))
        self.assertEqual((s.recipient_company, s.recipient_country, s.reference), ('ACME Inc', 'US', '102497269'))
        self.assertEqual((s.sender_company, s.sender_country), ('Sevskiy GmbH', 'DE'))
        self.assertEqual(s.declared_value, Decimal('50.00'))
        self.assertEqual(s.customs_articles['customs_line_items'][0]['sku'], 'AN220213-01C')
        p.refresh_from_db()
        self.assertEqual(p.result_url, f'/admin/shipping/shipment/{s.pk}/edit-draft/')
        self.assertTrue(AuditLog.objects.filter(object_repr=f'#{s.pk} 102497269').exists())
        # друга пропозиція для того самого замовлення — уже не створює
        out, err = tools.run(ctx, 'propose_shipment', {'order': '102497269', 'evidence': 'x'})
        self.assertTrue(err)
        self.assertIn('вже є відправлення', out)

    def test_draft_refused_when_shipment_appears(self):
        task = self.task('x', products=[])
        self.call('propose_shipment', tools.Context(task), order='102497269', evidence='x')
        Shipment.objects.create(order=self.order, carrier=self.carrier, status=Shipment.Status.SUBMITTED)
        p = task.proposals.get()
        out = decide.apply_selected(task, [p.pk], self.user)
        self.assertEqual(out['applied'], 0)
        p.refresh_from_db()
        self.assertEqual(p.status, AgentProposal.FAILED)
        self.assertIn('вже є відправлення', p.error)
        self.assertEqual(Shipment.objects.count(), 1)

    def test_eu_order_without_customs(self):
        self.make_order('7001', country='AT')
        task = self.task('x', products=[])
        self.call('propose_shipment', tools.Context(task), order='7001', evidence='x')
        decide.apply_selected(task, list(task.proposals.values_list('pk', flat=True)), self.user)
        self.assertFalse(Shipment.objects.get().customs_articles)

    def test_shipment_needs_right(self):
        clerk = User.objects.create_user('clerk2', 'c2@x.y', 'p')
        task = self.task('x', products=[])
        self.call('propose_shipment', tools.Context(task), order='102497269', evidence='x')
        p = task.proposals.get()
        self.assertIn('немає права', shipments.apply_shipment(p, clerk))
        self.assertFalse(Shipment.objects.exists())

    # ── зміни замовлення ──
    def test_tracking_and_status_on_approve(self):
        task = self.task('Внеси трек-номер з листа.', products=[])
        ctx = tools.Context(task)
        self.call('propose_order_update', ctx, order='102497269', field='tracking', value='1Z999AA10123456784',
                  evidence='Tracking: 1Z999AA10123456784', source='лист UPS')
        self.call('propose_order_update', ctx, order='102497269', field='status', value='shipped',
                  evidence='«відправлено сьогодні»')
        self.assertEqual(task.proposals.count(), 2)
        out = decide.apply_selected(task, list(task.proposals.values_list('pk', flat=True)), self.user)
        self.assertEqual(out['applied'], 2, out)
        self.order.refresh_from_db()
        self.assertEqual((self.order.tracking_number, self.order.status), ('1Z999AA10123456784', 'shipped'))
        from django.contrib.admin.models import LogEntry
        self.assertTrue(LogEntry.objects.filter(object_id=str(self.order.pk)).exists())
        rows = {decide.describe(p)[0]: decide.describe(p) for p in task.proposals.all()}
        self.assertEqual(rows['🧾 Статус'][1:], ('Отримано', 'Відправлено'))

    def test_order_changed_after_proposal(self):
        task = self.task('x', products=[])
        self.call('propose_order_update', tools.Context(task), order='102497269', field='tracking',
                  value='1Z111', evidence='x')
        SalesOrder.objects.filter(pk=self.order.pk).update(tracking_number='OTHER1')
        p = task.proposals.get()
        decide.apply_selected(task, [p.pk], self.user)
        p.refresh_from_db()
        self.assertEqual(p.status, AgentProposal.FAILED)
        self.assertIn('змінилось після пропозиції', p.error)
        self.order.refresh_from_db()
        self.assertEqual(self.order.tracking_number, 'OTHER1')

    def test_order_values_checked(self):
        ctx = self.ctx()
        for field, value in (('status', 'lost'), ('deadline', '31.12.2026'), ('tracking', '1Z 999'),
                             ('courier', '')):
            out, err = tools.run(ctx, 'propose_order_update', {'order': '102497269', 'field': field, 'value': value,
                                                               'evidence': 'x'})
            self.assertTrue(err, (field, value))
        out = self.call('propose_order_update', ctx, order='102497269', field='status', value='received',
                        evidence='x')
        self.assertIn('уже так', out)
        self.call('propose_order_update', ctx, order='102497269', field='deadline', value='2026-12-31',
                  evidence='x')
        self.assertEqual(AgentProposal.objects.get().new_value, '2026-12-31')

    def test_ambiguous_number(self):
        self.make_order('102497269', country='AT', source='webshop')
        out, err = tools.run(self.ctx(), 'get_order', {'order': '102497269'})
        self.assertTrue(err)
        self.assertIn('order_source', out)
        data = json.loads(self.call('get_order', order='102497269', order_source='webshop'))
        self.assertEqual(data['source'], 'webshop')

    # ── сторінки ──
    def test_order_list_action_and_review_page(self):
        self.client.force_login(self.user)
        r = self.client.post('/admin/sales/salesorder/', {'action': 'agent_task_for_orders',
                                                           '_selected_action': [self.order.pk]})
        self.assertEqual(r.status_code, 302)
        self.assertIn(f'?orders={self.order.pk}', r['Location'])
        page = self.client.get(r['Location'])
        self.assertContains(page, 'Замовлення: 102497269')
        task = self.task('x', products=[])
        self.call('propose_shipment', tools.Context(task), order='102497269', evidence='x')
        page = self.client.get(f'/admin/agent/agenttask/{task.pk}/review/')
        self.assertContains(page, f'/admin/sales/salesorder/{self.order.pk}/change/')
        self.assertContains(page, 'Чернетка відправлення')

    def test_task_loop(self):
        task = self.task('Підготуй відправлення для 102497269 через UPS.', products=[])
        self.go(task, [
            resp('tool_use', tool('t1', 'get_order', order='102497269')),
            resp('tool_use', tool('t2', 'propose_shipment', order='102497269', carrier='UPS',
                                  evidence='«через UPS»')),
            resp('end_turn', text('Чернетку запропоновано; бракує складу AN220213-01C.')),
        ])
        self.assertEqual(task.status, AgentTask.REVIEW)
        self.assertEqual(task.proposals.get().kind, AgentProposal.SHIPMENT)
