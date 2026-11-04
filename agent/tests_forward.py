"""📤 Доручення «Пересилати такі листи» (agent/mail.py process_forward): рахунок M2 Logistik → Сергію з вкладеннями й
текстом «прошу оплатити»; без AI.

python manage.py test agent.tests_forward --settings=tabele.settings_test
"""
import json
from unittest import mock

from django.utils import timezone

from agent import mail as agent_mail
from agent.models import AgentRule, AgentRun
from agent.tests_tasks import Base

ATTS = [{'name': 'Logo-M2.png', 'content_type': 'image/png', 'size': 4000},
        {'name': 'logo-inline.png', 'content_type': 'image/png', 'size': 90000, 'is_inline': True},
        {'name': '968548-sevskiy gmbh.pdf', 'content_type': 'application/pdf', 'size': 157000},
        {'name': 'AN-AA_2026-09.pdf', 'content_type': 'application/pdf', 'size': 49000}]


class ForwardTests(Base):
    def setUp(self):
        super().setUp()
        from email_assistant.models import EmailAccount
        self.account = EmailAccount.objects.create(user=self.user, email_address='slava@sevskiy.de')
        self.uid = 0

    def mail(self, subject='Rechnung Nr. 968548', frm='vlad.balan@m2logistik.de'):
        from email_assistant.models import EmailMessage
        self.uid += 1
        return EmailMessage.objects.create(account=self.account, folder='inbox', imap_folder_name='INBOX',
                                           imap_uid=self.uid, subject=subject, from_email=frm, from_name='Vlad Balan',
                                           body_text='Anbei die Rechnung.\n\nMit freundlichen Grüßen\nVlad Balan',
                                           to_emails=['slava@sevskiy.de'], sent_at=timezone.now(),
                                           attachments=[dict(a) for a in ATTS])

    def rule(self, **kw):
        defaults = dict(account=self.account, name='Переслати: рахунки M2', kind=AgentRule.MAIL_FORWARD,
                        senders='@m2logistik.de', subject_words='Rechnung', forward_to='sergey@sevskiy.de',
                        instruction='Сергію, прошу оплатити рахунок. Дякую!')
        defaults.update(kw)
        return AgentRule.objects.create(**defaults)

    def test_draft_with_invoice_attachments_no_ai(self):
        from email_assistant.models import EmailDraft
        rule = self.rule()
        msg = self.mail()
        self.mail(subject='Lieferschein 4711')                               # не рахунок — не пересилається
        with mock.patch('email_assistant.ai_helper._call_ai_direct') as ai:
            agent_mail.run_due()
        ai.assert_not_called()
        run = AgentRun.objects.get(rule=rule)
        self.assertEqual((run.message_id, run.status), (msg.pk, AgentRun.DRAFTED))
        draft = EmailDraft.objects.get(pk=run.draft_id)
        self.assertEqual((draft.to_emails, draft.subject), (['sergey@sevskiy.de'], 'WG: Rechnung Nr. 968548'))
        self.assertEqual([a['name'] for a in draft.attachments], ['968548-sevskiy gmbh.pdf', 'AN-AA_2026-09.pdf'])
        self.assertEqual(draft.attachments[0]['ref'], f'msg:{msg.pk}:2')
        self.assertTrue(draft.body.startswith('Сергію, прошу оплатити рахунок. Дякую!'))
        self.assertIn('Von: Vlad Balan <vlad.balan@m2logistik.de>', draft.body)
        self.assertIn('Anbei die Rechnung.', draft.body)
        self.assertIn('вкладень: 2', run.reason)

    def test_send_mode_schedules_once(self):
        from email_assistant.models import ScheduledEmail
        self.rule(mode=AgentRule.SEND, cc='slava@sevskiy.de')
        self.mail()
        agent_mail.run_due()
        agent_mail.run_due()
        s = ScheduledEmail.objects.get()
        self.assertEqual((s.to_emails, s.cc_emails, len(s.attachments), s.trigger),
                         (['sergey@sevskiy.de'], ['slava@sevskiy.de'], 2, 'agent'))
        self.assertGreater(s.scheduled_at, timezone.now())

    def test_dialog_try_and_save(self):
        msg = self.mail()
        self.client.force_login(self.user)
        body = {'kind': 'mail_forward', 'name': 'Переслати: Rechnung', 'senders': '@m2logistik.de',
                'subject_words': 'Rechnung', 'forward_to': 'sergey@sevskiy.de',
                'instruction': 'Сергію, прошу оплатити рахунок.'}
        r = self.client.post(f'/agent/mail/{msg.pk}/try/', json.dumps(body), content_type='application/json').json()
        self.assertEqual((r['to'], r['subject'], r['attachments']),
                         ('sergey@sevskiy.de', 'WG: Rechnung Nr. 968548', ['968548-sevskiy gmbh.pdf', 'AN-AA_2026-09.pdf']))
        body.update(draft_now=True, reply=r['reply'])
        r = self.client.post(f'/agent/mail/{msg.pk}/save/', json.dumps(body), content_type='application/json').json()
        self.assertTrue(r['ok'], r)
        self.assertEqual(AgentRule.objects.get(pk=r['id']).kind, AgentRule.MAIL_FORWARD)
        self.assertTrue(r['draft'])
        no_to = dict(body, forward_to='')
        r = self.client.post(f'/agent/mail/{msg.pk}/save/', json.dumps(no_to), content_type='application/json').json()
        self.assertFalse(r['ok'])
        self.assertIn('кому пересилати', r['error'])
