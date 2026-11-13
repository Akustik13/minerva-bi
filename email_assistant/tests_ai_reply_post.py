"""«Генерувати» AI-відповідь з довгою інструкцією: інструкція йде тілом POST, а не в адресі (рядок запиту понад
4094 байти gunicorn відкидав 400 не-JSON → «⚠️ Помилка»). python manage.py test email_assistant
--settings=tabele.settings_test"""
from unittest import mock
from urllib.parse import quote

from django.test import Client

from email_assistant import views
from email_assistant.models import EmailAccount
from email_assistant.tests_work_categories import Base

LONG = ('подякуй за відповідь, скажи що все зрозуміло. і скажи що частину функцій уже реалізували, що наш бікон має '
        'CTE functionality, також є сенсор положення — протестували, він детектує потрібний нахил; керування '
        'датчиком, можна міняти потужність, період випромінювання тощо, контроль батареї, температура та інше. ' * 3)
GUNICORN_LINE_MAX = 4094


class AiReplyPostTests(Base):
    def setUp(self):
        super().setUp()
        EmailAccount.objects.filter(pk=self.account.pk).update(is_primary=True)
        self.client.force_login(self.user)
        self.msg = self.mail('pieter@customer.example', subject='Beacon project', body='Dear Viacheslav, thanks!')

    def call(self, client=None, method='post', **data):
        with mock.patch('email_assistant.ai_helper.generate_reply', return_value='Dear Pieter, thank you!') as gen:
            c = client or self.client
            url = f'/email/message/{self.msg.pk}/ai-reply/'
            r = c.post(url, data) if method == 'post' else c.get(url, data)
        return r, gen

    def test_long_instruction_in_post_body(self):
        line = f'GET /email/message/{self.msg.pk}/ai-reply/?prompt={quote(LONG, safe="")} HTTP/1.1'
        self.assertGreater(len(line.encode()), GUNICORN_LINE_MAX)   # саме такий запит сервер відкидав
        r, gen = self.call(prompt=LONG)
        d = r.json()
        self.assertTrue(d['ok'], d)
        self.assertEqual(d['reply'], 'Dear Pieter, thank you!')
        self.assertEqual(gen.call_args.kwargs['instruction'], LONG)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.ai_reply_draft, 'Dear Pieter, thank you!')

    def test_old_get_still_works_and_long_text_is_cut(self):
        r, gen = self.call(method='get', prompt='коротко')
        self.assertEqual(gen.call_args.kwargs['instruction'], 'коротко')
        r, gen = self.call(prompt='x' * (views.AI_PROMPT_MAX + 500))
        self.assertEqual(len(gen.call_args.kwargs['instruction']), views.AI_PROMPT_MAX)

    def test_post_needs_csrf(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        r, gen = self.call(client=strict, prompt='hi')
        self.assertEqual(r.status_code, 403)
        gen.assert_not_called()

    def test_pages_send_instruction_by_post(self):
        for url in (f'/email/message/{self.msg.pk}/preview/', f'/email/thread/{self.msg.thread_id}/'):
            html = self.client.get(url).content.decode()
            self.assertNotIn('?prompt=', html, url)
            self.assertIn("fd.append('prompt'", html, url)
