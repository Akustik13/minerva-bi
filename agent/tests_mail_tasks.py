"""🧩 Доручення «Завдання агенту з кожного такого листа» (agent/mail.py process_task / auto_apply): лист колеги з
трек-номером → завдання агенту → «🧾 Трек-номер» у замовлення; з «✓ Трек-номер вносити самому» — без ✓, лише в
порожнє поле. Claude підмінено сценарієм.

python manage.py test agent.tests_mail_tasks --settings=tabele.settings_test
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone

from agent import mail as agent_mail
from agent.models import AgentProposal, AgentRule, AgentRun, AgentTask
from agent.tests_tasks import Base, resp, text, tool
from sales.models import SalesOrder, SalesOrderLine

TN = '1Z2R5D130407163813'


class MailTaskTests(Base):
    def setUp(self):
        super().setUp()
        from email_assistant.models import EmailAccount
        self.account = EmailAccount.objects.create(user=self.user, email_address='slava@sevskiy.de')
        self.order = SalesOrder.objects.create(source='digikey', order_number='102519268', client='DREW ADKINS',
                                               addr_country='US', status='processing', order_date=timezone.localdate())
        SalesOrderLine.objects.create(order=self.order, product=self.ant, sku_raw=self.ant.sku, qty=Decimal(2))
        self.uid = 0

    def mail(self, frm='sergey@sevskiy.de', subject='UPS 102519268', body=f'Bestellung 102519268\nUPS {TN}'):
        from email_assistant.models import EmailMessage
        self.uid += 1
        return EmailMessage.objects.create(account=self.account, folder='inbox', imap_folder_name='INBOX',
                                           imap_uid=self.uid, subject=subject, from_email=frm, body_text=body,
                                           to_emails=['slava@sevskiy.de'], sent_at=timezone.now())

    def rule(self, **kw):
        defaults = dict(account=self.account, name='Трек-номер DigiKey', kind=AgentRule.MAIL_TASK,
                        senders='sergey@sevskiy.de', subject_words='UPS',
                        instruction='Знайди номер замовлення й трек-номер і внеси трек-номер у замовлення.')
        defaults.update(kw)
        return AgentRule.objects.create(**defaults)

    def test_colleague_mail_becomes_task(self):
        rule = self.rule()
        msg = self.mail()
        self.assertTrue(agent_mail.blocked(AgentRule(account=self.account, kind=AgentRule.MAIL_REPLY), msg))  # відповідь — ні
        out = agent_mail.run_due()
        self.assertEqual((out['queued'], out['done']), (1, 1))
        run = AgentRun.objects.get(rule=rule, message=msg)
        self.assertEqual(run.status, AgentRun.TASKED)
        task = run.task
        self.assertEqual((task.owner, task.message, task.status, task.model, task.budget_usd),
                         (self.user, msg, AgentTask.QUEUED, 'claude-haiku-5-5', Decimal('0.50')))
        self.assertIn('внеси трек-номер у замовлення', task.instruction)
        self.assertIn('Доручення «Трек-номер DigiKey»', task.instruction)
        self.assertEqual(agent_mail.run_due()['queued'], 0)              # той самий лист — один раз

    def test_daily_limit_and_rights(self):
        self.rule(daily_limit=1)
        self.mail()
        self.mail(subject='UPS 2')
        agent_mail.run_due()
        statuses = sorted(AgentRun.objects.values_list('status', flat=True))
        self.assertEqual(statuses, [AgentRun.SKIPPED, AgentRun.TASKED])
        clerk = User.objects.create_user('clerk', 'c@x.y', 'p', is_staff=True)
        clerk.profile.role = 'readonly'                                 # агента в ролі немає
        clerk.profile.save()
        from email_assistant.models import EmailAccount
        acc = EmailAccount.objects.create(user=clerk, email_address='clerk@sevskiy.de')
        rule = self.rule(account=acc)
        from email_assistant.models import EmailMessage
        msg = EmailMessage.objects.create(account=acc, folder='inbox', imap_folder_name='INBOX', imap_uid=99,
                                          subject='UPS', from_email='sergey@sevskiy.de', sent_at=timezone.now())
        agent_mail.run_due()
        run = AgentRun.objects.get(rule=rule, message=msg)
        self.assertEqual(run.status, AgentRun.FAILED)
        self.assertIn('не може давати завдання', run.reason)

    def run_task(self, rule, script):
        self.mail()
        agent_mail.run_due()
        task = AgentRun.objects.filter(rule=rule).latest('pk').task
        self.go(task, script)
        return task

    def script(self):
        return [
            resp('tool_use', tool('t1', 'read_mail')),
            resp('tool_use', tool('t2', 'propose_order_update', order='102519268', field='tracking', value=TN,
                                  evidence=f'UPS {TN}', source='UPS 102519268'),
                 tool('t3', 'propose_order_update', order='102519268', field='status', value='shipped',
                      evidence='UPS', source='лист')),
            resp('end_turn', text('Трек-номер знайдено.')),
        ]

    def test_without_auto_apply_waits_for_review(self):
        task = self.run_task(self.rule(), self.script())
        self.assertEqual(task.status, AgentTask.REVIEW)
        self.assertEqual(task.proposals.filter(status=AgentProposal.PENDING).count(), 2)
        self.order.refresh_from_db()
        self.assertEqual(self.order.tracking_number, '')

    def test_auto_apply_only_tracking_into_empty_field(self):
        task = self.run_task(self.rule(auto_apply=True), self.script())
        self.order.refresh_from_db()
        self.assertEqual((self.order.tracking_number, self.order.status), (TN, 'processing'))   # статус — ні
        self.assertEqual(task.status, AgentTask.REVIEW)
        left = task.proposals.get(status=AgentProposal.PENDING)
        self.assertEqual(left.data['field'], 'status')
        self.assertIn('Внесено самому', task.summary)
        # трек-номер уже є — інший сам не перезапише
        SalesOrder.objects.filter(pk=self.order.pk).update(tracking_number='1ZOLD')
        task2 = self.run_task(AgentRule.objects.get(), [
            resp('tool_use', tool('t1', 'propose_order_update', order='102519268', field='tracking', value=TN,
                                  evidence=f'UPS {TN}', source='л')),
            resp('end_turn', text('ok'))])
        self.order.refresh_from_db()
        self.assertEqual(self.order.tracking_number, '1ZOLD')
        self.assertEqual(task2.proposals.get().status, AgentProposal.PENDING)

    def test_dialog_saves_task_rule(self):
        msg = self.mail()
        self.client.force_login(self.user)
        d = self.client.get(f'/agent/mail/{msg.pk}/').json()
        self.assertIn('mail_task', [k['value'] for k in d['kinds']])
        self.assertEqual(d['blocked_task'], '')
        self.assertTrue(d['blocked'])                                    # відповідати колезі — ні
        body = {'kind': 'mail_task', 'name': 'Завдання: UPS', 'senders': 'sergey@sevskiy.de', 'subject_words': 'UPS',
                'instruction': 'Внеси трек-номер у замовлення.', 'auto_apply': True, 'task_now': True}
        r = self.client.post(f'/agent/mail/{msg.pk}/try/', json.dumps(body), content_type='application/json').json()
        self.assertFalse(r['ok'])
        r = self.client.post(f'/agent/mail/{msg.pk}/save/', json.dumps(body), content_type='application/json').json()
        self.assertTrue(r['ok'], r)
        rule = AgentRule.objects.get(pk=r['id'])
        self.assertEqual((rule.kind, rule.auto_apply), (AgentRule.MAIL_TASK, True))
        self.assertEqual(r['task_url'], f'/admin/agent/agenttask/{r["task"]}/review/')
        self.assertEqual(AgentRun.objects.get(rule=rule).task_id, r['task'])

    def test_admin_form_warns_sentence_in_body_words(self):
        from agent.admin import AgentRuleForm
        form = AgentRuleForm(data={
            'name': 'x', 'enabled': True, 'account': self.account.pk, 'kind': AgentRule.MAIL_TASK,
            'body_words': 'номер замовлення і якщо це замовлення є в системі зазвичай діджікі трекінг від упс',
            'instruction': 'x', 'mode': 'draft', 'send_delay_minutes': 15, 'daily_limit': 20,
            'task_model': 'claude-sonnet-5-5', 'task_budget_usd': '0.50'})
        self.assertFalse(form.is_valid())
        self.assertIn('body_words', form.errors)


class DefaultModelTests(Base):
    """Claude Haiku 5.5 — за замовчуванням; пошук в інтернеті для Haiku — без динамічної фільтрації."""

    def test_haiku_default_and_web_search_version(self):
        from agent import tools
        task = self.task('x', products=[], web=True)
        self.assertEqual((task.model, AgentRule().task_model), ('claude-haiku-5-5', 'claude-haiku-5-5'))
        web = [t for t in tools.definitions(task) if t.get('name') == 'web_search']
        self.assertEqual(web[0]['type'], 'web_search_20250305')
        task.model = 'claude-opus-5-5'
        web = [t for t in tools.definitions(task) if t.get('name') == 'web_search']
        self.assertEqual(web[0]['type'], 'web_search_20260209')
