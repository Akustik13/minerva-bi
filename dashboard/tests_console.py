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


class ConsoleBase(TestCase):
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


class ConsoleTests(ConsoleBase):
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


class ServiceKeysTests(ConsoleBase):
    """Ключ RAG (база знань) і ключ RAG-ПК (наради) — не для консолі: інакше адміністратор прочитав би чужі листи й
    розшифровки нарад. Адреси /api/v1/knowledge/ і /api/v1/meetings/ консоль теж не відкриває."""

    def setUp(self):
        super().setUp()
        self.rag = APIKey.objects.create(name="RAG — база знань", scopes=["knowledge:read", "knowledge:write"])
        self.bench = APIKey.objects.create(name="RAG-ПК — наради", scopes=["meetings:write"])
        self.mixed = APIKey.objects.create(name="mixed", scopes=["products:read", "knowledge:read"])

    def test_console_list_has_no_service_keys(self):
        from dashboard.api_views import console_keys
        ids = {k["id"] for k in console_keys()}
        self.assertIn(self.key.pk, ids)
        self.assertFalse(ids & {self.rag.pk, self.bench.pk, self.mixed.pk})
        self.client.force_login(self.admin)
        html = self.client.get("/dashboard/api/console/").content.decode()
        self.assertNotIn("RAG-ПК — наради", html)
        self.assertNotIn("RAG — база знань", html)

    def test_closed_areas(self):
        req = RequestFactory().get("/", HTTP_HOST="testserver")
        for url in ("/api/v1/knowledge/documents/document:7/", "/api/v1/knowledge/changes/?after=0",
                    "/api/v1/meetings/sync/", "/api/v1/meetings/12/status/", "/api/v1/meetings/ping/",
                    "/api/v1/Meetings/ping/", "/api/v1/%6Deetings/ping/", "/api/v1/knowledge",
                    "http://testserver/api/v1/knowledge/summary/"):
            target, why = own_api_target(req, url)
            self.assertIsNone(target, url)
            self.assertTrue("RAG" in why, (url, why))
        self.assertEqual(own_api_target(req, "/api/v1/ping/")[0], ("/api/v1/ping/", ""))

    def test_proxy_refuses_service_keys_and_paths(self):
        from meetings import services
        self.client.force_login(self.admin)
        with mock.patch.object(services, "sync") as sync, mock.patch("rag_assistant.knowledge.get_document") as doc:
            # документ частини наради з ключем RAG — відмова ще до запиту
            r = self.proxy({"url": "/api/v1/knowledge/documents/document:7/", "method": "GET",
                            "auth": {"type": "key", "key_id": self.rag.pk}})
            self.assertEqual(r.status_code, 400)
            self.assertIn("RAG", r.json()["error"])
            # звичайна адреса, але службовий ключ — за id, вставлений як токен або в заголовок
            for payload in ({"auth": {"type": "key", "key_id": self.rag.pk}},
                            {"auth": {"type": "key", "key_id": self.bench.pk}},
                            {"auth": {"type": "key", "key_id": self.mixed.pk}},
                            {"auth": {"type": "token", "token": self.bench.key}},
                            {"auth": {"type": "bearer", "token": self.rag.key}},
                            {"headers": {"authorization": f"Token {self.bench.key}"}}):
                r = self.proxy({"url": "/api/v1/ping/", "method": "GET", **payload})
                self.assertEqual(r.status_code, 400, payload)
                self.assertIn("службовий ключ", r.json()["error"])
                self.assertNotIn(self.bench.key, r.content.decode())
                self.assertNotIn(self.rag.key, r.content.decode())
            # адреса нарад — відмова навіть зі звичайним ключем
            r = self.proxy({"url": "/api/v1/meetings/sync/", "method": "POST", "body": "{}",
                            "auth": {"type": "key", "key_id": self.key.pk}})
            self.assertEqual(r.status_code, 400)
            sync.assert_not_called()
            doc.assert_not_called()
        # звичайний ключ і звичайна адреса — як і раніше
        r = self.proxy({"url": "/api/v1/ping/", "method": "GET", "auth": {"type": "key", "key_id": self.key.pk}})
        self.assertEqual(r.json()["status"], 200)
