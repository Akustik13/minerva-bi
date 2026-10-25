"""Помічник Minerva на сайті: керування з картки магазину (shop/assistant.py), API GET /shop/assistant/,
рядок у перевірці зв'язку. python manage.py test shop --settings=tabele.settings_test"""
from unittest import mock

from django import forms
from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from api.models import APIKey

from . import assistant, connection
from .admin_payments import ShopForm
from .models import Shop
from .tests_connection import ConnectionBase, FakeResponse

URL = "/api/v1/shop/assistant/"


class AssistantApiTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Web", slug="webshop", is_default=True, url="https://shop.example.com")
        self.site_key = APIKey.objects.create(name="Сайт", scopes=["products:read"], shop=self.shop)
        self.rag_key = APIKey.objects.create(name="API RAG", scopes=["products:read"])   # без прив'язки

    def get(self, key):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Token " + key.key)
        return c.get(URL)

    def test_default_site_decides(self):
        data = self.get(self.site_key).json()
        self.assertEqual(data, {"shop": "webshop", "managed": False, "enabled": False, "backend": "bridge",
                                "upstream_url": "", "bridge_key": ""})

    def test_on_with_address_and_key_only_for_bound_key(self):
        Shop.objects.filter(pk=self.shop.pk).update(assistant_mode="on", assistant_url="https://rag.example.com",
                                                    assistant_key="bridge-secret")
        data = self.get(self.site_key).json()
        self.assertEqual((data["managed"], data["enabled"], data["upstream_url"], data["bridge_key"]),
                         (True, True, "https://rag.example.com", "bridge-secret"))
        other = self.get(self.rag_key).json()   # працює з магазином за замовчуванням, але ключа помічника не бачить
        self.assertEqual((other["enabled"], other["bridge_key"]), (True, ""))

    def test_off_and_scope(self):
        Shop.objects.filter(pk=self.shop.pk).update(assistant_mode="off", assistant_key="bridge-secret")
        data = self.get(self.site_key).json()
        self.assertEqual((data["managed"], data["enabled"]), (True, False))
        no_scope = APIKey.objects.create(name="Склад", scopes=["stock:read"], shop=self.shop)
        self.assertEqual(self.get(no_scope).status_code, 403)

    def test_source_bound_key_gets_key(self):
        Shop.objects.filter(pk=self.shop.pk).update(assistant_mode="on", assistant_key="bridge-secret")
        by_source = APIKey.objects.create(name="За джерелом", scopes=["products:read"], default_source="webshop")
        self.assertEqual(self.get(by_source).json()["bridge_key"], "bridge-secret")

    def test_rag_api_backend_uses_product_assistant_settings(self):
        from rag_assistant.models import RagSettings
        rag = RagSettings.get()
        rag.base_url, rag.api_key, rag.latest_only = "https://app.example.com/", "rag-api-key", True
        rag.save()
        Shop.objects.filter(pk=self.shop.pk).update(assistant_mode="on", assistant_backend="rag_api",
                                                    assistant_url="http://192.168.2.68:8790", assistant_key="bridge")
        data = self.get(self.site_key).json()
        self.assertEqual((data["backend"], data["upstream_url"], data["api_key"], data["bridge_key"], data["latest"]),
                         ("rag_api", "https://app.example.com", "rag-api-key", "", True))
        self.assertEqual(self.get(self.rag_key).json()["api_key"], "")   # ключ без прив'язки — без ключа RAG
        self.shop.refresh_from_db()
        self.assertIn("RAG API помічника Minerva", assistant.describe(self.shop))
        rag.api_key = ""
        rag.save()
        self.assertIn("не задано адресу або ключ", assistant.describe(self.shop))

    def test_check_url(self):
        for ok in ("", "https://rag.example.com", "https://app.sevskiy.com/site-chat", "http://192.168.2.68:8790",
                   "http://10.0.0.5"):
            self.assertEqual(assistant.check_url(ok), "", ok)
        for bad in ("http://rag.example.com", "http://8.8.8.8:8790", "ftp://192.168.2.1", "https://u:p@x.example",
                    "https://x.example/?k=1", "rag.example.com"):
            self.assertNotEqual(assistant.check_url(bad), "", bad)


class AssistantFormTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Web", slug="webshop", assistant_key="stored")

    def form(self, **cleaned):
        f = ShopForm(instance=self.shop)
        f.cleaned_data = cleaned
        return f

    def test_key_kept_when_empty_and_url_checked(self):
        self.assertEqual(self.form(assistant_key="").clean_assistant_key(), "stored")
        self.assertEqual(self.form(assistant_key=" new ").clean_assistant_key(), "new")
        self.assertEqual(self.form(assistant_url="http://192.168.2.68:8790/").clean_assistant_url(),
                         "http://192.168.2.68:8790")
        with self.assertRaises(forms.ValidationError):
            self.form(assistant_url="http://rag.example.com").clean_assistant_url()
        self.assertIsInstance(ShopForm().fields["assistant_key"].widget, forms.PasswordInput)

    def test_admin_page_shows_block(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        page = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        self.assertContains(page, "Помічник Minerva на сайті")
        self.assertContains(page, "керує сайт")
        self.assertNotContains(page, "stored")   # збережений ключ у сторінку не потрапляє


class AssistantConnectionTests(ConnectionBase):
    def status(self, **data):
        return FakeResponse(data=dict({"enabled": False, "source": "minerva"}, **data))

    def run_check(self, mode, status):
        Shop.objects.filter(pk=self.shop.pk).update(assistant_mode=mode)
        self.shop.refresh_from_db()
        with self.site(FakeResponse(data=self.catalog()), assistant=status):
            res = connection.check(self.shop)
        return self.levels(res)["Помічник на сайті"], self.detail(res, "Помічник на сайті")

    def test_states(self):
        self.assertEqual(self.run_check("off", self.status())[0], "ok")
        level, text = self.run_check("off", self.status(enabled=True))
        self.assertEqual(level, "warn")
        level, text = self.run_check("on", self.status(reason="no_key"))
        self.assertEqual(level, "error")
        self.assertIn("Ключ помічника", text)
        level, text = self.run_check("on", self.status(enabled=True, reachable=False))
        self.assertEqual(level, "error")
        self.assertIn("https://", text)
        self.assertEqual(self.run_check("on", self.status(enabled=True, reachable=True))[0], "ok")
        level, text = self.run_check("site", self.status(enabled=True, source="site", reachable=True))
        self.assertEqual(level, "info")
        self.assertIn("RAG відповідає", text)

    def test_rag_api_reasons(self):
        level, text = self.run_check("on", self.status(reason="no_upstream", backend="rag_api"))
        self.assertEqual(level, "error")
        self.assertIn("Налаштування помічника", text)
        level, text = self.run_check("on", self.status(enabled=True, reachable=True, backend="rag_api"))
        self.assertEqual(level, "ok")
        self.assertIn("через RAG API", text)
        level, text = self.run_check("on", self.status(enabled=True, reachable=False, backend="rag_api"))
        self.assertEqual(level, "error")
        self.assertIn("не дістає RAG API", text)

    def test_old_site_without_status(self):
        level, text = self.run_check("on", None)
        self.assertEqual(level, "info")
        self.assertIn("без керування", text)

    def test_status_request_goes_to_site(self):
        Shop.objects.filter(pk=self.shop.pk).update(assistant_mode="on")
        self.shop.refresh_from_db()
        seen = []

        def get(url, **kw):
            seen.append((url, kw.get("params")))
            return self.status(enabled=True, reachable=True) if "/api/minerva/" in url else FakeResponse(
                data=self.catalog())
        with mock.patch.object(connection.requests, "get", side_effect=get):
            connection.check(self.shop)
        self.assertIn(("https://shop.example.com/api/minerva/index.php", {"action": "status", "check": "1"}), seen)
