"""🎨 Вигляд пошти (layout.py), посилання й скрипти в листі, mailto: → новий лист, назви вкладень у списку:
python manage.py test email_assistant.tests_layout --settings=tabele.settings_test"""
import json

from email_assistant import layout
from email_assistant.models import EmailAccount, EmailMessage, EmailSettings
from email_assistant.tests_work_categories import Base


class LayoutTests(Base):
    def setUp(self):
        super().setUp()
        EmailAccount.objects.filter(pk=self.account.pk).update(is_primary=True)
        self.client.force_login(self.user)

    def post(self, data):
        return self.client.post('/email/layout/', json.dumps(data), content_type='application/json')

    def test_presets_options_and_custom(self):
        es = EmailSettings.get_for_user(self.user)
        self.assertEqual(layout.get(es), layout.DEFAULT)
        self.assertEqual(layout.preset_of(layout.get(es)), 'minerva')
        for key, _label, _hint, values in layout.PRESETS:       # кожен шаблон — лише відомі значення, усі перемикачі
            self.assertEqual(set(values), set(layout.OPTIONS), key)
            self.assertTrue(all(values[k] in layout.OPTIONS[k] for k in values), key)
        d = self.post({'preset': 'gmail'}).json()
        self.assertEqual((d['ok'], d['preset'], d['layout']['pane']), (True, 'gmail', 'off'))
        self.assertIn('lay-pane-off', d['classes'])
        d = self.post({'att': 'top'}).json()                    # один перемикач — шаблон стає «Свій»
        self.assertEqual((d['preset'], d['layout']['att'], d['layout']['pane']), ('custom', 'top', 'off'))
        self.assertEqual(self.post({'pane': 'sideways'}).status_code, 400)
        self.assertEqual(self.post({'preset': 'nope'}).status_code, 400)
        es.refresh_from_db()
        es.mail_layout['density'] = 'huge'                      # зіпсоване збережене — відкидається
        self.assertEqual(layout.get(es)['density'], 'normal')
        self.assertEqual(self.post({'reset': True}).json()['preset'], 'minerva')

    def test_inbox_gets_classes_dialog_and_chips(self):
        self.post({'preset': 'thunderbird'})
        msg = self.mail('sales@acme.example', subject='Rechnung')
        EmailMessage.objects.filter(pk=msg.pk).update(attachments=[
            {'name': 'Rechnung_4711.pdf', 'content_type': 'application/pdf', 'size': 1200},
            {'name': 'logo.png', 'content_type': 'image/png', 'content_id': 'logo@x', 'is_inline': True}])
        page = self.client.get('/email/').content.decode()
        self.assertIn('lay-pane-below lay-density-compact', page)
        self.assertIn('id="em-layout-data"', page)
        self.assertIn('id="em-look-dlg"', page)
        self.assertIn('id="em-keys-dlg"', page)
        self.assertIn('id="em-drag-v"', page)
        self.assertIn(f'href="/email/message/{msg.pk}/attachment/0/"', page)   # назва вкладення в рядку
        self.assertIn('📎 Rechnung_4711.pdf', page)
        self.assertNotIn('📎 logo.png', page)                                    # картинка в тексті — ні

    def test_letter_html_scripts_blocked_links_open_outside(self):
        msg = self.mail('sales@acme.example', subject='Link')
        EmailMessage.objects.filter(pk=msg.pk).update(
            body_html='<html><head><title>x</title></head><body><a href="https://acme.example/">site</a>'
                      '<script>alert(1)</script></body></html>')
        r = self.client.get(f'/email/message/{msg.pk}/html/')
        self.assertIn("script-src 'none'", r['Content-Security-Policy'])
        self.assertEqual(r['Referrer-Policy'], 'no-referrer')
        html = r.content.decode()
        self.assertIn('<head><base target="_blank">', html)
        EmailMessage.objects.filter(pk=msg.pk).update(body_html='', body_text='Siehe https://acme.example/info bitte')
        r = self.client.get(f'/email/message/{msg.pk}/html/')
        self.assertIn('<a href="https://acme.example/info" rel="nofollow">', r.content.decode())
        self.assertIn("script-src 'none'", r['Content-Security-Policy'])
        thread = EmailMessage.objects.get(pk=msg.pk).thread
        preview = self.client.get(f'/email/thread/{thread.pk}/preview/').content.decode()
        self.assertIn('href="https://acme.example/info"', preview)               # текст без HTML — теж посилання

    def test_mailto_opens_compose_prefilled(self):
        page = self.client.get('/email/compose/', {'to': 'max@acme.example', 'subject': 'Anfrage',
                                                   'body': 'Hallo\nTest <b>'}).content.decode()
        self.assertIn('value="max@acme.example"', page)
        self.assertIn('value="Anfrage"', page)
        self.assertIn('Hallo<br>Test &lt;b&gt;', page)
