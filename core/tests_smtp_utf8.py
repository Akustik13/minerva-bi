"""Пошта з паролем не лише з латиниці («§», «ä», кирилиця) — core/smtp_utf8.py: SMTP AUTH PLAIN / LOGIN у UTF-8,
IMAP AUTHENTICATE PLAIN, Django-бекенд; «🔌 Перевірити з'єднання» в налаштуваннях сповіщень. Сервер підмінено.

python manage.py test core.tests_smtp_utf8 --settings=tabele.settings_test
"""
import base64
import json
import smtplib
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from core import smtp_utf8

PASSWORD = 'Hg&3§xY9S4%3'


class FakeConn:
    """Те, що бачить вхід: EHLO з AUTH, docmd — відповіді сервера."""

    def __init__(self, auth='PLAIN LOGIN', codes=None):
        self.esmtp_features = {'auth': auth}
        self.sent, self.codes, self.logged = [], list(codes or [(235, b'ok')]), None

    def ehlo_or_helo_if_needed(self):
        pass

    def has_extn(self, name):
        return name in self.esmtp_features

    def docmd(self, cmd, args=''):
        self.sent.append((cmd, args))
        return self.codes.pop(0) if self.codes else (235, b'ok')

    def login(self, user, password):
        self.logged = (user, password)
        return 235, b'ok'


class LoginTests(TestCase):
    def test_plain_utf8(self):
        conn = FakeConn()
        smtp_utf8.smtp_login(conn, 'shop@sevskiy.de', PASSWORD)
        cmd, args = conn.sent[0]
        self.assertEqual(cmd, 'AUTH')
        self.assertTrue(args.startswith('PLAIN '))
        self.assertEqual(base64.b64decode(args[6:]).decode('utf-8'), f'\0shop@sevskiy.de\0{PASSWORD}')
        self.assertIsNone(conn.logged)

    def test_login_mechanism_and_errors(self):
        conn = FakeConn(auth='LOGIN', codes=[(334, b'UGFzc3dvcmQ6'), (235, b'ok')])
        smtp_utf8.smtp_login(conn, 'shop@sevskiy.de', PASSWORD)
        self.assertEqual(base64.b64decode(conn.sent[1][0]).decode('utf-8'), PASSWORD)
        with self.assertRaises(smtplib.SMTPAuthenticationError):
            smtp_utf8.smtp_login(FakeConn(codes=[(535, b'bad')]), 'shop@sevskiy.de', PASSWORD)
        with self.assertRaises(smtplib.SMTPException):
            smtp_utf8.smtp_login(FakeConn(auth='CRAM-MD5'), 'shop@sevskiy.de', PASSWORD)

    def test_ascii_uses_standard_login(self):
        conn = FakeConn()
        smtp_utf8.smtp_login(conn, 'shop@sevskiy.de', 'plain-ascii')
        self.assertEqual((conn.logged, conn.sent), (('shop@sevskiy.de', 'plain-ascii'), []))

    def test_stdlib_fails_ours_does_not(self):
        conn = smtp_utf8.SMTP()                                     # без підключення: лише вхід
        conn.ehlo_or_helo_if_needed = lambda: None
        conn.esmtp_features = {'auth': 'PLAIN'}
        conn.does_esmtp = True
        conn.docmd = mock.Mock(return_value=(235, b'ok'))
        with self.assertRaises(UnicodeEncodeError):                 # те, що бачив користувач
            smtplib.SMTP.login(conn, 'shop@sevskiy.de', PASSWORD)
        self.assertEqual(conn.login('shop@sevskiy.de', PASSWORD), (235, b'ok'))
        from django.core.mail import get_connection
        backend = get_connection(backend=smtp_utf8.BACKEND, host='x', port=465, use_ssl=True)
        self.assertIs(backend.connection_class, smtp_utf8.SMTP_SSL)

    def test_imap(self):
        conn = mock.Mock()
        smtp_utf8.imap_login(conn, 'shop@sevskiy.de', PASSWORD)
        mech, auth = conn.authenticate.call_args.args
        self.assertEqual((mech, auth(b'')), ('PLAIN', f'\0shop@sevskiy.de\0{PASSWORD}'.encode('utf-8')))
        smtp_utf8.imap_login(conn, 'u', 'ascii')
        conn.login.assert_called_once_with('u', 'ascii')


class CheckTests(TestCase):
    def fake_server(self, login_error=None):
        conn = FakeConn(codes=[(535, b'Authentication credentials invalid')] if login_error else None)
        conn.starttls = mock.Mock()
        conn.quit = mock.Mock()
        return mock.Mock(return_value=conn)

    def test_check_ok_and_auth_error(self):
        with mock.patch.object(smtp_utf8, 'SMTP_SSL', self.fake_server()):
            out = smtp_utf8.check_smtp('smtp.ionos.de', 465, use_ssl=True, user='shop@sevskiy.de', password=PASSWORD)
        self.assertTrue(out['ok'], out)
        self.assertIn('Вхід як shop@sevskiy.de — успішно (пароль не лише з латиниці — вхід у UTF-8)', out['steps'][-1][1])
        with mock.patch.object(smtp_utf8, 'SMTP_SSL', self.fake_server(login_error=True)):
            out = smtp_utf8.check_smtp('smtp.ionos.de', 465, use_ssl=True, user='shop@sevskiy.de',
                                       password=PASSWORD + '0')
        self.assertFalse(out['ok'])
        self.assertIn('Логін або пароль не підходить', out['hint'])
        self.assertFalse(smtp_utf8.check_smtp('', 465)['ok'])

    def test_admin_button_uses_saved_password(self):
        from config.models import NotificationSettings, SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        ns = NotificationSettings.get()
        ns.email_host_password = PASSWORD
        ns.save()
        self.client.force_login(User.objects.create_superuser('boss', 'b@x.y', 'p'))
        page = self.client.get(f'/admin/config/notificationsettings/{ns.pk}/change/').content.decode()
        self.assertIn("🔌 Перевірити з'єднання", page)
        url = f'/admin/config/notificationsettings/{ns.pk}/smtp-check/'
        with mock.patch('core.smtp_utf8.check_smtp', return_value={'ok': True, 'steps': [], 'hint': ''}) as check:
            d = self.client.post(url, json.dumps({'host': 'smtp.ionos.de', 'port': '465', 'ssl': True,
                                                  'user': 'shop@sevskiy.de', 'password': ''}),
                                 content_type='application/json').json()
        self.assertTrue(d['ok'])
        self.assertEqual(check.call_args.kwargs['password'], PASSWORD)      # порожнє — збережений
        self.assertTrue(check.call_args.kwargs['use_ssl'])
        self.assertEqual(self.client.get(url).status_code, 403)
