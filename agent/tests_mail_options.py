"""📬 Листи агента: ❗ Важливо, запит на прочитання й ⏰ нагадування з доручення (AgentRule.opt_* / remind_*) —
у чернетку й запланований лист, а з них — у форму «Написати» й надсилання (email_assistant/followups.py).

python manage.py test agent.tests_mail_options --settings=tabele.settings_test
"""
import datetime
import io
import json
from unittest import mock

from django.utils import timezone

from agent import mail as agent_mail
from agent.models import AgentRule, AgentRun
from agent.tests_tasks import Base

OUR_ID = '<agent.1@sevskiy.de>'


class MailOptionsTests(Base):
    def setUp(self):
        super().setUp()
        from email_assistant.models import EmailAccount
        self.account = EmailAccount.objects.create(user=self.user, email_address='slava@sevskiy.de')
        self.uid = 0

    def mail(self, subject='Rechnung Nr. 968548', frm='vlad.balan@m2logistik.de'):
        from email_assistant.models import EmailMessage
        self.uid += 1
        return EmailMessage.objects.create(account=self.account, folder='inbox', imap_folder_name='INBOX',
                                           imap_uid=self.uid, subject=subject, from_email=frm,
                                           body_text='Anbei die Rechnung.', to_emails=['slava@sevskiy.de'],
                                           sent_at=timezone.now())

    def rule(self, **kw):
        defaults = dict(account=self.account, name='Переслати: рахунки', kind=AgentRule.MAIL_FORWARD,
                        senders='@m2logistik.de', forward_to='sergey@sevskiy.de', instruction='Прошу оплатити.',
                        opt_receipt=True, remind_days=3, remind_until='reply')
        defaults.update(kw)
        return AgentRule.objects.create(**defaults)

    def test_draft_carries_options_into_compose_and_send(self):
        from email_assistant.models import EmailDraft, MailFollowup
        self.rule(opt_important=True)
        self.mail()
        agent_mail.run_due()
        draft = EmailDraft.objects.get()
        self.assertEqual(draft.send_options, {'importance': 'important', 'read_receipt': True, 'remind_days': 3,
                                              'until': 'reply'})
        self.client.force_login(self.user)
        page = self.client.get(f'/email/compose/?draft={draft.pk}').content.decode()
        for name in ('opt_important', 'opt_receipt', 'opt_remind'):
            self.assertRegex(page, rf'name="{name}" value="1"[^>]*checked')
        self.assertIn('<option value="reply" selected>', page)
        self.assertIn('<option value="3" selected>', page)
        with mock.patch('email_assistant.smtp_client.SMTPClient.send',
                        return_value={'ok': True, 'message_id': OUR_ID}) as send:
            d = self.client.post('/email/send/', {'to': 'sergey@sevskiy.de', 'subject': draft.subject, 'body': 'x',
                                                  'draft_pk': draft.pk, 'opt_important': '1', 'opt_receipt': '1',
                                                  'opt_remind': '1', 'remind_days': '3',
                                                  'remind_until': 'reply'}).json()
        self.assertTrue(d['ok'], d)
        self.assertEqual(send.call_args.kwargs['read_receipt'], True)
        self.assertEqual(MailFollowup.objects.get(msg_id=OUR_ID).until, 'reply')

    def test_send_mode_scheduled_with_options(self):
        from django.core.management import call_command
        from email_assistant.models import MailFollowup, ScheduledEmail
        self.rule(mode=AgentRule.SEND, send_delay_minutes=0)
        self.mail()
        agent_mail.run_due()
        s = ScheduledEmail.objects.get()
        self.assertEqual((s.send_options['read_receipt'], s.send_options['remind_days']), (True, 3))
        ScheduledEmail.objects.filter(pk=s.pk).update(scheduled_at=timezone.now() - datetime.timedelta(minutes=1))
        with mock.patch('email_assistant.smtp_client.SMTPClient.send',
                        return_value={'ok': True, 'message_id': OUR_ID}) as send:
            call_command('send_scheduled', stdout=io.StringIO())
        self.assertTrue(send.call_args.kwargs['read_receipt'])
        f = MailFollowup.objects.get(msg_id=OUR_ID)
        self.assertEqual((f.read_receipt, f.until), (True, 'reply'))
        self.assertAlmostEqual((f.remind_at - f.sent_at).total_seconds(), 3 * 86400, delta=5)

    def test_no_options_by_default(self):
        from email_assistant.models import EmailDraft
        self.rule(opt_receipt=False, remind_days=0)
        self.mail()
        agent_mail.run_due()
        self.assertEqual(EmailDraft.objects.get().send_options, {})

    def test_dialog_saves_options(self):
        msg = self.mail()
        self.client.force_login(self.user)
        data = {'kind': 'mail_forward', 'name': 'Переслати', 'senders': '@m2logistik.de', 'instruction': 'Оплатити',
                'forward_to': 'sergey@sevskiy.de', 'opt_receipt': True, 'remind_days': 5, 'reply': 'x',
                'draft_now': True}
        d = self.client.post(f'/agent/mail/{msg.pk}/save/', json.dumps(data), content_type='application/json').json()
        self.assertTrue(d['ok'], d)
        rule = AgentRule.objects.get(pk=d['id'])
        self.assertEqual((rule.opt_important, rule.opt_receipt, rule.remind_days), (False, True, 5))
        run = AgentRun.objects.get(rule=rule)
        self.assertEqual(run.draft.send_options['remind_days'], 5)

    def test_compose_draft_save_keeps_options(self):
        from email_assistant.models import EmailDraft
        self.client.force_login(self.user)
        d = self.client.post('/email/draft/save/', {'to': 'a@b.de', 'subject': 'Angebot', 'body': 'x',
                                                    'opt_receipt': '1'}).json()
        self.assertTrue(d['ok'], d)
        self.assertEqual(EmailDraft.objects.get(pk=d['draft_pk']).send_options['read_receipt'], True)
