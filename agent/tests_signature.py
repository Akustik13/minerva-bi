"""🤖 Підпис AI-помічника в листах агента (`AgentRule.ai_sign`, `agent.mail.ai_signature` / `with_ai_signature`):
відповіді й листи клієнту замовлення; мова — як у листі; підпис скриньки додається після нього при відправці.
Claude підмінено.

python manage.py test agent.tests_signature --settings=tabele.settings_test
"""
from unittest import mock

from agent import mail as agent_mail
from agent import rule_check
from agent.models import AgentRule, AgentRun
from agent.tests import AI, Base, ai_json
from agent.tests_customer_mail import LETTER
from agent.tests_repeat import RepeatBase

EN = 'Dear Mr. Obinwa,\nplease pay the brokerage fee of 46.80 USD to UPS. Thank you for your order.'
DE = 'Sehr geehrte Damen und Herren,\nbitte zahlen Sie die Gebühr bei UPS. Vielen Dank für Ihre Bestellung.'
UK = 'Добрий день!\nБудь ласка, оплатіть збір UPS — 46,80 USD. Дякуємо за замовлення.'


class SignatureTextTests(Base):
    def setUp(self):
        super().setUp()
        self.user.first_name, self.user.last_name = 'Viacheslav', 'Pryimak'
        self.user.save()
        from config.models import SystemSettings
        SystemSettings.objects.filter(pk=1).update(company_name='Sevskiy GmbH')

    def test_full_signature_in_language_of_letter(self):
        rule = self.rule()
        en = agent_mail.ai_signature(rule, EN)
        self.assertTrue(en.startswith('Minerva\nAI assistant to Viacheslav Pryimak · Sevskiy GmbH\n\n'), en)
        self.assertIn('This email was written by an AI assistant on behalf of Viacheslav Pryimak.', en)
        self.assertIn('your message will reach Viacheslav Pryimak directly', en)
        de = agent_mail.ai_signature(rule, DE)
        self.assertTrue(de.startswith('Minerva\nKI-Assistent von Viacheslav Pryimak · Sevskiy GmbH'), de)
        self.assertIn('im Auftrag von Viacheslav Pryimak verfasst', de)
        self.assertIn('AI-помічник · Viacheslav Pryimak', agent_mail.ai_signature(rule, UK))
        self.assertEqual(agent_mail.letter_lang('Vielen Dank, wir haben das Dokument erhalten.'), 'de')

    def test_assistant_name_and_company_not_set(self):
        from config.models import SystemSettings
        SystemSettings.objects.filter(pk=1).update(company_name='Моя компанія')     # не заповнено
        rule = self.rule(ai_sign_name='Anna')
        self.assertTrue(agent_mail.ai_signature(rule, EN).startswith('Anna\nAI assistant to Viacheslav Pryimak\n\n'))

    def test_line_off_and_own_text(self):
        line = self.rule(ai_sign=AgentRule.SIGN_LINE)
        self.assertEqual(agent_mail.ai_signature(line, DE), agent_mail.AI_LINES['de'])
        off = self.rule(ai_sign=AgentRule.SIGN_OFF)
        self.assertEqual(agent_mail.with_ai_signature(off, EN + '\n\n'), EN)
        own = self.rule(ai_sign_name='Anna', ai_sign_text='— {assistant}, digitale Assistenz von {owner} ({company})')
        self.assertEqual(agent_mail.ai_signature(own, EN),
                         '— Anna, digitale Assistenz von Viacheslav Pryimak (Sevskiy GmbH)')

    def test_not_added_twice(self):
        rule = self.rule()
        once = agent_mail.with_ai_signature(rule, EN)
        self.assertEqual(agent_mail.with_ai_signature(rule, once), once)
        self.assertEqual(once.count('Minerva\nAI assistant to'), 1)

    def test_plan_and_admin_preview(self):
        rule = self.rule()
        self.assertTrue(any('підпис AI-помічника мовою листа' in ln for ln in rule_check.plan(rule)))
        rule.ai_sign = AgentRule.SIGN_OFF
        self.assertTrue(any('Без позначки AI' in ln for ln in rule_check.plan(rule)))
        rule.save()
        forward = self.rule(kind=AgentRule.MAIL_FORWARD, forward_to='a@b.de')
        self.assertFalse(any('AI-помічника' in ln for ln in rule_check.plan(forward)))
        AgentRule.objects.filter(pk=rule.pk).update(ai_sign=AgentRule.SIGN_FULL)
        self.client.force_login(self.user)
        page = self.client.get(f'/admin/agent/agentrule/{rule.pk}/change/')
        self.assertContains(page, 'KI-Assistent von Viacheslav Pryimak · Sevskiy GmbH')
        self.assertContains(page, 'AI assistant to Viacheslav Pryimak · Sevskiy GmbH')

    def test_reply_scheduled_with_signature(self):
        from email_assistant.models import ScheduledEmail
        self.rule(mode=AgentRule.SEND)
        self.mail()
        with mock.patch(AI, return_value=ai_json()):
            agent_mail.run_due()
        run = AgentRun.objects.get()
        body = ScheduledEmail.objects.get(pk=run.scheduled_id).body
        self.assertTrue(body.startswith('Vielen Dank, wir haben das Dokument erhalten.\n\nMinerva\nKI-Assistent von '
                                        'Viacheslav Pryimak · Sevskiy GmbH'), body)
        self.assertEqual(run.reply, 'Vielen Dank, wir haben das Dokument erhalten.')   # журнал — текст AI


class CustomerSignatureTests(RepeatBase):
    def test_customer_letter_draft_and_try(self):
        from email_assistant.models import EmailDraft
        rule = self.rule()
        msg = self.mail()
        with self.ai():
            agent_mail.run_due()
        run = AgentRun.objects.get(rule=rule, message=msg)
        draft = EmailDraft.objects.get(pk=run.draft_id)
        self.assertTrue(draft.body.startswith(LETTER['reply'] + '\n\nMinerva\nAI assistant to '), draft.body)
        self.assertEqual(run.reply, LETTER['reply'])

        # «🧪 Спробувати» у вікні (доручення ще не збережене) — лист так, як піде
        trial = AgentRule(account=self.account, kind=AgentRule.MAIL_CUSTOMER, instruction=rule.instruction)
        with self.ai():
            out = agent_mail.try_on(trial, msg)
        self.assertIn('This email was written by an AI assistant', out['reply'])
        trial.ai_sign = AgentRule.SIGN_OFF
        with self.ai():
            out = agent_mail.try_on(trial, msg)
        self.assertEqual(out['reply'], LETTER['reply'])
