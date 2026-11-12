"""🖼 Розмір картинки в тексті листа (static/admin/js/mv_img_resize.js): скрипт є на сторінках листа, розмір
доходить до листа й чернетки. python manage.py test email_assistant --settings=tabele.settings_test"""
from pathlib import Path

from django.conf import settings

from email_assistant.html_clean import clean_html
from email_assistant.models import EmailAccount
from email_assistant.tests_work_categories import Base

SCRIPT = '/static/admin/js/mv_img_resize.js'
SIZED = ('<p>See:</p><img src="data:image/png;base64,iVBORw0KGgo=" width="411" height="231" '
         'style="width: 411px; height: auto; max-width: 100%;">')


class ImageResizeTests(Base):
    def setUp(self):
        super().setUp()
        EmailAccount.objects.filter(pk=self.account.pk).update(is_primary=True)
        self.client.force_login(self.user)
        self.msg = self.mail('anduin@hrpcba.example', subject='Production timeline')

    def test_script_on_every_mail_editor_page(self):
        self.assertTrue((Path(settings.BASE_DIR) / SCRIPT.lstrip('/')).is_file())
        for url in ('/email/', '/email/compose/', f'/email/compose/?reply_to={self.msg.pk}',
                    f'/email/thread/{self.msg.thread_id}/'):
            self.assertContains(self.client.get(url), SCRIPT, msg_prefix=url)

    def test_size_survives_draft_cleaning(self):
        html = clean_html(SIZED)
        self.assertIn('width="411"', html)
        self.assertIn('height="231"', html)
        self.assertIn('width:411px', html)
        self.assertIn('src="data:image/png;base64,', html)

    def test_size_reaches_sent_letter(self):
        from unittest import mock
        sent = {}

        def fake_send(client, **kw):
            sent.update(kw)
            return {'ok': True, 'message_id': '<img1@sevskiy.example>', 'via': 'test'}
        with mock.patch('email_assistant.smtp_client.SMTPClient.send', fake_send):
            d = self.client.post('/email/send/', {'to': 'anduin@hrpcba.example', 'subject': 'Screenshot',
                                                  'body': 'See:', 'body_html': SIZED}).json()
        self.assertTrue(d['ok'], d)
        self.assertIn('width="411" height="231"', sent['body_html'])
