"""Стан листа за дорученням — як є зараз (agent/run_state.py): запланований лист пішов → «✓ Надіслано», чернетку
надіслали чи видалили → так і написано; у «🧪 Перевірити», «📜 Останніх листах», журналі й прев'ю листа.

python manage.py test agent.tests_run_state --settings=tabele.settings_test
"""
import datetime
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.utils import timezone

from agent import mail as agent_mail
from agent import run_state
from agent.models import AgentLetter, AgentRun
from agent.tests_repeat import RepeatBase


class RunStateTests(RepeatBase):
    def scheduled_run(self, status='pending', minutes=5):
        from email_assistant.models import ScheduledEmail
        rule = self.rule(mode='send')
        s = ScheduledEmail.objects.create(account=self.account, subject='Order 102055312', body='Hi',
                                          to_emails=['matthew@customer.example'], status=status, trigger='agent',
                                          scheduled_at=timezone.now() + datetime.timedelta(minutes=minutes))
        return AgentRun.objects.create(rule=rule, message=self.mail(), status=AgentRun.SCHEDULED, scheduled=s)

    def drafted_run(self, outcome='', with_draft=True):
        from email_assistant.models import EmailDraft
        draft = EmailDraft.objects.create(account=self.account, subject='Order 102055312', body='Hi',
                                          to_emails=['matthew@customer.example']) if with_draft else None
        return AgentRun.objects.create(rule=self.rule(), message=self.mail(), status=AgentRun.DRAFTED, draft=draft,
                                       outcome=outcome)

    def test_scheduled_letter_states(self):
        run = self.scheduled_run()
        self.assertTrue(run_state.text(run).startswith('⏳ Надішле о '))
        self.assertEqual(run_state.done(run), '')
        with mock.patch('email_assistant.smtp_client.SMTPClient.send', return_value={'ok': True}):
            call_command('send_scheduled', stdout=StringIO())        # ще не час — не йде
            run.scheduled.__class__.objects.filter(pk=run.scheduled_id).update(
                scheduled_at=timezone.now() - datetime.timedelta(minutes=1))
            call_command('send_scheduled', stdout=StringIO())
        run = AgentRun.objects.get(pk=run.pk)
        self.assertTrue(run_state.text(run).startswith('✓ Надіслано '), run_state.text(run))
        self.assertEqual(run_state.text(self.scheduled_run('cancelled')), '✕ Відправку скасовано')
        self.assertIn('Не надіслано', run_state.text(self.scheduled_run('failed')))

    def test_draft_states(self):
        self.assertEqual(run_state.text(self.drafted_run()), '📝 Чернетка готова — чекає вас')
        self.assertEqual(run_state.text(self.drafted_run(AgentRun.SENT_SAME, with_draft=False)), '✓ Надіслано без змін')
        self.assertEqual(run_state.text(self.drafted_run(AgentRun.DISCARDED, with_draft=False)), '🗑 Чернетку видалено')
        closed = self.drafted_run(with_draft=False)
        self.assertEqual(run_state.text(closed), '📝 Чернетку закрито')
        AgentLetter.objects.create(account=self.account, message_id='<x@y>', run=closed, how=AgentLetter.EDITED)
        self.assertEqual(run_state.text(AgentRun.objects.get(pk=closed.pk)), '✏️ Надіслано з правками')

    def test_shown_on_rule_page_journal_and_preview(self):
        from email_assistant.models import ScheduledEmail
        run = self.scheduled_run()
        ScheduledEmail.objects.filter(pk=run.scheduled_id).update(status='sent', sent_at=timezone.now())
        self.client.force_login(self.user)
        page = self.client.get(f'/admin/agent/agentrule/{run.rule_id}/change/').content.decode()
        self.assertIn('✓ Надіслано', page)
        self.assertNotIn('✉️ ✉️', page)
        self.assertNotIn('Заплановано надіслати', page)
        journal = self.client.get('/admin/agent/agentrun/').content.decode()
        self.assertIn('✓ Надіслано', journal)
        runs = agent_mail.runs_for([run.message])
        self.assertTrue(runs[run.message_id][0].done_text.startswith('✓ Надіслано'))
