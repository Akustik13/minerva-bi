"""Помічник на сайті: звіт сайту про private/minerva-config.php, фактична адреса RAG, видалення файлу на сайтах,
«🧪 Перевірити RAG» (shop/assistant.py). python manage.py test shop --settings=tabele.settings_test"""
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from api.models import APIKey

from . import assistant
from .models import Shop

URL = "/api/v1/shop/assistant/"


class Resp:
    def __init__(self, code, data=None):
        self.status_code, self._data, self.text = code, data, ""

    def json(self):
        return self._data


class AssistantSitesTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True, url="https://shop.example.com")
        self.key = APIKey.objects.create(name="Сайт", scopes=["products:read"], shop=self.shop)
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION="Token " + self.key.key)

    def report(self, **params):
        return self.c.get(URL, dict({"site": "web.example.com", "file": "1", "file_enabled": "1", "file_key": "1",
                                     "file_url": "http://192.168.2.68:8790"}, **params)).json()

    def set(self, **fields):
        Shop.objects.filter(pk=self.shop.pk).update(**fields)
        self.shop.refresh_from_db()

    def test_report_and_effective_endpoint(self):
        self.report()
        self.shop.refresh_from_db()
        rep = self.shop.assistant_sites["web.example.com"]
        self.assertEqual((rep["file"], rep["file_url"], rep["file_key"]), (True, "http://192.168.2.68:8790", True))
        self.assertEqual(assistant.effective(self.shop, rep)["endpoint"], "http://192.168.2.68:8790/api")
        self.set(assistant_mode="on", assistant_url="https://ai.example.com")
        eff = assistant.effective(self.shop, rep)
        self.assertEqual((eff["endpoint"], eff["url_from"], eff["key_from"]),
                         ("https://ai.example.com/api", "Minerva", "файл сайту"))
        unbound = APIKey.objects.create(name="RAG", scopes=["products:read"])   # без прив'язки — звіт не приймається
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Token " + unbound.key)
        c.get(URL, {"site": "evil.example.org", "file": "0"})
        self.c.get(URL, {"site": "bad host!", "file": "0"})
        self.shop.refresh_from_db()
        self.assertEqual(list(self.shop.assistant_sites), ["web.example.com"])

    def test_remove_file_flow(self):
        self.report()
        self.shop.refresh_from_db()
        self.assertIn("Увімкнено", assistant.can_remove_file(self.shop))   # «як на сайті» — файл ще потрібен
        self.set(assistant_mode="on", assistant_url="https://ai.example.com")
        self.assertIn("ключ", assistant.can_remove_file(self.shop))          # без ключа в Minerva чат зупиниться
        self.set(assistant_key="bridge", assistant_remove_file=True)
        self.assertTrue(self.report()["remove_file"])
        self.report(file="0")                                                # сайт видалив файл
        self.shop.refresh_from_db()
        self.assertFalse(self.shop.assistant_remove_file)
        self.assertFalse(self.report(file="0")["remove_file"])

    def test_admin_table_buttons_and_rag_test(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.report()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        page = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        for text in ("web.example.com", "http://192.168.2.68:8790/api", "🗑 Видалити private/minerva-config.php",
                     'id="shop-ragtest-run"'):
            self.assertContains(page, text)
        self.client.post(f"/admin/shop/shop/{self.shop.pk}/assistant-remove-file/")
        self.shop.refresh_from_db()
        self.assertFalse(self.shop.assistant_remove_file)                    # «як на сайті» — відмова
        self.set(assistant_mode="on", assistant_url="https://ai.example.com", assistant_key="bridge-key")
        self.client.post(f"/admin/shop/shop/{self.shop.pk}/assistant-remove-file/")
        self.shop.refresh_from_db()
        self.assertTrue(self.shop.assistant_remove_file)
        calls = []

        def get(url, **kw):
            calls.append(url)
            if url.endswith("/api/session"):
                return Resp(200, {"csrf": "c" * 43})
            return Resp(200, {"status": "completed", "result": {"answer": "Antennen: AN-1", "sources": [1]}})

        def post(url, **kw):
            calls.append(url)
            return Resp(201, {"id": "a" * 32}) if url.endswith("/conversations") else Resp(202, {"job_id": "b" * 32})
        with mock.patch("requests.Session.get", side_effect=get), mock.patch("requests.Session.post", side_effect=post):
            data = self.client.post(f"/admin/shop/shop/{self.shop.pk}/assistant-test/", {"question": "Test?"}).json()
        self.assertTrue(data["summary"].startswith("✅"), data)
        self.assertIn("Antennen: AN-1", data["checks"][-1]["detail"])
        self.assertIn("https://ai.example.com/api/ask", calls)
        self.assertEqual(self.client.get(f"/admin/shop/shop/{self.shop.pk}/assistant-test/").status_code, 403)

    def test_rag_test_rejected_key_and_rag_api(self):
        self.set(assistant_mode="on", assistant_url="https://ai.example.com", assistant_key="wrong")
        with mock.patch("requests.Session.get", return_value=Resp(403)):
            res = assistant.test_rag(self.shop)
        self.assertEqual(res["checks"][0]["level"], "error")
        self.assertIn("не прийнято", res["checks"][0]["detail"])
        self.set(assistant_url="", assistant_key="")
        self.assertEqual(assistant.test_rag(self.shop)["checks"][0]["level"], "warn")   # адреса лише у файлі сайту
        from rag_assistant.models import RagSettings
        rag = RagSettings.get()
        rag.base_url, rag.api_key = "https://app.example.com", "rag-key"
        rag.save()
        self.set(assistant_backend="rag_api")
        with mock.patch("rag_assistant.client.RagClient.health", return_value={"status": "ok"}), \
                mock.patch("rag_assistant.client.RagClient.ask", return_value="j1"), \
                mock.patch("rag_assistant.client.RagClient.job",
                           return_value={"status": "completed", "result": {"answer": "OK", "sources": []}}):
            res = assistant.test_rag(self.shop, "Frage?")
        self.assertEqual([c["level"] for c in res["checks"]], ["ok", "ok"])
        self.assertIn("app.example.com/v1", res["checks"][0]["detail"])
