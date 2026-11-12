"""Відповідь як відповідь у гілці: історія листування під текстом, References усієї гілки, «↩ Відповісти» з AI.
python manage.py test email_assistant --settings=tabele.settings_test"""
from email import message_from_bytes, policy
from unittest import mock

from email_assistant import reply_quote
from email_assistant.models import EmailAccount, EmailMessage
from email_assistant.tests_work_categories import ME, Base


class FakeSMTP:
    """З'єднання SMTP у пам'яті: що пішло на сервер."""
    sent = []

    def sendmail(self, sender, recipients, data):
        FakeSMTP.sent.append(message_from_bytes(data, policy=policy.default))

    def quit(self):
        pass


class ReplyBase(Base):
    def setUp(self):
        super().setUp()
        EmailAccount.objects.filter(pk=self.account.pk).update(is_primary=True)
        self.client.force_login(self.user)
        self.first = self.mail(ME, subject='Production timeline', to=['anduin@hrpcba.example'], folder='Sent',
                               msg_id='<a@sevskiy.example>', body='Dear Anduin, could you send the timeline?')
        self.second = self.mail('anduin@hrpcba.example', subject='Re: Production timeline',
                                msg_id='<b@hrpcba.example>', reply_to='<a@sevskiy.example>',
                                body='Dear Viacheslav, Eva will meet you at Electronica.')
        EmailMessage.objects.filter(pk=self.second.pk).update(
            from_name='Anduin Chen', body_html='<p>Dear Viacheslav,<script>alert(1)</script></p>'
                                               '<img src="x" onerror="alert(2)"><p>Eva will meet you.</p>')
        self.second.refresh_from_db()

    def send(self, **data):
        sent = {}
        self.n = getattr(self, 'n', 0) + 1
        mid = '<reply@sevskiy.example>' if self.n == 1 else f'<reply{self.n}@sevskiy.example>'

        def fake_send(client, **kw):
            sent.update(kw)
            return {'ok': True, 'message_id': mid, 'via': 'test'}
        with mock.patch('email_assistant.smtp_client.SMTPClient.send', fake_send):
            d = self.client.post('/email/send/', {'to': 'anduin@hrpcba.example', 'subject': 'Re: Production timeline',
                                                  'body': 'Thank you for the contacts!',
                                                  'body_html': '<p>Thank you for the contacts!</p>', **data}).json()
        self.assertTrue(d['ok'], d)
        return sent


class QuickReplyQuoteTests(ReplyBase):
    def test_quick_reply_keeps_history_and_thread(self):
        sent = self.send(reply_to_pk=self.second.pk, with_quote='1')
        self.assertEqual(sent['reply_to_message'], self.second)
        text, html = sent['body_text'], sent['body_html']
        self.assertTrue(text.startswith('Thank you for the contacts!'))
        self.assertIn(reply_quote.MARKER, text)
        self.assertIn('From: Anduin Chen <anduin@hrpcba.example>', text)
        self.assertIn('Eva will meet you at Electronica', text)
        self.assertTrue(html.startswith('<p>Thank you for the contacts!</p>'))
        self.assertIn('Eva will meet you.', html)
        self.assertIn('<b>Subject:</b> Re: Production timeline', html)
        self.assertNotIn('<script', html)
        self.assertNotIn('onerror', html)
        copy = EmailMessage.objects.get(message_id='<reply@sevskiy.example>')   # копія в Minerva — як пішов лист
        self.assertEqual((copy.thread_id, copy.in_reply_to), (self.second.thread_id, '<b@hrpcba.example>'))
        self.assertIn(reply_quote.MARKER, copy.body_text)
        from email_assistant.quotes import new_text
        self.assertEqual(new_text(copy.body_text).strip(), 'Thank you for the contacts!')   # AI бачить лише нове

    def test_without_checkbox_or_reply_no_history(self):
        sent = self.send(reply_to_pk=self.second.pk, with_quote='0')
        self.assertNotIn(reply_quote.MARKER, sent['body_text'])
        sent = self.send(with_quote='1')   # новий лист — цитувати нічого
        self.assertNotIn(reply_quote.MARKER, sent['body_text'])

    def test_text_only_reply_gets_html_with_history(self):
        sent = self.send(reply_to_pk=self.second.pk, with_quote='1', body_html='', body='Danke\n2. Zeile')
        self.assertTrue(sent['body_html'].startswith('<div>Danke<br>2. Zeile</div>'))
        self.assertIn('Eva will meet you.', sent['body_html'])


class ReferencesTests(ReplyBase):
    def test_whole_thread_chain(self):
        self.assertEqual(self.second.thread_id, self.first.thread_id)
        third = self.mail(ME, subject='Re: Production timeline', to=['anduin@hrpcba.example'], folder='Sent',
                          msg_id='<c@sevskiy.example>', reply_to='<b@hrpcba.example>')
        third.refresh_from_db()
        self.assertEqual(reply_quote.references(third), '<a@sevskiy.example> <b@hrpcba.example> <c@sevskiy.example>')
        self.assertEqual(reply_quote.references(self.second), '<a@sevskiy.example> <b@hrpcba.example>')
        EmailMessage.objects.filter(pk=third.pk).update(message_id='')
        third.refresh_from_db()
        self.assertEqual(reply_quote.references(third), '')

    def test_smtp_headers_and_signature_before_history(self):
        FakeSMTP.sent = []
        self.account.signature = 'Dr. Viacheslav Pryimak'
        self.account.save()
        text, html = reply_quote.with_quote(self.second, 'Thank you!', '<p>Thank you!</p>')
        from email_assistant.smtp_client import SMTPClient
        with mock.patch.object(SMTPClient, '_open', return_value=FakeSMTP()), \
                mock.patch('core.smtp_utf8.smtp_login'), \
                mock.patch('email_assistant.imap_client.IMAPClient', side_effect=OSError('no imap')):
            r = SMTPClient(self.account).send(to_emails=['anduin@hrpcba.example'], subject='Re: Production timeline',
                                              body_text=text, body_html=html, reply_to_message=self.second)
        self.assertTrue(r['ok'], r)
        msg = FakeSMTP.sent[0]
        self.assertEqual(msg['In-Reply-To'], '<b@hrpcba.example>')
        self.assertEqual(' '.join(str(msg['References']).split()), '<a@sevskiy.example> <b@hrpcba.example>')
        plain = msg.get_body(preferencelist=('plain',)).get_content()
        self.assertLess(plain.index('Dr. Viacheslav Pryimak'), plain.index(reply_quote.MARKER))


class ReplyButtonsTests(ReplyBase):
    def test_preview_has_history_checkbox_and_reply_button(self):
        r = self.client.get(f'/email/message/{self.second.pk}/preview/')
        self.assertContains(r, 'id="pv-reply-quote" checked')
        self.assertContains(r, f'onclick="pvReplyFull({self.second.pk})"')
        self.assertContains(r, "fd.append('with_quote'")

    def test_compose_reply_with_ai_text(self):
        r = self.client.get('/email/compose/', {'reply_to': self.second.pk, 'body': 'Thank you <b>Anduin</b>!\nBye',
                                                'to': 'eva@hrpcba.example'})
        self.assertContains(r, 'Thank you &lt;b&gt;Anduin&lt;/b&gt;!<br>Bye')   # текст AI — як текст, не HTML
        self.assertContains(r, 'value="eva@hrpcba.example"')
        self.assertContains(r, f'name="reply_to_pk" value="{self.second.pk}"')
        self.assertContains(r, 'Оригінальний лист від anduin@hrpcba.example')   # історія листування — у формі
