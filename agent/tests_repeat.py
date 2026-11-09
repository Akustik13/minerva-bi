"""🔁 Повторно про те саме замовлення (`repeat_check`), ↻ Обробити ще раз (`retry`) і мітка «🤖 агент» у пошті
(agent/letters.py). Claude і SMTP підмінено.

python manage.py test agent.tests_repeat --settings=tabele.settings_test
"""
import datetime
import importlib
import json
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.utils import timezone

from agent import letters
from agent import mail as agent_mail
from agent.models import AgentLetter, AgentRule, AgentRun
from agent.tests_customer_mail import LETTER, TN, UPS
from agent.tests_tasks import Base
from sales.models import SalesOrder, SalesOrderLine
from shipping.models import Carrier, Shipment

FINAL = f'WG: **Final Reminder** UPS [E-KTY362] Tracking Number: {TN} Exception Notification'
FIRST_TEXT = 'Dear Mr. Obinwa,\nUPS is holding your shipment until the brokerage fee of 46.80 USD is paid…'


class RepeatBase(Base):
    def setUp(self):
        super().setUp()
        from email_assistant.models import EmailAccount
        self.account = EmailAccount.objects.create(user=self.user, email_address='slava@sevskiy.de')
        self.order = SalesOrder.objects.create(source='digikey', order_number='102055312', client='Raytheon',
                                               email='matthew@customer.example', addr_country='US', status='shipped',
                                               order_date=timezone.localdate())
        SalesOrderLine.objects.create(order=self.order, product=self.ant, sku_raw=self.ant.sku, qty=Decimal(3))
        ups = Carrier.objects.create(name='UPS', carrier_type='ups', is_active=True)
        Shipment.objects.create(order=self.order, carrier=ups, status='in_transit', tracking_number=TN)
        self.uid = 0

    def mail(self, subject=f'WG: **Reminder** UPS [E-KTY362] Tracking Number: {TN} Exception Notification',
             days_ago=0, **kw):
        from email_assistant.models import EmailMessage
        self.uid += 1
        defaults = dict(account=self.account, folder='inbox', imap_folder_name='INBOX', imap_uid=self.uid,
                        subject=subject, from_email='sergey@sevskiy.de', from_name='Dr. Sergey Sevskiy',
                        body_text=UPS, to_emails=['slava@sevskiy.de'],
                        sent_at=timezone.now() - datetime.timedelta(days=days_ago))
        defaults.update(kw)
        return EmailMessage.objects.create(**defaults)

    def rule(self, **kw):
        defaults = dict(account=self.account, name='Клієнту: мито UPS', kind=AgentRule.MAIL_CUSTOMER,
                        senders='sergey@sevskiy.de', subject_words='Exception Notification', cc='sergey@sevskiy.de',
                        instruction='Попроси клієнта оплатити мито / брокерський збір в UPS.')
        defaults.update(kw)
        return AgentRule.objects.create(**defaults)

    def ai(self, letter=LETTER):
        return mock.patch('email_assistant.ai_helper._call_ai_direct', return_value=json.dumps(letter))

    def earlier(self, rule, *, days_ago=5, how='scheduled', status='sent'):
        """Попередній лист клієнту за дорученням: запланований (надісланий / скасований) або чернетка."""
        from email_assistant.models import EmailDraft, ScheduledEmail
        when = timezone.now() - datetime.timedelta(days=days_ago)
        msg = self.mail(days_ago=days_ago)
        run = AgentRun.objects.create(rule=rule, message=msg, order=self.order, reply=FIRST_TEXT,
                                      finished_at=when, status=AgentRun.SCHEDULED)
        AgentRun.objects.filter(pk=run.pk).update(created_at=when)
        if how == 'scheduled':
            s = ScheduledEmail.objects.create(account=self.account, subject='Order 102055312 — UPS', body=FIRST_TEXT,
                                              to_emails=['matthew@customer.example'], scheduled_at=when,
                                              sent_at=when, status=status, trigger='agent')
            AgentRun.objects.filter(pk=run.pk).update(scheduled=s)
        else:
            draft = EmailDraft.objects.create(account=self.account, subject='Order 102055312', body=FIRST_TEXT,
                                              to_emails=['matthew@customer.example'])
            AgentRun.objects.filter(pk=run.pk).update(status=AgentRun.DRAFTED, draft=draft,
                                                      outcome={'sent': AgentRun.SENT_SAME, 'waiting': '',
                                                               'deleted': AgentRun.DISCARDED}[how])
            if how != 'waiting':
                AgentRun.objects.filter(pk=run.pk).update(draft=None)
        return AgentRun.objects.get(pk=run.pk)


class RepeatTests(RepeatBase):
    def test_repeat_reminder_knows_previous_letter(self):
        rule = self.rule()                                         # за замовчуванням — не частіше ніж раз на день
        self.earlier(rule, days_ago=5)
        msg = self.mail(subject=FINAL)
        with self.ai() as call:
            agent_mail.run_due()
        run = AgentRun.objects.get(rule=rule, message=msg)
        self.assertEqual(run.status, AgentRun.DRAFTED)
        self.assertIn('🔁 повторне нагадування', run.reason)
        prompt = call.call_args[0][0]
        self.assertIn('РАНІШЕ МИ ВЖЕ ПИСАЛИ', prompt)
        self.assertIn('brokerage fee of 46.80 USD', prompt)          # текст попереднього листа
        self.assertIn('ПОВТОРНЕ нагадування', prompt)
        self.assertIn('з назвою місяця', prompt)

    def test_within_interval_is_skipped(self):
        rule = self.rule(repeat_days=7)
        self.earlier(rule, days_ago=5)
        msg = self.mail(subject=FINAL)
        with self.ai() as call:
            agent_mail.run_due()
        run = AgentRun.objects.get(rule=rule, message=msg)
        self.assertEqual(run.status, AgentRun.SKIPPED)
        self.assertIn('уже писали', run.reason)
        self.assertIn('повторно не раніше', run.reason)
        call.assert_not_called()

    def test_never_and_every_letter(self):
        rule = self.rule(repeat_days=AgentRule.REPEAT_NEVER)
        self.earlier(rule, days_ago=30)
        msg = self.mail(subject=FINAL)
        with self.ai() as call:
            agent_mail.run_due()
        self.assertIn('повторно не пишемо', AgentRun.objects.get(rule=rule, message=msg).reason)
        call.assert_not_called()
        AgentRule.objects.filter(pk=rule.pk).update(repeat_days=0)
        again = self.mail(subject=FINAL + ' 2')
        with self.ai() as call:
            agent_mail.run_due()
        self.assertEqual(AgentRun.objects.get(rule=rule, message=again).status, AgentRun.DRAFTED)

    def test_waiting_draft_blocks_deleted_and_cancelled_do_not_count(self):
        rule = self.rule()
        self.earlier(rule, days_ago=3, how='waiting')
        msg = self.mail(subject=FINAL)
        with self.ai() as call:
            agent_mail.run_due()
        self.assertIn('ще не надіслана', AgentRun.objects.get(rule=rule, message=msg).reason)
        call.assert_not_called()

        rule2 = self.rule(name='друге')
        self.earlier(rule2, days_ago=3, how='deleted')
        self.earlier(rule2, days_ago=4, how='scheduled', status='cancelled')
        sent, waiting = agent_mail.earlier_letters(rule2, self.order)
        self.assertEqual((sent, waiting), ([], None))

    def test_try_on_shows_repeat(self):
        rule = self.rule()
        self.earlier(rule, days_ago=5)
        msg = self.mail(subject=FINAL)
        with self.ai():
            out = agent_mail.try_on(rule, msg)
        self.assertIn('повторне нагадування', out.get('repeat', ''))
        AgentRule.objects.filter(pk=rule.pk).update(repeat_days=14)
        rule.refresh_from_db()
        out = agent_mail.try_on(rule, msg)
        self.assertTrue(out['skip'])
        self.assertIn('повторно не раніше', out['skip_reason'])


class RetryTests(RepeatBase):
    def test_retry_skipped_old_letter(self):
        rule = self.rule(repeat_days=14)
        self.earlier(rule, days_ago=5)
        msg = self.mail(subject=FINAL)
        with self.ai():
            agent_mail.run_due()
        run = AgentRun.objects.get(rule=rule, message=msg)
        self.assertEqual(run.status, AgentRun.SKIPPED)
        AgentRule.objects.filter(pk=rule.pk).update(repeat_days=1)       # змінили налаштування
        type(msg).objects.filter(pk=msg.pk).update(sent_at=timezone.now() - datetime.timedelta(days=5))  # давній
        self.client.force_login(self.user)
        r = self.client.post(f'/admin/agent/agentrule/{rule.pk}/retry/{run.pk}/')
        self.assertEqual(r.status_code, 302)
        run.refresh_from_db()
        self.assertEqual((run.status, bool(run.retried_at)), (AgentRun.QUEUED, True))
        with self.ai():
            agent_mail.run_due()
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.DRAFTED, run.reason)
        self.assertIn('повторне', run.reason)
        self.assertIn('лише пропущений', agent_mail.retry(run))       # чернетку вдруге не створює
        page = self.client.get(f'/admin/agent/agentrule/{rule.pk}/change/')
        self.assertContains(page, '🔁 Повторно про те саме замовлення')

    def test_journal_action(self):
        rule = self.rule()
        msg = self.mail(subject='WG: UPS Exception Notification', body_text='без трек-номера')
        with self.ai():
            agent_mail.run_due()
        run = AgentRun.objects.get(rule=rule, message=msg)
        self.assertEqual(run.status, AgentRun.SKIPPED)
        self.client.force_login(self.user)
        r = self.client.post('/admin/agent/agentrun/', {'action': 'retry_runs', '_selected_action': [run.pk]})
        self.assertEqual(r.status_code, 302)
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRun.QUEUED)


class LetterMarkTests(RepeatBase):
    def sent_copy(self, message_id, body=FIRST_TEXT, subject='Order 102055312 — UPS', **kw):
        from email_assistant.models import EmailMessage
        self.uid += 1
        return EmailMessage.objects.create(account=self.account, folder='sent', imap_folder_name='Sent',
                                           imap_uid=self.uid, message_id=message_id, subject=subject,
                                           from_email='slava@sevskiy.de', body_text=body,
                                           to_emails=['matthew@customer.example'], sent_at=timezone.now(), **kw)

    def test_scheduled_agent_mail_marked_when_sent(self):
        rule = self.rule(mode=AgentRule.SEND)
        self.mail()
        with self.ai():
            agent_mail.run_due()
        run = AgentRun.objects.get(rule=rule)
        from email_assistant.models import ScheduledEmail
        ScheduledEmail.objects.filter(pk=run.scheduled_id).update(scheduled_at=timezone.now())
        with mock.patch('email_assistant.smtp_client.SMTPClient.send',
                        return_value={'ok': True, 'message_id': '<agent-1@sevskiy.de>'}):
            call_command('send_scheduled')
        letter = AgentLetter.objects.get()
        self.assertEqual((letter.message_id, letter.run_id, letter.how), ('<agent-1@sevskiy.de>', run.pk, 'sent'))
        copy = self.sent_copy('<agent-1@sevskiy.de>')
        manual = self.sent_copy('<me@sevskiy.de>', subject='Hallo')
        letters.attach(self.account, [copy, manual])
        self.assertIn('доручення «Клієнту: мито UPS»', copy.agent_mark['title'])
        self.assertIn('агент надіслав сам', copy.agent_mark['title'])
        self.assertIsNone(manual.agent_mark)
        self.client.force_login(self.user)
        page = self.client.get('/email/?folder=sent')
        self.assertContains(page, 'em-tag ag')
        page = self.client.get(f'/email/message/{copy.pk}/preview/')
        self.assertContains(page, 'em-card-ag')
        self.assertContains(page, f'/admin/agent/agentrun/{run.pk}/change/')

    def test_draft_sent_or_scheduled_by_you(self):
        from email_assistant import drafts
        from email_assistant.models import ScheduledEmail
        rule = self.rule()
        self.mail()
        with self.ai():
            agent_mail.run_due()
        run = AgentRun.objects.get(rule=rule)
        drafts.discard(self.account, run.draft_id, how='sent', text=LETTER['reply'] + '\n\nGrüße',
                       message_id='<draft-1@sevskiy.de>')
        letter = AgentLetter.objects.get()
        self.assertEqual((letter.how, letter.run_id), (AgentLetter.DRAFT, run.pk))
        run.refresh_from_db()
        self.assertEqual(run.outcome, AgentRun.SENT_SAME)

        second = self.mail(subject=FINAL, body_text=UPS + ' x')
        AgentRule.objects.filter(pk=rule.pk).update(repeat_days=0)
        with self.ai():
            agent_mail.run_due()
        run2 = AgentRun.objects.get(rule=rule, message=second)
        later = ScheduledEmail.objects.create(account=self.account, subject='x', body='зовсім інший текст',
                                              to_emails=['matthew@customer.example'], scheduled_at=timezone.now())
        drafts.discard(self.account, run2.draft_id, how='scheduled', text='зовсім інший текст', scheduled=later)
        pending = AgentLetter.objects.get(run=run2)
        self.assertEqual((pending.message_id, pending.how), ('', AgentLetter.EDITED))
        letters.scheduled_sent(later, '<later@sevskiy.de>')
        self.assertEqual(AgentLetter.objects.get(run=run2).message_id, '<later@sevskiy.de>')

    def test_draft_list_marks_agent_drafts(self):
        rule = self.rule()
        self.mail()
        with self.ai():
            agent_mail.run_due()
        self.client.force_login(self.user)
        page = self.client.get('/email/?folder=drafts')
        self.assertContains(page, 'em-tag ag')
        self.assertTrue(rule.pk)

    def test_backfill_marks_letters_sent_before(self):
        migration = importlib.import_module('agent.migrations.0018_repeat_letters')
        from django.apps import apps
        rule = self.rule()
        run = self.earlier(rule, days_ago=5, how='sent')
        copy = self.sent_copy('<old@sevskiy.de>', body='Grüße\n\n' + FIRST_TEXT)
        type(copy).objects.filter(pk=copy.pk).update(sent_at=timezone.now() - datetime.timedelta(days=4))
        migration.mark_earlier_letters(apps, None)
        letter = AgentLetter.objects.get(message_id='<old@sevskiy.de>')
        self.assertEqual((letter.run_id, letter.how), (run.pk, AgentLetter.DRAFT))
        migration.mark_earlier_letters(apps, None)                      # повтор — без дубля
        self.assertEqual(AgentLetter.objects.count(), 1)
        # запис зроблено «зараз», але лист пішов 4 дні тому — повтор рахується від листа, не від оновлення
        self.assertLess(agent_mail.sent_when(run), timezone.now() - datetime.timedelta(days=3))
        msg = self.mail(subject=FINAL)
        with self.ai():
            agent_mail.run_due()
        self.assertEqual(AgentRun.objects.get(rule=rule, message=msg).status, AgentRun.DRAFTED)
