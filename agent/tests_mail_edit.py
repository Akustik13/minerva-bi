"""✍️ Лист завдання агенту: «🔄 Перегенерувати лист» (що змінити + мова, agent/mail_edit.py), «Копія (CC)» на
сторінці перевірки, поля завдання «Кому надсилати листи» / «Копія (CC)». AI підмінено.

python manage.py test agent.tests_mail_edit --settings=tabele.settings_test
"""
import json
from unittest import mock

from agent import decide, mail_edit, tools
from agent.models import AgentProposal, AgentTask
from agent.tests_tasks import Base

BODY = ('Добрий день, В\'ячеславе!\n\nСтаном на 2026-11-04 є 4 замовлення з DigiKey, які ще не відправлені.\n\n'
        '1. Замовлення 102572339 — LOG.IN SRL (Італія, ROMA)\n   - AN220201-04A (антена, 2 шт.)')


class MailEditTests(Base):
    def setUp(self):
        super().setUp()
        from email_assistant.models import EmailAccount
        EmailAccount.objects.create(user=self.user, email_address='slava@sevskiy.de', imap_username='x',
                                    imap_password='x', smtp_username='x', smtp_password='x')
        self.client.force_login(self.user)

    def proposal(self, **task_kw):
        task = self.task('Перевір замовлення DigiKey і напиши на viacheslav.pryimak@sevskiy.de', **task_kw)
        return AgentProposal.objects.create(task=task, kind=AgentProposal.MAIL, mail_to='viacheslav.pryimak@sevskiy.de',
                                            mail_subject='Замовлення DigiKey, не відправлені', mail_body=BODY)

    def ai(self, subject='DigiKey-Bestellungen, nicht versendet', body='Hallo Slava,\n\n1. Bestellung 102572339'):
        return mock.patch('email_assistant.ai_helper._call_ai_direct',
                          return_value=json.dumps({'subject': subject, 'body': body}))

    def test_rewrite_with_change_language_and_cc(self):
        p = self.proposal()
        with self.ai() as call:
            r = self.client.post(f'/admin/agent/agenttask/{p.task_id}/review/', {
                'action': 'mail_rewrite', 'pid': p.pk, 'change': 'Звернення: Hallo Slava', 'lang': 'de',
                'subject': p.mail_subject, 'body': p.mail_body, 'cc': 'sergey@sevskiy.de'})
        self.assertEqual(r.status_code, 302)
        prompt = call.call_args.args[0]
        self.assertIn('Звернення: Hallo Slava', prompt)
        self.assertIn('німецькою (Deutsch)', prompt)
        self.assertIn('102572339', prompt)
        self.assertEqual(call.call_args.kwargs['model'], p.task.model)
        p.refresh_from_db()
        self.assertEqual((p.mail_subject, p.status), ('DigiKey-Bestellungen, nicht versendet', AgentProposal.PENDING))
        self.assertTrue(p.mail_body.startswith('Hallo Slava'))
        self.assertEqual((p.data['cc'], p.data['lang']), (['sergey@sevskiy.de'], 'de'))
        p.task.refresh_from_db()
        self.assertIn('✍️ Лист переписано', p.task.log[-1]['text'])
        page = self.client.get(f'/admin/agent/agenttask/{p.task_id}/review/').content.decode()
        self.assertIn('value="sergey@sevskiy.de"', page)
        self.assertIn('<option value="de" selected>', page)

    def test_rewrite_needs_change_or_language_and_valid_cc(self):
        p = self.proposal()
        with self.ai() as call:
            self.assertEqual(mail_edit.rewrite(p, self.user), 'напишіть, що змінити, або виберіть мову')
            self.assertIn('не схоже на адресу', mail_edit.rewrite(p, self.user, change='коротше', cc='sergey@'))
        call.assert_not_called()
        self.assertEqual(mail_edit.parse('Просто текст'), {'subject': '', 'body': 'Просто текст'})

    def test_cc_goes_into_draft_and_scheduled(self):
        from email_assistant.models import EmailDraft, ScheduledEmail
        p1, p2 = self.proposal(), self.proposal()
        self.assertEqual(decide.mail(p1, self.user, cc='sergey@sevskiy.de, anna@kunde.de'), '')
        self.assertEqual(EmailDraft.objects.get().cc_emails, ['sergey@sevskiy.de', 'anna@kunde.de'])
        p2.data = {'cc': ['sergey@sevskiy.de']}
        p2.save(update_fields=['data'])
        self.assertEqual(decide.mail(p2, self.user, send=True), '')            # CC із пропозиції
        self.assertEqual(ScheduledEmail.objects.get().cc_emails, ['sergey@sevskiy.de'])

    def test_task_to_and_cc_fields(self):
        task = self.task('Знайди не відправлені замовлення DigiKey і напиши, що підготувати',
                         mail_to='viacheslav.pryimak@sevskiy.de', mail_cc='sergey@sevskiy.de')
        self.assertIn('viacheslav.pryimak@sevskiy.de', tools.allowed_addresses(task))
        ctx = tools.Context(task)
        out = tools.run(ctx, 'propose_email', {'to': [], 'subject': 'Замовлення', 'body': 'Текст'})
        self.assertIn('viacheslav.pryimak@sevskiy.de', str(out))
        p = AgentProposal.objects.get(task=task)
        self.assertEqual((p.mail_to, p.data['cc']), ('viacheslav.pryimak@sevskiy.de', ['sergey@sevskiy.de']))
        bad = tools.run(ctx, 'propose_email', {'to': ['boss@other.com'], 'subject': 'x', 'body': 'y'})
        self.assertIn('немає в завданні', str(bad))

    def test_add_form_fields(self):
        r = self.client.post('/admin/agent/agenttask/add/', {
            'instruction': 'Напиши лист про замовлення', 'mail_to': 'viacheslav.pryimak@sevskiy.de',
            'mail_cc': 'not-an-address', 'model': 'claude-haiku-5-5', 'budget_usd': '2'})
        self.assertContains(r, 'Не схоже на адресу')
        self.assertFalse(AgentTask.objects.exists())
        page = self.client.get('/admin/agent/agenttask/add/')
        self.assertContains(page, 'Кому надсилати листи')
        self.assertContains(page, 'Копія (CC)')
