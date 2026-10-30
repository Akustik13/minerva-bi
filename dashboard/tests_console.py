"""Консоль API: лише адміністратор Minerva, ключі не в сторінці, проксі — лише API цієї Minerva без мережі.

    python manage.py test dashboard --settings=tabele.settings_test
"""
import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from api.models import APIKey
from dashboard.api_views import own_api_target


def person(username, role="manager", superuser=False):
    user = User.objects.create_user(username, f"{username}@x.y", "p", is_staff=True, is_superuser=superuser)
    user.profile.role = role
    user.profile.save()
    return User.objects.get(pk=user.pk)


class ConsoleTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.key = APIKey.objects.create(name='Shop <b>"x"</b>', scopes=["products:read"])
        self.admin = person("admin1", role="admin")
        self.staff = person("staff1", role="manager")

    def proxy(self, payload):
        return self.client.post("/dashboard/api/proxy/", json.dumps(payload), content_type="application/json")

    def test_staff_without_admin_role_is_refused(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/dashboard/api/console/").status_code, 403)
        self.assertEqual(self.proxy({"url": "/api/v1/ping/"}).status_code, 403)
        r = self.client.get("/dashboard/api/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "/dashboard/api/console/")

    def test_page_has_no_full_key_or_digikey_secret(self):
        from bots.models import DigiKeyConfig
        DigiKeyConfig.objects.update_or_create(pk=1, defaults={"client_id": "dk-id", "client_secret": "DK-SECRET-777"})
        self.client.force_login(self.admin)
        r = self.client.get("/dashboard/api/console/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertNotIn(self.key.key, html)
        self.assertNotIn("DK-SECRET-777", html)
        self.assertIn('id="con-keys"', html)                 # json_script
        self.assertNotIn('<b>"x"</b>', html)                 # назва ключа — дані, не розмітка
        self.assertIn(self.key.key[-4:], html)               # замаскований: видно кінець

    def test_proxy_uses_selected_key_server_side(self):
        self.client.force_login(self.admin)
        r = self.proxy({"url": "http://testserver/api/v1/ping/", "method": "GET",
                        "auth": {"type": "key", "key_id": self.key.pk}})
        data = r.json()
        self.assertEqual(data["status"], 200, data)
        self.assertNotIn(self.key.key, r.content.decode())
        self.assertTrue(data["request"]["auth"].startswith("key: "))
        # без ключа — 401 від самого API
        self.assertEqual(self.proxy({"url": "/api/v1/ping/"}).json()["status"], 401)

    def test_proxy_never_leaves_minerva(self):
        self.client.force_login(self.admin)
        with mock.patch("requests.request") as net, mock.patch("urllib.request.urlopen") as net2:
            for url in ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:5432/",
                        "http://192.168.2.123:81/api/v1/ping/", "https://evil.example/api/v1/ping/",
                        "//evil.example/api/v1/ping/", "/admin/", "/api/v1/../../admin/",
                        "http://user:pw@testserver/api/v1/ping/", "file:///etc/passwd"):
                r = self.proxy({"url": url, "auth": {"type": "key", "key_id": self.key.pk}})
                self.assertEqual(r.status_code, 400, url)
            net.assert_not_called()
            net2.assert_not_called()

    def test_target_rules(self):
        req = RequestFactory().get("/", HTTP_HOST="minerva.example:8443")
        self.assertEqual(own_api_target(req, "/api/v1/stock/?sku=A")[0], ("/api/v1/stock/", "sku=A"))
        self.assertEqual(own_api_target(req, "https://minerva.example:8443/api/v1/ping/")[0], ("/api/v1/ping/", ""))
        self.assertIsNone(own_api_target(req, "https://minerva.example/api/v1/ping/")[0])   # інший порт
        self.assertIsNone(own_api_target(req, "/api/v1/%2e%2e/admin/")[0])
