"""🤖 Завдання агенту (agent/tasks.py, tools.py, decide.py): Claude підмінено сценарієм, нічого не надсилається.

python manage.py test agent.tests_tasks --settings=tabele.settings_test
"""
import shutil
import tempfile
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from agent import decide, tasks
from agent.models import AgentProposal, AgentTask
from email_assistant.tests_attachment_links import pdf_bytes
from inventory.models import Product

DATASHEET = pdf_bytes('AN220213 Antenna 2.4 GHz', 'Mechanical data', 'Weight: 45 g', 'Country of origin: Germany')


def tool(id_, name, **inp):
    return NS(type='tool_use', id=id_, name=name, input=inp)


def text(t):
    return NS(type='text', text=t)


def resp(stop, *blocks, inp=1000, out=200):
    return NS(stop_reason=stop, content=list(blocks),
              usage=NS(input_tokens=inp, output_tokens=out, cache_read_input_tokens=0, cache_creation_input_tokens=0,
                       server_tool_use=None))


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.messages = self

    def create(self, **kw):
        self.calls.append({**kw, 'messages': list(kw['messages'])})   # стан розмови на момент запиту
        return self.script.pop(0) if self.script else resp('end_turn', text('Готово.'))


class Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        over = override_settings(MEDIA_ROOT=self.media)
        over.enable()
        self.addCleanup(over.disable)
        old, tasks.BACKGROUND = tasks.BACKGROUND, False
        self.addCleanup(setattr, tasks, 'BACKGROUND', old)
        self.user = User.objects.create_superuser('slava', 's@x.y', 'p')
        self.ant = Product.objects.create(sku='AN220213-01C', name='Antenna 2.4 GHz')
        self.ant.datasheet_file.save('AN220213.pdf', ContentFile(DATASHEET), save=True)
        self.filt = Product.objects.create(sku='FT220407', name='Filter')

    def task(self, instruction='Знайди вагу в даташиті й внеси.', products=None, **kw):
        task = AgentTask.objects.create(owner=self.user, title=instruction[:200], instruction=instruction, **kw)
        task.products.set(products if products is not None else [self.ant])
        return task

    def go(self, task, script):
        fake = FakeClient(script)
        with mock.patch('agent.tasks._client', return_value=fake):
            tasks.start(task)
        task.refresh_from_db()
        return fake


class LoopTests(Base):
    def test_reads_datasheet_and_proposes_weight(self):
        task = self.task()
        fake = self.go(task, [
            resp('tool_use', tool('t1', 'read_datasheet', sku='AN220213-01C')),
            resp('tool_use', tool('t2', 'propose_change', sku='AN220213-01C', field='net_weight_g', value='45',
                                  evidence='Weight: 45 g', source='даташит, стор. 1', confidence='high')),
            resp('end_turn', text('Вагу 45 г знайдено на стор. 1.')),
        ])
        self.assertEqual(task.status, AgentTask.REVIEW)
        p = task.proposals.get()
        self.assertEqual((p.kind, p.field, p.old_value, p.new_value), (AgentProposal.FIELD, 'net_weight_g', '', '45'))
        self.assertEqual(p.evidence, 'Weight: 45 g')
        self.assertIn('Вагу 45 г', task.summary)
        self.assertGreater(task.cost_usd, 0)
        datasheet_result = fake.calls[1]['messages'][-1]['content'][0]
        self.assertEqual(datasheet_result['tool_use_id'], 't1')
        self.assertIn('Weight: 45 g', datasheet_result['content'])
        self.assertIn('ДАНІ', fake.calls[0]['system'])
        self.assertEqual(Product.objects.get(pk=self.ant.pk).net_weight_g, None)   # нічого не змінено без ✓

    def test_bad_value_and_foreign_product_go_back_as_errors(self):
        task = self.task()
        fake = self.go(task, [
            resp('tool_use', tool('t1', 'propose_change', sku='AN220213-01C', field='net_weight_g', value='abc',
                                  evidence='x', source='y'),
                 tool('t2', 'get_product', sku='FT220407'),
                 tool('t3', 'propose_change', sku='AN220213-01C', field='sale_price', value='1', evidence='x',
                      source='y')),
            resp('end_turn', text('Не вийшло.')),
        ])
        results = fake.calls[1]['messages'][-1]['content']
        self.assertTrue(all(r.get('is_error') for r in results))
        self.assertIn('грамах', results[0]['content'])
        self.assertIn('не входить у завдання', results[1]['content'])
        self.assertIn('змінювати не можна', results[2]['content'])
        self.assertFalse(task.proposals.exists())
        self.assertEqual(task.status, AgentTask.DONE)

    def test_email_only_to_addresses_from_instruction(self):
        task = self.task('Знайди даташит цієї антени й надішли на anna@kunde.de')
        fake = self.go(task, [
            resp('tool_use', tool('t1', 'propose_email', to=['evil@example.com'], subject='x', body='y')),
            resp('tool_use', tool('t2', 'propose_email', to=['anna@kunde.de'], subject='Datasheet AN220213',
                                  body='Hallo Anna, anbei das Datenblatt.', attach_datasheets=['AN220213-01C'])),
            resp('end_turn', text('Лист підготовлено.')),
        ])
        self.assertIn('немає в тексті завдання', fake.calls[1]['messages'][-1]['content'][0]['content'])
        mail = task.proposals.get(kind=AgentProposal.MAIL)
        self.assertEqual((mail.mail_to, mail.mail_attach), ('anna@kunde.de', ['AN220213-01C']))

    def test_each_product_mode_and_resume_point(self):
        task = self.task(products=[self.ant, self.filt], each_product=True)
        fake = self.go(task, [resp('end_turn', text('Даташита немає.')), resp('end_turn', text('Даташита немає.'))])
        self.assertEqual(len(fake.calls), 2)
        self.assertNotIn('propose_email', [t['name'] for t in fake.calls[0]['tools']])
        self.assertIn('ТОВАР: AN220213-01C', fake.calls[0]['messages'][0]['content'])
        self.assertEqual(sorted(task.done_products), sorted([self.ant.pk, self.filt.pk]))
        self.assertEqual(task.status, AgentTask.DONE)

    def test_cron_slice_stops_between_products(self):
        task = self.task(products=[self.ant, self.filt], each_product=True)
        tasks._claim(task)
        task.refresh_from_db()
        with mock.patch('agent.tasks._client', return_value=FakeClient([])):
            tasks.run(task, deadline=0)               # час вийшов ще до першого товару
        task.refresh_from_db()
        self.assertEqual((task.status, task.done_products), (AgentTask.QUEUED, []))

    def test_budget_and_cancel_stop(self):
        task = self.task(budget_usd=Decimal('0.01'))
        self.go(task, [resp('tool_use', tool('t1', 'get_product', sku='AN220213-01C'), inp=100000, out=10000)])
        self.assertIn('ліміт витрат', task.summary)
        task2 = self.task()
        AgentTask.objects.filter(pk=task2.pk).update(status=AgentTask.QUEUED)
        with mock.patch('agent.tasks._check', side_effect=tasks.Stop('зупинено людиною')):
            self.go(task2, [])
        self.assertEqual(task2.status, AgentTask.CANCELLED)

    def test_no_ai_key(self):
        task = self.task()
        tasks.start(task)
        task.refresh_from_db()
        self.assertEqual(task.status, AgentTask.FAILED)
        self.assertIn('ключа AI', task.error)

    def test_stale_task_is_requeued(self):
        import datetime

        from django.utils import timezone
        task = self.task()
        AgentTask.objects.filter(pk=task.pk).update(status=AgentTask.RUNNING,
                                                    heartbeat_at=timezone.now() - datetime.timedelta(hours=1))
        with mock.patch('agent.tasks._client', return_value=FakeClient([])):
            out = tasks.run_due()
        task.refresh_from_db()
        self.assertEqual((out['requeued'], out['ran'], task.attempts, task.status), (1, 1, 1, AgentTask.DONE))


class DecideTests(Base):
    def _proposed(self, **kw):
        task = self.task()
        data = dict(task=task, kind=AgentProposal.FIELD, product=self.ant, field='net_weight_g', old_value='',
                    new_value='45', evidence='Weight: 45 g', source='даташит')
        data.update(kw)
        AgentTask.objects.filter(pk=task.pk).update(status=AgentTask.REVIEW)
        task.refresh_from_db()
        return task, AgentProposal.objects.create(**data)

    def test_apply_field_with_audit(self):
        from core.models import AuditLog
        task, p = self._proposed()
        out = decide.apply_selected(task, [p.pk], self.user)
        self.assertEqual(out['applied'], 1)
        self.assertEqual(Product.objects.get(pk=self.ant.pk).net_weight_g, Decimal('45'))
        p.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual((p.status, task.status), (AgentProposal.APPLIED, AgentTask.DONE))
        log = AuditLog.objects.get(object_id=str(self.ant.pk))
        self.assertEqual((log.extra['field'], log.extra['after'], log.extra['via']), ('net_weight_g', '45', 'agent'))

    def test_changed_meanwhile_is_not_overwritten(self):
        task, p = self._proposed()
        Product.objects.filter(pk=self.ant.pk).update(net_weight_g=Decimal('50'))
        out = decide.apply_selected(task, [p.pk], self.user)
        self.assertIn('змінилось', out['failed'][0])
        self.assertEqual(Product.objects.get(pk=self.ant.pk).net_weight_g, Decimal('50'))

    def test_attribute(self):
        task, p = self._proposed(kind=AgentProposal.ATTRIBUTE, field='Operating Temperature', new_value='-40…85 °C')
        decide.apply_selected(task, [p.pk], self.user)
        self.assertEqual(Product.objects.get(pk=self.ant.pk).tech_attributes['Operating Temperature'], '-40…85 °C')

    def test_mail_draft_and_send(self):
        from email_assistant.models import EmailAccount, EmailDraft, ScheduledEmail
        EmailAccount.objects.create(user=self.user, email_address='slava@sevskiy.de', imap_username='x',
                                    imap_password='x', smtp_username='x', smtp_password='x')
        task = self.task('Надішли даташит на anna@kunde.de')
        mk = dict(task=task, kind=AgentProposal.MAIL, mail_to='anna@kunde.de', mail_subject='Datenblatt',
                  mail_body='Hallo Anna', mail_attach=['AN220213-01C'])
        p1, p2, p3 = (AgentProposal.objects.create(**mk) for _ in range(3))
        self.assertEqual(decide.mail(p1, self.user), '')
        draft = EmailDraft.objects.get()
        self.assertEqual((draft.to_emails, draft.attachments[0]['name']), (['anna@kunde.de'], 'AN220213-01C-datasheet.pdf'))
        self.assertEqual(decide.mail(p2, self.user, send=True), '')
        self.assertEqual(ScheduledEmail.objects.get().to_emails, ['anna@kunde.de'])
        self.assertIn('немає в тексті завдання', decide.mail(p3, self.user, to='boss@other.com'))


class PageTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_add_from_product_action_runs_and_review_applies(self):
        url = '/admin/inventory/product/'
        r = self.client.post(url, {'action': 'action_agent_task', '_selected_action': [self.ant.pk]})
        self.assertEqual(r.status_code, 302)
        self.assertIn(f'/admin/agent/agenttask/add/?ids={self.ant.pk}', r['Location'])
        self.assertEqual(self.client.get(r['Location']).status_code, 200)
        script = [resp('tool_use', tool('t1', 'propose_change', sku='AN220213-01C', field='country_of_origin',
                                        value='de', evidence='Country of origin: Germany', source='даташит')),
                  resp('end_turn', text('Країну знайдено.'))]
        with mock.patch('agent.tasks._client', return_value=FakeClient(script)):
            r = self.client.post('/admin/agent/agenttask/add/', {
                'instruction': 'Знайди країну виробника й внеси', 'products': [self.ant.pk], 'model': 'claude-opus-5-5',
                'budget_usd': '2'})
        task = AgentTask.objects.get()
        self.assertRedirects(r, f'/admin/agent/agenttask/{task.pk}/review/', fetch_redirect_response=False)
        page = self.client.get(r['Location'])
        self.assertContains(page, 'Countr')
        self.assertContains(page, 'Застосувати вибрані')
        p = task.proposals.get()
        self.client.post(f'/admin/agent/agenttask/{task.pk}/review/', {'action': 'apply', 'ids': [p.pk]})
        self.assertEqual(Product.objects.get(pk=self.ant.pk).country_of_origin, 'DE')
        self.assertEqual(self.client.get(f'/admin/agent/agenttask/{task.pk}/status/').json()['status'], 'done')

    def test_other_user_cannot_see(self):
        task = self.task()
        other = User.objects.create_superuser('anna', 'a@x.y', 'p')
        self.client.force_login(other)
        self.assertEqual(self.client.get(f'/admin/agent/agenttask/{task.pk}/review/').status_code, 404)
        self.assertNotContains(self.client.get('/admin/agent/agenttask/'), 'Знайди вагу')
