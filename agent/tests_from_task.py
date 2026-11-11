"""🔁 Те саме для інших листів, 🤖 доручення з завдання, ✓ закупівля сама й 🔔 звіт доручення
(agent/from_task.py, agent/admin.py, agent/mail.py auto_apply, agent/notify.py rule_report). Claude — сценарій.

python manage.py test agent.tests_from_task --settings=tabele.settings_test
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from agent import from_task, notify, tasks
from agent import mail as agent_mail
from agent.models import AgentProposal, AgentRule, AgentRun, AgentSchedule, AgentTask
from agent.tests_tasks import Base, FakeClient, resp, text, tool

TN = '1Z2R5D130407163813'


class FromTaskBase(Base):
    def setUp(self):
        super().setUp()
        from email_assistant.models import EmailAccount
        self.account = EmailAccount.objects.create(user=self.user, email_address='slava@sevskiy.de')
        self.uid = 0
        self.client.force_login(self.user)

    def mail(self, frm='oscar@raynool.com', subject='WG: RFQ for MHF1/MHF4 cable assemblies', account=None, **kw):
        from email_assistant.models import EmailMessage
        self.uid += 1
        return EmailMessage.objects.create(account=account or self.account, folder='inbox', imap_folder_name='INBOX',
                                           imap_uid=self.uid, subject=subject, from_email=frm,
                                           body_text=kw.get('body', f'Order have been sent, UPS No.{TN}'),
                                           to_emails=['slava@sevskiy.de'], sent_at=timezone.now())

    def mail_task(self, msg=None, **kw):
        msg = msg or self.mail()
        task = AgentTask.objects.create(owner=self.user, title='внеси заказ в базу данних замовлень',
                                        instruction='внеси заказ в базу данних замовлень', message=msg,
                                        status=AgentTask.DONE, budget_usd=Decimal('2'), **kw)
        return task


class BaseInstructionTests(FromTaskBase):
    def test_notes_of_rule_and_schedule_are_dropped(self):
        rule_note = agent_mail.TASK_NOTE.format(name='PO')
        from agent.schedules import NOTE
        for tail in (rule_note, NOTE.format(name='Ранковий огляд')):
            task = AgentTask(instruction='внеси заказ' + tail)
            self.assertEqual(from_task.base_instruction(task), 'внеси заказ')
        self.assertEqual(from_task.base_instruction(AgentTask(instruction='Замовлення: 1\nвнеси')), 'Замовлення: 1\nвнеси')


class RepeatTests(FromTaskBase):
    def test_candidates_similar_first_own_only_and_done_marked(self):
        task = self.mail_task()
        other = self.mail(frm='anna@kunde.de', subject='Angebot')
        same = self.mail(subject='WG: RFQ for MHF1/MHF4 cable assemblies')
        domain = self.mail(frm='sales@raynool.com', subject='New price list')
        bob = User.objects.create_user('bob', 'b@x.y', 'p', is_staff=True)
        from email_assistant.models import EmailAccount
        foreign = self.mail(account=EmailAccount.objects.create(user=bob, email_address='bob@sevskiy.de'))
        done = AgentTask.objects.create(owner=self.user, title='x', message=domain, status=AgentTask.DONE,
                                        instruction='внеси заказ в базу данних замовлень'
                                                    + agent_mail.TASK_NOTE.format(name='PO'))
        rows = from_task.candidates(task, self.user)
        ids = [r['msg'].pk for r in rows]
        self.assertEqual(ids[:2], [same.pk, domain.pk])                  # той самий відправник + тема, потім домен
        self.assertIn(other.pk, ids)
        self.assertNotIn(foreign.pk, ids)
        self.assertNotIn(task.message_id, ids)
        self.assertEqual(rows[1]['done'], done)
        self.assertIn('той самий відправник', rows[0]['why'])
        self.assertEqual([r['msg'].pk for r in from_task.candidates(task, self.user, q='angebot')], [other.pk])

    def test_repeat_page_creates_and_starts_tasks(self):
        task = self.mail_task(web=True)
        task.products.set([self.ant])
        a, b = self.mail(subject='Second'), self.mail(subject='Third')
        url = reverse('admin:agent_agenttask_repeat', args=[task.pk])
        page = self.client.get(url)
        self.assertContains(page, 'Second')
        self.assertContains(page, '▶ Те саме для вибраних')
        fake = FakeClient([resp('end_turn', text('Готово.')), resp('end_turn', text('Готово.'))])
        with mock.patch('agent.tasks._client', return_value=fake):
            r = self.client.post(url, {'ids': [a.pk, b.pk]})
        self.assertRedirects(r, reverse('admin:agent_agenttask_changelist'), fetch_redirect_response=False)
        new = AgentTask.objects.exclude(pk=task.pk).order_by('pk')
        self.assertEqual([t.message_id for t in new], [a.pk, b.pk])
        t = new[0]
        self.assertEqual((t.instruction, t.model, t.budget_usd, t.web, t.owner),
                         (task.instruction, task.model, task.budget_usd, True, self.user))
        self.assertEqual(list(t.products.all()), [self.ant])
        self.assertTrue(all(x.status == AgentTask.DONE for x in new))     # агент відпрацював (FakeClient)
        self.assertEqual(len(fake.calls), 2)

    def test_foreign_mail_and_empty_choice_refused(self):
        task = self.mail_task()
        bob = User.objects.create_user('bob', 'b@x.y', 'p', is_staff=True)
        from email_assistant.models import EmailAccount
        foreign = self.mail(account=EmailAccount.objects.create(user=bob, email_address='bob@sevskiy.de'))
        url = reverse('admin:agent_agenttask_repeat', args=[task.pk])
        self.client.post(url, {'ids': [foreign.pk]})
        self.client.post(url, {})
        self.assertEqual(AgentTask.objects.count(), 1)

    def test_task_without_mail_runs_again(self):
        task = AgentTask.objects.create(owner=self.user, title='знайди вагу', instruction='знайди вагу',
                                        status=AgentTask.DONE)
        with mock.patch('agent.tasks._client', return_value=FakeClient([])):
            r = self.client.post(reverse('admin:agent_agenttask_repeat', args=[task.pk]))
        new = AgentTask.objects.exclude(pk=task.pk).get()
        self.assertRedirects(r, reverse('admin:agent_agenttask_review', args=[new.pk]), fetch_redirect_response=False)
        self.assertIsNone(new.message)

    def test_list_action_and_review_buttons(self):
        task = self.mail_task()
        changelist = reverse('admin:agent_agenttask_changelist')
        r = self.client.post(changelist, {'action': 'action_repeat', '_selected_action': [task.pk]})
        self.assertRedirects(r, reverse('admin:agent_agenttask_repeat', args=[task.pk]), fetch_redirect_response=False)
        page = self.client.get(reverse('admin:agent_agenttask_review', args=[task.pk]))
        self.assertContains(page, '🔁 Те саме для інших листів…')
        self.assertContains(page, '🤖 Робити так з усіма подібними листами…')
        mcp = AgentTask.objects.create(owner=self.user, title='m', instruction='m', mcp_session='day:2026-11-11')
        page = self.client.get(reverse('admin:agent_agenttask_review', args=[mcp.pk]))
        self.assertNotContains(page, '🔁 Те саме')


class MakeRuleTests(FromTaskBase):
    def test_action_opens_prefilled_rule_and_saves_example(self):
        task = self.mail_task()
        r = self.client.post(reverse('admin:agent_agenttask_changelist'),
                             {'action': 'action_make_rule', '_selected_action': [task.pk]})
        add = reverse('admin:agent_agentrule_add') + f'?from_task={task.pk}'
        self.assertRedirects(r, add, fetch_redirect_response=False)
        form = self.client.get(add).context['adminform'].form
        self.assertEqual((form.initial['kind'], form.initial['account'], form.initial['senders'],
                          form.initial['subject_words'], form.initial['instruction']),
                         (AgentRule.MAIL_TASK, self.account.pk, 'oscar@raynool.com', 'RFQ for',
                          'внеси заказ в базу данних замовлень'))
        self.assertEqual(form.initial['task_budget_usd'], Decimal('0.50'))
        data = {k: v for k, v in form.initial.items() if v is not None}
        data.update(enabled='on', report_email='on', report_when='result', mode='draft', send_delay_minutes=15,
                    daily_limit=20, task_model=task.model, ai_sign='signature', ai_sign_name='Minerva',
                    repeat_days=1, importance='important', remind_days=0, remind_until='reaction')
        r = self.client.post(add, data)
        self.assertEqual(r.status_code, 302, getattr(r, 'context', None) and r.context['adminform'].form.errors)
        rule = AgentRule.objects.get()
        self.assertEqual((rule.kind, rule.example_message, rule.instruction, rule.created_by),
                         (AgentRule.MAIL_TASK, task.message, task.instruction, self.user))

    def test_task_from_rule_or_schedule_points_to_it(self):
        rule = AgentRule.objects.create(account=self.account, name='PO', kind=AgentRule.MAIL_TASK, senders='@raynool.com',
                                        instruction='внеси заказ')
        task = self.mail_task()
        AgentRun.objects.create(rule=rule, message=task.message, task=task, status=AgentRun.TASKED)
        url, label = from_task.target(task, self.user)
        self.assertEqual(url, reverse('admin:agent_agentrule_change', args=[rule.pk]))
        self.assertIn('PO', label)
        sched = AgentSchedule.objects.create(owner=self.user, name='Огляд', instruction='огляд')
        st = AgentTask.objects.create(owner=self.user, title='o', instruction='o', schedule=sched)
        self.assertEqual(from_task.target(st, self.user)[0],
                         reverse('admin:agent_agentschedule_change', args=[sched.pk]))

    def test_task_without_mail_becomes_schedule(self):
        task = AgentTask.objects.create(owner=self.user, title='Огляд складу', instruction='подивись склад',
                                        model='claude-sonnet-5-5', budget_usd=Decimal('0.4'))
        url, label = from_task.target(task, self.user)
        self.assertEqual(url, reverse('admin:agent_agentschedule_add') + f'?from_task={task.pk}')
        form = self.client.get(url).context['adminform'].form
        self.assertEqual((form.initial['name'], form.initial['instruction'], form.initial['model']),
                         ('Огляд складу', 'подивись склад', 'claude-sonnet-5-5'))

    def test_foreign_task_or_mail_not_used(self):
        bob = User.objects.create_user('bob', 'b@x.y', 'p', is_staff=True)
        task = AgentTask.objects.create(owner=bob, title='чуже', instruction='чуже')
        form = self.client.get(reverse('admin:agent_agentrule_add') + f'?from_task={task.pk}').context['adminform'].form
        self.assertNotIn('instruction', {k for k, v in form.initial.items() if v})
        from email_assistant.models import EmailAccount
        mine = self.mail_task(msg=self.mail(account=EmailAccount.objects.create(user=bob, email_address='b@s.de')))
        self.assertEqual(from_task.target(mine, self.user)[0], '')


class AutoPurchaseAndReportTests(FromTaskBase):
    def setUp(self):
        super().setUp()
        self.sent = mock.patch('worktime.reports.deliver', return_value='').start()
        self.tg = mock.patch('dashboard.notifications._send_telegram').start()
        self.addCleanup(mock.patch.stopall)
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
        ns.telegram_bot_token = 'x'
        ns.save()
        from shipping.models import Carrier
        Carrier.objects.create(name='UPS', carrier_type='ups', is_active=True, sender_company='Sevskiy GmbH',
                               sender_city='München', sender_country='DE')

    def rule(self, **kw):
        defaults = dict(account=self.account, name='Закупівлі Raynool', kind=AgentRule.MAIL_TASK,
                        senders='@raynool.com', instruction='Внеси закупівлю з листа постачальника.')
        defaults.update(kw)
        return AgentRule.objects.create(**defaults)

    def run_rule(self, rule, script):
        msg = self.mail()
        agent_mail.run_due()
        task = AgentRun.objects.get(rule=rule, message=msg).task
        with mock.patch('agent.tasks._client', return_value=FakeClient(script)):
            tasks.start(task)
        task.refresh_from_db()
        return task

    PO = [resp('tool_use', tool('t1', 'propose_purchase_order', supplier={'name': 'Raynool Technology Co.,Ltd',
                                                                           'email': 'oscar@raynool.com'},
                                lines=[{'sku': 'AN220213-01C', 'qty': 300}], tracking_number=TN, carrier='UPS',
                                evidence=f'Order have been sent, UPS No.{TN}', source='лист Oscar')),
          resp('end_turn', text('Закупівлю запропоновано.'))]

    def test_purchase_applied_by_itself_and_reported_to_chosen(self):
        rule = self.rule(auto_purchase=True, report_to='sergey@sevskiy.de, anna@sevskiy.de', report_chats='-100123')
        task = self.run_rule(rule, self.PO)
        self.assertTrue(task.proposals.exists(), [e['text'] for e in task.log])
        p = task.proposals.get(kind=AgentProposal.PURCHASE)
        self.assertEqual(p.status, AgentProposal.APPLIED, task.summary)
        from inventory.models import PurchaseOrder
        self.assertTrue(PurchaseOrder.objects.filter(supplier__name='Raynool Technology Co.,Ltd').exists())
        self.assertIn('Внесено самому', task.summary)
        to = self.sent.call_args[0][1]
        self.assertEqual(to, ['sergey@sevskiy.de', 'anna@sevskiy.de'])
        self.assertIn('Закупівлі Raynool', self.sent.call_args[0][2])
        self.assertEqual(self.tg.call_args.kwargs['chat_id_override'], '-100123')
        self.assertTrue(any('🔔 Повідомлено' in e['text'] for e in task.log))

    def test_purchase_waits_without_flag_and_owner_gets_report(self):
        self.user.email = 'slava@sevskiy.de'
        self.user.save()
        rule = self.rule(report_telegram=False)
        task = self.run_rule(rule, self.PO)
        self.assertEqual(task.proposals.get().status, AgentProposal.PENDING)
        self.assertEqual(self.sent.call_args[0][1], ['slava@sevskiy.de'])
        self.tg.assert_not_called()

    def test_report_when_result_skips_empty_and_always_sends(self):
        rule = self.rule()
        self.run_rule(rule, [resp('end_turn', text('Це не про закупівлю.'))])
        self.sent.assert_not_called()
        rule.report_when = AgentRule.REPORT_ALWAYS
        rule.save()
        self.run_rule(rule, [resp('end_turn', text('Це не про закупівлю.'))])
        self.assertEqual(self.sent.call_count, 1)

    def test_report_off(self):
        rule = self.rule(report_email=False, report_telegram=False)
        self.run_rule(rule, self.PO)
        self.sent.assert_not_called()
        self.tg.assert_not_called()

    def test_form_checks_recipients(self):
        form_cls = __import__('agent.admin', fromlist=['AgentRuleForm']).AgentRuleForm
        form = form_cls(data={'name': 'x', 'account': self.account.pk, 'kind': AgentRule.MAIL_TASK, 'senders': '@a.de',
                              'instruction': 'x', 'report_to': 'not-an-address', 'report_chats': 'abc'})
        form.is_valid()
        self.assertIn('report_to', form.errors)
        self.assertIn('report_chats', form.errors)


class ProcessNowTests(FromTaskBase):
    """«▶ Обробити зараз» у «🧪 Перевірити, як працює» (agent/mail.py process_now)."""

    def rule(self, **kw):
        defaults = dict(account=self.account, name='Закупівлі', kind=AgentRule.MAIL_TASK, senders='@raynool.com',
                        instruction='Внеси закупівлю з листа постачальника.')
        defaults.update(kw)
        return AgentRule.objects.create(**defaults)

    def test_old_mail_is_skipped_by_cron_but_processed_by_button(self):
        old = self.mail()
        rule = self.rule()
        rule.seen_pk = agent_mail.start_pk(self.account)
        rule.save()
        agent_mail.run_due()
        self.assertFalse(AgentRun.objects.exists())                       # лист старший за доручення — сам не бере
        page = self.client.get(reverse('admin:agent_agentrule_change', args=[rule.pk]))
        url = reverse('admin:agent_agentrule_process', args=[rule.pk, old.pk])
        self.assertContains(page, url)
        self.assertContains(page, '▶ Обробити зараз')
        with mock.patch('agent.tasks._client', return_value=FakeClient([resp('end_turn', text('Готово.'))])):
            r = self.client.post(url)
        self.assertRedirects(r, reverse('admin:agent_agentrule_change', args=[rule.pk]) + '#fieldset-0-1-heading',
                             fetch_redirect_response=False)
        run = AgentRun.objects.get(rule=rule, message=old)
        self.assertEqual(run.status, AgentRun.TASKED)
        self.assertIsNotNone(run.retried_at)
        self.assertEqual(run.task.status, AgentTask.DONE)                 # агент відпрацював одразу
        self.assertIn('Внеси закупівлю', run.task.instruction)
        why, _run = agent_mail.process_now(rule, old)                     # удруге — ні
        self.assertIn('уже в роботі', why)
        self.assertNotContains(self.client.get(reverse('admin:agent_agentrule_change', args=[rule.pk])), url)

    def test_old_mail_older_than_three_days_too(self):
        old = self.mail()
        from email_assistant.models import EmailMessage
        EmailMessage.objects.filter(pk=old.pk).update(sent_at=timezone.now() - timezone.timedelta(days=20))
        old.refresh_from_db()
        rule = self.rule()
        with mock.patch('agent.tasks._client', return_value=FakeClient([])):
            why, run = agent_mail.process_now(rule, old)
        self.assertEqual(why, '')
        self.assertEqual(run.status, AgentRun.TASKED)

    def test_refusals(self):
        rule = self.rule()
        other = self.mail(frm='anna@kunde.de')
        why, run = agent_mail.process_now(rule, other)
        self.assertIn('не підходить', why)
        self.assertIsNone(run)
        rule.enabled = False
        rule.save()
        self.assertIn('вимкнене', agent_mail.process_now(rule, self.mail())[0])
        self.assertFalse(AgentRun.objects.exists())

    def test_skipped_run_goes_again(self):
        rule = self.rule()
        msg = self.mail()
        AgentRun.objects.create(rule=rule, message=msg, status=AgentRun.SKIPPED, reason='ліміт на день')
        with mock.patch('agent.tasks._client', return_value=FakeClient([])):
            why, run = agent_mail.process_now(rule, msg)
        self.assertEqual(why, '')
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.TASKED)

    def test_reply_rule_waits_for_cron_and_foreign_rule_refused(self):
        rule = self.rule(kind=AgentRule.MAIL_REPLY, instruction='Дякуємо, відповімо.')
        msg = self.mail()
        why, run = agent_mail.process_now(rule, msg)
        self.assertEqual((why, run.status), ('', AgentRun.QUEUED))       # відповідь — cron протягом хвилини
        bob = User.objects.create_user('bob', 'b@x.y', 'p', is_staff=True, is_superuser=True)
        self.client.force_login(bob)
        r = self.client.post(reverse('admin:agent_agentrule_process', args=[rule.pk, msg.pk]))
        self.assertEqual(r.status_code, 404)                             # чуже доручення
