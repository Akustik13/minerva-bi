"""🧪 «Перевірити, як працює» на сторінці доручення (agent/rule_check.py, AgentRuleAdmin.test_panel / test_view): опис,
листи, що підходять (і майже підходять), що агент зробив би — без збереження й надсилання.

python manage.py test agent.tests_rule_check --settings=tabele.settings_test
"""
import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone

from agent import rule_check
from agent.models import AgentRule, AgentRun
from agent.tests_tasks import Base


class RuleTestTests(Base):
    def setUp(self):
        super().setUp()
        from email_assistant.models import EmailAccount
        self.account = EmailAccount.objects.create(user=self.user, email_address='slava@sevskiy.de')
        self.uid = 0
        self.client.force_login(self.user)

    def mail(self, subject='Rechnung Nr. 968548 ABD', frm='vlad.balan@m2logistik.de', days=0):
        from email_assistant.models import EmailMessage
        self.uid += 1
        return EmailMessage.objects.create(account=self.account, folder='inbox', imap_folder_name='INBOX',
                                           imap_uid=self.uid, subject=subject, from_email=frm, from_name='Vlad',
                                           body_text='Anbei die Rechnung.', to_emails=['slava@sevskiy.de'],
                                           sent_at=timezone.now() - datetime.timedelta(days=days))

    def rule(self, **kw):
        defaults = dict(account=self.account, name='Переслати: рахунки M2', kind=AgentRule.MAIL_FORWARD,
                        senders='@m2logistik.de', subject_words='Rechnung', forward_to='sergey@sevskiy.de',
                        instruction='Сергей, нужно оплатить счёт.', opt_receipt=True, remind_days=3,
                        remind_until='reply')
        defaults.update(kw)
        return AgentRule.objects.create(**defaults)

    def test_page_lists_matching_and_near_mails(self):
        from email_assistant.models import EmailDraft, ScheduledEmail
        rule = self.rule()
        hit = self.mail(days=10)                                   # давній — у перевірці теж видно
        near = self.mail(subject='Lieferschein 4711')              # той самий відправник, інша тема
        self.mail(subject='Rechnung 1', frm='info@other.de')       # інший відправник — не показується
        page = self.client.get(f'/admin/agent/agentrule/{rule.pk}/change/').content.decode()
        self.assertIn('🧪 Перевірити, як працює', page)
        self.assertIn('📤 Пересилає лист з усіма вкладеннями на sergey@sevskiy.de', page)
        self.assertIn('📬 із запитом на прочитання', page)
        self.assertIn(f'data-art="{hit.pk}"', page)
        self.assertNotIn(f'data-art="{near.pk}"', page)
        self.assertIn('інша тема', page)
        self.assertIn('Листи за 60 днів, що підходять: 1', page)
        d = self.client.post(f'/admin/agent/agentrule/{rule.pk}/test/{hit.pk}/').json()
        self.assertTrue(d['ok'], d)
        self.assertEqual((d['to'], d['subject']), ('sergey@sevskiy.de', 'WG: Rechnung Nr. 968548 ABD'))
        self.assertIn('📝 Поклав би чернетку', d['outcome'])
        self.assertIn('нагадаю вам через 3 дн., якщо немає відповіді', d['options'])
        self.assertTrue(d['reply'].startswith('Сергей, нужно оплатить счёт.'))
        self.assertFalse(EmailDraft.objects.exists() or ScheduledEmail.objects.exists() or AgentRun.objects.exists())

    def test_send_mode_and_ai_reply(self):
        rule = self.rule(kind=AgentRule.MAIL_REPLY, mode=AgentRule.SEND, send_delay_minutes=10, forward_to='')
        msg = self.mail()
        ai = {'skip': False, 'skip_reason': '', 'reply': 'Danke, erhalten.', 'confident': True, 'doubts': '',
              'model': 'claude-haiku-5-5'}
        with mock.patch('agent.mail.generate', return_value=ai) as gen:
            d = self.client.post(f'/admin/agent/agentrule/{rule.pk}/test/{msg.pk}/').json()
        gen.assert_called_once()
        self.assertIn('✉️ Надіслав би сам', d['outcome'])
        self.assertTrue(d['ai'])
        self.assertIn('(AI)', self.client.get(f'/admin/agent/agentrule/{rule.pk}/change/').content.decode())

    def test_mark_task_and_nothing_found(self):
        rule = self.rule(kind=AgentRule.MAIL_MARK, importance='urgent', notify=True, forward_to='')
        msg = self.mail()
        with mock.patch('agent.notify.deliver') as deliver:
            d = self.client.post(f'/admin/agent/agentrule/{rule.pk}/test/{msg.pk}/').json()
        deliver.assert_not_called()
        msg.refresh_from_db()
        self.assertEqual((msg.importance, d['outcome']), ('', '‼️ Дуже важливо · 🔔 одразу лист і Telegram вам'))
        task = self.rule(kind=AgentRule.MAIL_TASK, forward_to='')
        d = self.client.post(f'/admin/agent/agentrule/{task.pk}/test/{msg.pk}/').json()
        self.assertIn('У перевірці не запускається', d['outcome'])
        empty = self.rule(senders='@nobody.example')
        self.assertIn('Жоден лист за 60 днів не підходить',
                      self.client.get(f'/admin/agent/agentrule/{empty.pk}/change/').content.decode())
        self.assertEqual(rule_check.candidates(empty)['found'], [])

    def test_only_owner_and_post(self):
        rule = self.rule()
        msg = self.mail()
        self.assertEqual(self.client.get(f'/admin/agent/agentrule/{rule.pk}/test/{msg.pk}/').status_code, 405)
        other = User.objects.create_superuser('olga', 'o@x.y', 'p')
        self.client.force_login(other)
        self.assertEqual(self.client.post(f'/admin/agent/agentrule/{rule.pk}/test/{msg.pk}/').status_code, 404)
