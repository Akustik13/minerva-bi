"""Логотипи відправників (logos.py): python manage.py test email_assistant --settings=tabele.settings_test
(мережа імітується)."""
import os
import shutil
import tempfile
from unittest import mock

from django.test import override_settings

from email_assistant import logos
from email_assistant.models import EmailAccount, EmailSettings
from email_assistant.tests_work_categories import Base

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 40


def public(host, *a, **k):
    ip = '192.168.2.10' if host.startswith('intranet') else '93.184.216.34'
    return [(2, 1, 6, '', (ip, 443))]


class LogoTests(Base):
    def setUp(self):
        super().setUp()
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        patcher = mock.patch('email_assistant.logos.socket.getaddrinfo', side_effect=public)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_domain_for(self):
        self.assertEqual(logos.domain_for('Orders@EU.DigiKey.com'), 'digikey.com')
        self.assertEqual(logos.domain_for('a@shop.example.co.uk'), 'example.co.uk')
        self.assertEqual(logos.domain_for('kunde@gmail.com'), '')       # поштовий сервіс — без логотипа
        self.assertEqual(logos.domain_for('no-address'), '')

    def test_icon_urls_and_kind(self):
        html = ('<link rel="icon" href="/fav-32.png" sizes="32x32"><link rel="mask-icon" href="/m.svg">'
                '<link rel="icon" type="image/svg+xml" href="/i.svg"><link rel="apple-touch-icon" href="/touch.png">'
                '<link rel="icon" sizes="192x192" href="https://cdn.example.com/192.png">')
        self.assertEqual(logos.icon_urls('https://example.com/', html),
                         ['https://example.com/touch.png', 'https://cdn.example.com/192.png',
                          'https://example.com/fav-32.png'])
        self.assertEqual(logos._kind(PNG), 'png')
        self.assertEqual(logos._kind(b'<svg xmlns="http://www.w3.org/2000/svg"/>'), '')

    def test_private_network_is_refused(self):
        with mock.patch('requests.get') as get:
            self.assertIsNone(logos._get('https://intranet.example.com/', 1000))
        get.assert_not_called()

    def test_fetch_saves_logo_or_marks_missing(self):
        pages = {'https://acme.example/': ('https://acme.example/', b'<link rel="apple-touch-icon" href="/t.png">'),
                 'https://acme.example/t.png': ('https://acme.example/t.png', PNG)}
        with mock.patch('email_assistant.logos._get', side_effect=lambda url, limit: pages.get(url)):
            self.assertTrue(logos.fetch('acme.example'))
        path, ctype = logos.cached('acme.example')
        self.assertEqual(ctype, 'image/png')
        with mock.patch('email_assistant.logos._get', return_value=None):
            self.assertFalse(logos.fetch('nologo.example'))
        self.assertIsNone(logos.cached('nologo.example'))
        with mock.patch('email_assistant.logos.threading.Thread') as thread:
            logos.start('nologo.example')   # нещодавно шукали — без повтору
            logos.start('acme.example')     # уже є
        thread.assert_not_called()

    def test_view_and_inbox(self):
        EmailAccount.objects.filter(pk=self.account.pk).update(is_primary=True)
        self.client.force_login(self.user)
        with mock.patch('email_assistant.logos.start') as start:
            self.assertEqual(self.client.get('/email/logo/acme.example/').status_code, 404)
        start.assert_called_once_with('acme.example')
        os.makedirs(os.path.join(self.media, logos.DIR), exist_ok=True)
        with open(logos._base('acme.example') + '.png', 'wb') as f:
            f.write(PNG)
        r = self.client.get('/email/logo/acme.example/')
        self.assertEqual((r.status_code, r['Content-Type']), (200, 'image/png'))
        self.assertEqual(self.client.get('/email/logo/gmail.com/').status_code, 404)
        self.mail('sales@acme.example', subject='Angebot')
        self.mail('kunde@gmail.com', subject='Frage')
        page = self.client.get('/email/')
        self.assertContains(page, 'src="/email/logo/acme.example/"')
        self.assertNotContains(page, '/email/logo/gmail.com/')
        es = EmailSettings.get_for_user(self.user)
        es.sender_logos = False
        es.save()
        self.assertNotContains(self.client.get('/email/'), 'class="em-av-logo"')
        self.assertEqual(self.client.get('/email/logo/acme.example/').status_code, 404)
