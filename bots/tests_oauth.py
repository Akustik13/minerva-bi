"""🔑 Авторизація DigiKey Marketplace (3-legged OAuth): state у сесії, лише людина з правом змінювати налаштування
DigiKey (bots/oauth.py, bots.views.digikey_oauth_callback). DigiKey не викликається — обмін коду підмінено.
python manage.py test bots --settings=tabele.settings_test"""
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.models import User
from django.test import TestCase

from bots import oauth
from bots.models import DigiKeyConfig

START = "/admin/bots/digikeyconfig/oauth-start/"
CALLBACK = "/bots/digikey/oauth-callback/"
CONFIG = "/admin/bots/digikeyconfig/1/change/"


class OAuthStateTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        DigiKeyConfig.objects.update_or_create(pk=1, defaults={"client_id": "cid",
                                                               "public_base_url": "https://minerva.example"})
        DigiKeyConfig.objects.filter(pk=1).update(marketplace_access_token="old-access",
                                                  marketplace_refresh_token="old-refresh")
        self.admin = User.objects.create_superuser("adm", "adm@example.com", "p")
        exchange = mock.patch("bots.services.digikey.exchange_code_for_tokens",
                              return_value={"access_token": "new-access", "refresh_token": "new-refresh",
                                            "expires_in": 1800})
        self.exchange = exchange.start()
        self.addCleanup(exchange.stop)

    def tokens(self):
        c = DigiKeyConfig.objects.get(pk=1)
        return c.marketplace_access_token, c.marketplace_refresh_token

    def start(self):
        r = self.client.get(START)
        self.assertEqual(r.status_code, 302)
        url = urlparse(r["Location"])
        self.assertEqual(url.netloc, "api.digikey.com")
        query = parse_qs(url.query)
        self.assertEqual(query["redirect_uri"], ["https://minerva.example/bots/digikey/oauth-callback/"])
        return query["state"][0]

    def test_start_sends_state_and_callback_accepts_it_once(self):
        self.client.force_login(self.admin)
        state = self.start()
        self.assertGreaterEqual(len(state), 40)
        self.assertEqual(self.client.session[oauth.SESSION_KEY]["value"], state)
        r = self.client.get(CALLBACK, {"code": "c1", "state": state})
        self.assertRedirects(r, CONFIG, fetch_redirect_response=False)
        self.exchange.assert_called_once()
        self.assertEqual(self.exchange.call_args[0][1:], ("c1", "https://minerva.example/bots/digikey/oauth-callback/"))
        self.assertEqual(self.tokens(), ("new-access", "new-refresh"))
        self.assertNotIn(oauth.SESSION_KEY, self.client.session)
        r = self.client.get(CALLBACK, {"code": "c2", "state": state})                # той самий state удруге — ні
        self.assertContains(r, "Авторизацію DigiKey не прийнято", status_code=400)
        self.assertEqual(self.exchange.call_count, 1)

    def test_forged_callback_without_state_does_not_touch_tokens(self):
        self.client.force_login(self.admin)
        r = self.client.get(CALLBACK, {"code": "attacker"})                          # авторизацію не починали
        self.assertContains(r, "не починала авторизацію", status_code=400)
        self.start()
        r = self.client.get(CALLBACK, {"code": "attacker", "state": "guess"})
        self.assertContains(r, "Токени DigiKey Marketplace у Minerva не змінено", status_code=400)
        self.exchange.assert_not_called()
        self.assertEqual(self.tokens(), ("old-access", "old-refresh"))

    def test_expired_state(self):
        self.client.force_login(self.admin)
        state = self.start()
        session = self.client.session
        session[oauth.SESSION_KEY] = {"value": state, "expires": 0}
        session.save()
        r = self.client.get(CALLBACK, {"code": "c1", "state": state})
        self.assertContains(r, "15 хвилин", status_code=400)
        self.exchange.assert_not_called()

    def test_anonymous_callback_refused(self):
        r = self.client.get(CALLBACK, {"code": "attacker", "state": "x"})
        self.assertContains(r, "Увійти в Minerva", status_code=403)
        self.exchange.assert_not_called()
        self.assertEqual(self.tokens(), ("old-access", "old-refresh"))

    def test_user_without_change_permission_cannot_authorize(self):
        u = User.objects.create_user("v", "v@example.com", "p", is_staff=True)
        u.profile.role = "manager"            # модуль bots відкрито, операції — лише перегляд
        u.profile.allowed_modules = ["bots"]
        u.profile.module_operations = {"bots": ["view"]}
        u.profile.save()
        self.assertFalse(oauth.can_authorize(u))
        self.assertTrue(oauth.can_authorize(self.admin))
        self.client.force_login(u)
        r = self.client.get(START)
        self.assertRedirects(r, CONFIG, fetch_redirect_response=False)               # не на DigiKey
        self.assertNotIn(oauth.SESSION_KEY, self.client.session)
        session = self.client.session
        oauth.new_state(session)
        session.save()
        state = session[oauth.SESSION_KEY]["value"]
        r = self.client.get(CALLBACK, {"code": "c1", "state": state})
        self.assertContains(r, "право змінювати", status_code=403)
        self.exchange.assert_not_called()

    def test_error_from_digikey_shows_message(self):
        self.client.force_login(self.admin)
        self.start()
        r = self.client.get(CALLBACK, {"error": "access_denied"})
        self.assertRedirects(r, CONFIG, fetch_redirect_response=False)
        self.assertIn("access_denied", self.client.session["digikey_oauth_error"])
        self.assertNotIn(oauth.SESSION_KEY, self.client.session)
        self.exchange.assert_not_called()
