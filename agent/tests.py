"""🤖 Агент Minerva — доручення для пошти (agent/mail.py). AI підмінено, нічого не надсилається.

python manage.py test agent --settings=tabele.settings_test
"""
import datetime
import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from agent import mail as agent_mail
from agent.models import AgentRule, AgentRun
from email_assistant.models import EmailAccount, EmailDraft, EmailMessage, EmailThread, ScheduledEmail

AI = 'email_assistant.ai_helper._call_ai_direct'


def ai_json(**kw):
    data = {'skip': False, 'skip_reason': '', 'reply': 'Vielen Dank, wir haben das Dokument erhalten.',
            'confident': True, 'doubts': ''}
    data.update(kw)
    return json.dumps(data, ensure_ascii=False)


class Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        self.user = User.objects.create_superuser('slava', 's@x.y', 'p')
        self.account = EmailAccount.objects.create(
            user=self.user, email_address='viacheslav.pryimak@sevskiy.de',
            imap_username='viacheslav.pryimak@sevskiy.de', imap_password='x',
            smtp_username='viacheslav.pryimak@sevskiy.de', smtp_password='x',
            imap_folder_inbox='INBOX', imap_folder_sent='Gesendete Objekte')
        self.uid = 100

    def mail(self, subject='Lieferschein 4711', sender='nicolas.fischer@m2logistik.de', body='Anbei der Lieferschein.',
             folder=EmailMessage.FOLDER_INBOX, when=None, thread=None):
        self.uid += 1
        when = when or timezone.now()
        thread = thread or EmailThread.objects.create(account=self.account, subject=subject, last_message_at=when)
        return EmailMessage.objects.create(
            account=self.account, thread=thread, folder=folder, imap_folder_name='INBOX', imap_uid=self.uid,
            subject=subject, from_email=sender, to_emails=[self.account.email_address], cc_emails=[],
            body_text=body, sent_at=when)

    def rule(self, **kw):
        data = dict(account=self.account, name='Lieferscheine', senders='@m2logistik.de',
                    subject_words='lieferschein', instruction='Bedanke dich kurz für den Lieferschein.')
        data.update(kw)
        rule = AgentRule(**data)
        rule.seen_pk = agent_mail.start_pk(self.account)
        rule.save()
        return rule


class MatchTests(Base):
    def test_all_conditions_together(self):
        rule = self.rule()
        self.assertTrue(agent_mail.matches(rule, self.mail())[0])
        self.assertEqual(agent_mail.matches(rule, self.mail(sender='anna@kunde.de'))[1], 'інший відправник')
        self.assertEqual(agent_mail.matches(rule, self.mail(subject='Rechnung 12'))[1], 'інша тема')

    def test_without_conditions_never(self):
        rule = self.rule(senders='', subject_words='')
        self.assertFalse(agent_mail.matches(rule, self.mail())[0])

    def test_category_condition(self):
        from email_assistant.models import MailCategory
        cat = MailCategory.objects.create(name='Logistik', senders='@m2logistik.de')
        rule = self.rule(senders='', subject_words='', category=cat)
        self.assertTrue(agent_mail.matches(rule, self.mail())[0])
        self.assertFalse(agent_mail.matches(rule, self.mail(sender='anna@kunde.de'))[0])

    def test_blocked(self):
        rule = self.rule()
        self.assertIn('noreply', agent_mail.blocked(rule, self.mail(sender='noreply@dakosy.de')))
        self.assertEqual(agent_mail.blocked(rule, self.mail(sender='viacheslav.pryimak@sevskiy.de')), 'лист від нас самих')
        self.assertIn('автовідповідь', agent_mail.blocked(rule, self.mail(subject='Automatic reply: Lieferschein')))
        old = self.mail(when=timezone.now() - datetime.timedelta(days=5))
        self.assertIn('три дні', agent_mail.blocked(rule, old))
        self.assertEqual(agent_mail.blocked(rule, old, check_age=False), '')

    def test_blocked_when_already_answered(self):
        rule = self.rule()
        msg = self.mail()
        EmailMessage.objects.create(account=self.account, thread=msg.thread, folder=EmailMessage.FOLDER_SENT,
                                    imap_folder_name='minerva:x', imap_uid=0, subject='Re: Lieferschein 4711',
                                    from_email=self.account.email_address, to_emails=[msg.from_email],
                                    cc_emails=[], body_text='Danke', sent_at=timezone.now())
        self.assertEqual(agent_mail.blocked(rule, msg), 'на лист уже відповіли')

    def test_subject_phrase(self):
        self.assertEqual(agent_mail.subject_phrase('AW: ABD - Ausfuhrbegleitdokument (DE), 187472-1, 26DE76'),
                         'ABD - Ausfuhrbegleitdokument (DE)')


class RunTests(Base):
    def test_old_mail_is_not_touched_new_mail_gets_draft(self):
        before = self.mail()                     # лежав у скриньці до доручення
        rule = self.rule()
        msg = self.mail()
        with mock.patch(AI, return_value=ai_json()) as ai:
            out = agent_mail.run_due()
        self.assertEqual((out['queued'], out['done']), (1, 1))
        self.assertFalse(AgentRun.objects.filter(message=before).exists())
        run = AgentRun.objects.get(message=msg)
        self.assertEqual(run.status, AgentRun.DRAFTED)
        draft = EmailDraft.objects.get(pk=run.draft_id)
        self.assertEqual((draft.reply_to, draft.to_emails, draft.subject), (msg, [msg.from_email], 'Re: Lieferschein 4711'))
        self.assertTrue(draft.body.startswith('Vielen Dank, wir haben das Dokument erhalten.\n\nMinerva\n'
                                              'KI-Assistent von '), draft.body)   # 🤖 підпис AI-помічника
        self.assertEqual(run.reply, 'Vielen Dank, wir haben das Dokument erhalten.')
        prompt = ai.call_args[0][0]
        self.assertIn('Bedanke dich kurz für den Lieferschein.', prompt)
        self.assertIn('ДАНІ від сторонніх людей', prompt)
        self.assertIn('Anbei der Lieferschein.', prompt)
        with mock.patch(AI, return_value=ai_json()):
            self.assertEqual(agent_mail.run_due()['done'], 0)   # той самий лист удруге — ні

    def test_skip_and_not_matching(self):
        self.rule()
        self.mail(sender='anna@kunde.de')        # не підходить — у черзі не з'являється
        msg = self.mail()
        with mock.patch(AI, return_value=ai_json(skip=True, skip_reason='це рахунок, не Lieferschein')):
            agent_mail.run_due()
        run = AgentRun.objects.get()
        self.assertEqual((run.message, run.status), (msg, AgentRun.SKIPPED))
        self.assertIn('це рахунок', run.reason)
        self.assertFalse(EmailDraft.objects.exists())

    def test_send_mode_schedules_with_delay(self):
        self.rule(mode=AgentRule.SEND, send_delay_minutes=15)
        msg = self.mail()
        with mock.patch(AI, return_value=ai_json()):
            agent_mail.run_due()
        run = AgentRun.objects.get()
        self.assertEqual(run.status, AgentRun.SCHEDULED)
        sched = ScheduledEmail.objects.get(pk=run.scheduled_id)
        self.assertEqual((sched.reply_to, sched.to_emails, sched.trigger), (msg, [msg.from_email], 'agent'))
        self.assertGreater(sched.scheduled_at, timezone.now() + datetime.timedelta(minutes=14))
        self.assertFalse(EmailDraft.objects.exists())

    def test_send_mode_not_confident_or_over_limit_only_draft(self):
        self.rule(mode=AgentRule.SEND, daily_limit=1)
        self.mail()
        with mock.patch(AI, return_value=ai_json(confident=False, doubts='Termin fehlt')):
            agent_mail.run_due()
        run = AgentRun.objects.get()
        self.assertEqual(run.status, AgentRun.DRAFTED)
        self.assertIn('не впевнений', run.reason)
        self.mail(subject='Lieferschein 4712')
        self.mail(subject='Lieferschein 4713')
        with mock.patch(AI, return_value=ai_json()):
            agent_mail.run_due()
        statuses = sorted(AgentRun.objects.values_list('status', flat=True))
        self.assertEqual(statuses, [AgentRun.DRAFTED, AgentRun.DRAFTED, AgentRun.SCHEDULED])
        self.assertTrue(AgentRun.objects.filter(reason__contains='ліміт 1').exists())

    def test_no_ai_key_fails_without_draft(self):
        self.rule()
        self.mail()
        with mock.patch(AI, return_value=''):
            agent_mail.run_due()
        run = AgentRun.objects.get()
        self.assertEqual(run.status, AgentRun.FAILED)
        self.assertFalse(EmailDraft.objects.exists())

    def test_disabled_rule_does_nothing(self):
        self.rule(enabled=False)
        self.mail()
        with mock.patch(AI) as ai:
            agent_mail.run_due()
        ai.assert_not_called()
        self.assertFalse(AgentRun.objects.exists())

    def test_parse_not_json(self):
        out = agent_mail.parse('Danke!\n\nMit freundlichen Grüßen\nSlava')
        self.assertEqual((out['reply'], out['confident']), ('Danke!', False))

    def test_cron_command(self):
        from django.core.management import call_command
        self.rule()
        self.mail()
        with mock.patch(AI, return_value=ai_json()):
            call_command('agent_runs')
        self.assertEqual(AgentRun.objects.get().status, AgentRun.DRAFTED)


class OutcomeTests(Base):
    def _drafted(self):
        rule = self.rule()
        self.mail()
        with mock.patch(AI, return_value=ai_json()):
            agent_mail.run_due()
        return rule, AgentRun.objects.get()

    def test_sent_unchanged_counts_and_promotes(self):
        from email_assistant import drafts
        rule, run = self._drafted()
        drafts.discard(self.account, run.draft_id, how='sent',
                       text='Vielen Dank, wir haben das Dokument erhalten.\n\n--\nViacheslav Pryimak\n> Anbei …')
        run.refresh_from_db()
        rule.refresh_from_db()
        self.assertEqual((run.outcome, rule.streak), (AgentRun.SENT_SAME, 1))
        rule.streak = agent_mail.PROMOTE_AFTER
        self.assertTrue(agent_mail.promote_ready(rule))

    def test_edited_or_deleted_resets(self):
        from email_assistant import drafts
        rule, run = self._drafted()
        AgentRule.objects.filter(pk=rule.pk).update(streak=3)
        drafts.discard(self.account, run.draft_id, how='sent', text='Danke, erhalten. Termin folgt.')
        run.refresh_from_db()
        rule.refresh_from_db()
        self.assertEqual((run.outcome, rule.streak), (AgentRun.SENT_EDITED, 0))

    def test_deleted(self):
        rule, run = self._drafted()
        self.client.force_login(self.user)
        self.client.post(f'/email/draft/{run.draft_id}/delete/')
        run.refresh_from_db()
        self.assertEqual(run.outcome, AgentRun.DISCARDED)


class ViewTests(Base):
    def setUp(self):
        super().setUp()
        from email_assistant.models import EmailSettings
        EmailSettings.objects.update_or_create(user=self.user, defaults={'mark_read_on_server': False})  # без IMAP
        self.client.force_login(self.user)
        self.msg = self.mail(subject='AW: ABD - Ausfuhrbegleitdokument (DE), 187472-1')

    def test_suggest(self):
        d = self.client.get(f'/agent/mail/{self.msg.pk}/').json()
        self.assertEqual((d['sender'], d['domain'], d['subject']),
                         ('nicolas.fischer@m2logistik.de', '@m2logistik.de', 'ABD - Ausfuhrbegleitdokument (DE)'))
        self.assertEqual(d['blocked'], '')

    def test_try_saves_nothing(self):
        data = {'name': 'ABD', 'senders': '@m2logistik.de', 'instruction': 'Danke sagen.'}
        with mock.patch(AI, return_value=ai_json()):
            d = self.client.post(f'/agent/mail/{self.msg.pk}/try/', json.dumps(data),
                                 content_type='application/json').json()
        self.assertEqual((d['ok'], d['matches']), (True, True))
        self.assertTrue(d['reply'].startswith('Vielen Dank, wir haben das Dokument erhalten.\n\nMinerva'), d['reply'])
        self.assertFalse(AgentRule.objects.exists() or EmailDraft.objects.exists())

    def test_save_with_draft_now(self):
        data = {'name': 'ABD', 'senders': '@m2logistik.de', 'instruction': 'Danke sagen.', 'mode': 'draft',
                'draft_now': True, 'reply': 'Danke!'}
        d = self.client.post(f'/agent/mail/{self.msg.pk}/save/', json.dumps(data),
                             content_type='application/json').json()
        rule = AgentRule.objects.get(pk=d['id'])
        self.assertEqual((rule.account, rule.example_message, rule.created_by), (self.account, self.msg, self.user))
        self.assertGreaterEqual(rule.seen_pk, self.msg.pk)       # уже наявні листи — не чіпає
        body = EmailDraft.objects.get(pk=d['draft']).body
        self.assertTrue(body.startswith('Danke!\n\nMinerva\nKI-Assistent von '), body)
        self.assertEqual(AgentRun.objects.get().status, AgentRun.DRAFTED)

    def test_save_needs_condition_and_instruction(self):
        url = f'/agent/mail/{self.msg.pk}/save/'
        d = self.client.post(url, json.dumps({'instruction': 'x'}), content_type='application/json').json()
        self.assertIn('хоча б одна умова', d['error'])
        d = self.client.post(url, json.dumps({'senders': '@a.de'}), content_type='application/json').json()
        self.assertIn('інструкцію', d['error'])

    def test_other_mailbox_is_404_and_hidden_in_admin(self):
        other = User.objects.create_superuser('anna', 'a@x.y', 'p')
        rule = self.rule()
        self.client.force_login(other)
        self.assertEqual(self.client.get(f'/agent/mail/{self.msg.pk}/').status_code, 404)
        self.assertEqual(self.client.get('/admin/agent/agentrule/').status_code, 200)
        self.assertNotContains(self.client.get('/admin/agent/agentrule/'), 'Lieferscheine')
        self.assertIn(self.client.get(f'/admin/agent/agentrule/{rule.pk}/change/').status_code, (302, 404))

    def test_admin_pages_and_preview_line(self):
        rule = self.rule()
        msg = self.mail()
        with mock.patch(AI, return_value=ai_json()):
            agent_mail.run_due()
        self.assertContains(self.client.get('/admin/agent/agentrule/'), 'Lieferscheine')
        self.assertContains(self.client.get(f'/admin/agent/agentrule/{rule.pk}/change/'), 'відкрити чернетку')
        self.assertEqual(self.client.get('/admin/agent/agentrun/').status_code, 200)
        page = self.client.get(f'/email/message/{msg.pk}/preview/')
        self.assertContains(page, 'чернетка відповіді готова')
        self.assertContains(self.client.get('/email/'), 'em-agent-dlg')
