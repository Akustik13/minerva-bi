"""🖥 «Пошта Minerva» для Windows (email_assistant/desktop.py, views_desktop.py): режим «лише пошта» за User-Agent,
підключення програми одноразовим кодом, сесія програми не закінчується, поки програмою користуються:
python manage.py test email_assistant.tests_desktop --settings=tabele.settings_test"""
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from core.shared_cache import cache
from email_assistant import desktop
from email_assistant.models import EmailAccount
from email_assistant.tests_work_categories import Base

APP_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36 MinervaMail/1.0.0'
BROWSER_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36'


class DesktopTests(Base):
    def setUp(self):
        super().setUp()
        cache.clear()
        EmailAccount.objects.filter(pk=self.account.pk).update(is_primary=True)

    def code_for(self, user=None):
        self.client.force_login(user or self.user)
        d = self.client.post('/email/app/pair/new/').json()
        self.client.logout()
        return d

    def test_user_agent(self):
        rf = RequestFactory()
        self.assertEqual(desktop.app_version(rf.get('/', HTTP_USER_AGENT=APP_UA)), '1.0.0')
        self.assertTrue(desktop.is_app(rf.get('/', HTTP_USER_AGENT=APP_UA)))
        self.assertFalse(desktop.is_app(rf.get('/', HTTP_USER_AGENT=BROWSER_UA)))
        self.assertFalse(desktop.is_app(rf.get('/')))

    def test_app_mode_hides_admin_sidebar(self):
        self.client.force_login(self.user)
        r = self.client.get('/email/', HTTP_USER_AGENT=APP_UA)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context['mv_mail_app'])
        self.assertFalse(r.context['is_nav_sidebar_enabled'])
        self.assertNotContains(r, 'href="/email/app/"')        # «🖥 Пошта для Windows» — лише в браузері
        r = self.client.get('/email/', HTTP_USER_AGENT=BROWSER_UA)
        self.assertFalse(r.context['mv_mail_app'])
        self.assertContains(r, 'href="/email/app/"')
        r = self.client.get('/email/sort/', HTTP_USER_AGENT=APP_UA)
        self.assertFalse(r.context['is_nav_sidebar_enabled'])

    def test_hello_is_public_and_says_nothing_about_people(self):
        r = self.client.get('/email/app/hello/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {'minerva': 'mail', 'v': 1, 'pair': True})
        self.assertEqual(self.client.post('/email/app/hello/').status_code, 405)

    def test_desktop_page(self):
        self.assertEqual(self.client.get('/email/app/').status_code, 302)          # без входу — сторінка входу
        self.client.force_login(self.user)
        r = self.client.get('/email/app/')
        self.assertContains(r, desktop.APP_URL)
        self.assertContains(r, 'id="dk-pair"')
        r = self.client.get('/email/app/', HTTP_USER_AGENT=APP_UA)
        self.assertNotContains(r, 'id="dk-pair"')

    def test_pair_new_needs_staff_and_post(self):
        self.assertEqual(self.client.post('/email/app/pair/new/').status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/email/app/pair/new/').status_code, 405)
        d = self.client.post('/email/app/pair/new/').json()
        self.assertTrue(d['ok'])
        link = urlparse(d['link'])
        self.assertEqual((link.scheme, link.netloc), ('minerva-mail', 'pair'))
        q = parse_qs(link.query)
        self.assertEqual(q['url'], ['http://testserver'])
        self.assertEqual(q['code'], [d['code']])
        self.assertEqual(len(d['key']), 16)

    def test_pair_confirm_then_login_once(self):
        d = self.code_for()
        r = self.client.get('/email/app/pair/', {'code': d['code']}, HTTP_USER_AGENT=APP_UA)
        self.assertContains(r, 'anna')                                             # чий акаунт — до підтвердження
        self.assertContains(r, 'name="code"')
        self.assertNotIn('_auth_user_id', self.client.session)                    # GET нічого не робить
        r = self.client.post('/email/app/pair/', {'code': d['code']}, HTTP_USER_AGENT=APP_UA)
        self.assertRedirects(r, '/email/', fetch_redirect_response=False)
        self.assertEqual(int(self.client.session['_auth_user_id']), self.user.pk)
        self.assertGreater(self.client.session.get_expiry_age(), 29 * 86400)      # сесія програми — 30 днів
        self.client.logout()
        r = self.client.post('/email/app/pair/', {'code': d['code']}, HTTP_USER_AGENT=APP_UA)
        self.assertEqual(r.status_code, 410)                                       # лише один раз
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(self.client.get('/email/app/pair/', {'code': d['code']}).status_code, 410)
        self.assertEqual(self.client.get('/email/app/pair/', {'code': 'nope'}).status_code, 410)

    def test_pair_state_only_for_owner(self):
        d = self.code_for()
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/email/app/pair/state/', {'key': d['key']}).json(), {'paired': False})
        self.client.logout()
        self.client.post('/email/app/pair/', {'code': d['code']}, HTTP_USER_AGENT=APP_UA)
        self.assertEqual(self.client.get('/email/app/pair/state/', {'key': d['key']}).json(),
                         {'paired': True, 'device': '1.0.0'})
        other = get_user_model().objects.create_user('bob', 'bob@x.example', 'x', is_staff=True)
        self.client.force_login(other)
        self.assertFalse(self.client.get('/email/app/pair/state/', {'key': d['key']}).json()['paired'])
        self.assertTrue(self.client.get('/email/app/pair/state/', {'key': 'zz'}).json()['expired'])

    def test_disabled_user_code_does_not_log_in(self):
        d = self.code_for()
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.client.get('/email/app/pair/', {'code': d['code']}).status_code, 410)
        r = self.client.post('/email/app/pair/', {'code': d['code']})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_too_many_attempts(self):
        for _ in range(desktop.PAIR_PER_HOUR):
            self.client.post('/email/app/pair/', {'code': 'wrong'})
        d = self.code_for()
        self.assertEqual(self.client.post('/email/app/pair/', {'code': d['code']}).status_code, 429)

    def test_app_session_is_kept_alive_browser_is_not(self):
        self.client.force_login(self.user)
        self.client.get('/email/unread-count/', HTTP_USER_AGENT=BROWSER_UA)
        self.assertLess(self.client.session.get_expiry_age(), 15 * 86400)         # браузер — як завжди
        r = self.client.get('/email/unread-count/', HTTP_USER_AGENT=APP_UA)
        self.assertEqual(r.status_code, 200)
        self.assertIn('latest', r.json())
        self.assertGreater(self.client.session.get_expiry_age(), 29 * 86400)


class NewMessagesTests(Base):
    def setUp(self):
        super().setUp()
        EmailAccount.objects.filter(pk=self.account.pk).update(is_primary=True)
        self.client.force_login(self.user)

    def test_new_after_lists_unread_inbox_only(self):
        from email_assistant.models import EmailMessage
        old = self.mail('a@x.example', subject='Alt')
        new = self.mail('Bob <bob@x.example>', subject='Angebot bitte')
        read = self.mail('c@x.example', subject='Gelesen')
        spam = self.mail('d@x.example', subject='Spam')
        EmailMessage.objects.filter(pk__in=[old.pk, new.pk, spam.pk]).update(is_read=False)
        EmailMessage.objects.filter(pk=spam.pk).update(is_spam=True)
        d = self.client.get('/email/unread-count/', {'new_after': old.pk}, HTTP_USER_AGENT=APP_UA).json()
        self.assertEqual([m['pk'] for m in d['new']], [new.pk])
        self.assertEqual(d['new'][0]['subject'], 'Angebot bitte')
        self.assertNotIn(read.pk, [m['pk'] for m in d['new']])
        self.assertNotIn('new', self.client.get('/email/unread-count/').json())   # сторінки Minerva — як раніше


class KeepSessionTests(TestCase):
    def test_anonymous_and_browser_untouched(self):
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.sessions.backends.db import SessionStore
        rf = RequestFactory()
        req = rf.get('/', HTTP_USER_AGENT=APP_UA)
        req.user, req.session = AnonymousUser(), SessionStore()
        self.assertFalse(desktop.keep_session(req))
        user = get_user_model().objects.create_user('c', 'c@x.example', 'x', is_staff=True)
        req = rf.get('/', HTTP_USER_AGENT=BROWSER_UA)
        req.user, req.session = user, SessionStore()
        self.assertFalse(desktop.keep_session(req))
        req = rf.get('/', HTTP_USER_AGENT=APP_UA)
        req.user, req.session = user, SessionStore()
        self.assertTrue(desktop.keep_session(req))
        self.assertFalse(desktop.keep_session(req))                                # уже продовжено — не щоразу
