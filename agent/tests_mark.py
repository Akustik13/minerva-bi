"""❗ Доручення «Позначати такі листи важливими» (agent/mail.py process_mark): ❗ / ‼️ листу, вибір людини
сильніший, «🔔 Одразу повідомити мене» — лист і Telegram власнику (agent/notify.deliver підмінено). Без AI.

python manage.py test agent.tests_mark --settings=tabele.settings_test
"""
import json
from unittest import mock

from django.utils import timezone

from agent import mail as agent_mail
from agent.models import AgentRule, AgentRun
from agent.tests_tasks import Base


class MarkTests(Base):
    def setUp(self):
        super().setUp()
        from email_assistant.models import EmailAccount
        self.account = EmailAccount.objects.create(user=self.user, email_address='slava@sevskiy.de')
        self.uid = 0

    def mail(self, subject='Zahlungserinnerung 968548', frm='buchhaltung@m2logistik.de'):
        from email_assistant.models import EmailMessage
        self.uid += 1
        return EmailMessage.objects.create(account=self.account, folder='inbox', imap_folder_name='INBOX',
                                           imap_uid=self.uid, subject=subject, from_email=frm,
                                           body_text='Bitte begleichen Sie die offene Rechnung bis Freitag.',
                                           to_emails=['slava@sevskiy.de'], sent_at=timezone.now())

    def rule(self, **kw):
        defaults = dict(account=self.account, name='Позначити: нагадування про оплату', kind=AgentRule.MAIL_MARK,
                        senders='@m2logistik.de', subject_words='Zahlungserinnerung', importance='urgent')
        defaults.update(kw)
        return AgentRule.objects.create(**defaults)

    def test_marks_without_ai_and_notifies(self):
        rule = self.rule(notify=True, instruction='Оплатити сьогодні')
        msg = self.mail()
        other = self.mail(subject='Lieferschein 4711')
        with mock.patch('email_assistant.ai_helper._call_ai_direct') as ai, \
                mock.patch('agent.notify.deliver', return_value='🔔 Повідомлено: пошта, Telegram') as deliver:
            agent_mail.run_due()
        ai.assert_not_called()
        msg.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((msg.importance, msg.importance_by), ('urgent', 'agent'))
        self.assertEqual(other.importance, '')
        run = AgentRun.objects.get(rule=rule)
        self.assertEqual(run.status, AgentRun.MARKED)
        self.assertIn('Повідомлено', run.reason)
        user, m = deliver.call_args.args
        self.assertEqual(user, self.user)
        self.assertTrue(m['subject'].startswith('‼️ Дуже важливо: Zahlungserinnerung'))
        self.assertIn('Примітка: Оплатити сьогодні', m['lines'])
        self.assertIn('Bitte begleichen', m['summary'])
        self.assertIn(f'/email/?msg={msg.pk}', m['link'])

    def test_human_choice_wins_and_no_notify_by_default(self):
        from email_assistant import importance
        self.rule(importance='important')
        msg = self.mail()
        importance.set_level([msg], '')                      # людина вирішила: звичайний
        with mock.patch('agent.notify.deliver') as deliver:
            agent_mail.run_due()
        deliver.assert_not_called()
        msg.refresh_from_db()
        self.assertEqual(msg.importance, '')
        self.assertIn('ви позначили лист інакше', AgentRun.objects.get(message=msg).reason)

    def test_dialog_rule_without_instruction(self):
        msg = self.mail()
        self.client.force_login(self.user)
        data = {'kind': 'mail_mark', 'name': 'Позначити', 'senders': '@m2logistik.de', 'instruction': '',
                'importance': 'urgent', 'notify': True}
        d = self.client.post(f'/agent/mail/{msg.pk}/try/', json.dumps(data), content_type='application/json').json()
        self.assertTrue(d['ok'], d)
        self.assertIn('‼️ Дуже важливо', d['reply'])
        d = self.client.post(f'/agent/mail/{msg.pk}/save/', json.dumps(data), content_type='application/json').json()
        self.assertTrue(d['ok'], d)
        rule = AgentRule.objects.get(pk=d['id'])
        self.assertEqual((rule.kind, rule.importance, rule.notify), (AgentRule.MAIL_MARK, 'urgent', True))
        msg.refresh_from_db()
        self.assertEqual(msg.importance, 'urgent')            # той самий лист — одразу, без повідомлення
        page = self.client.get(f'/email/message/{msg.pk}/preview/')
        self.assertContains(page, '‼️ Дуже важливо')
        # відповідь без інструкції — як і раніше, ні
        data.update(kind='mail_reply')
        d = self.client.post(f'/agent/mail/{msg.pk}/save/', json.dumps(data), content_type='application/json').json()
        self.assertFalse(d['ok'])
