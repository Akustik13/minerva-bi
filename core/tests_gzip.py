"""Стиснення сторінок gzip (core/gzip.py) і меню адмінки без запиту на кожен розділ (tabele/admin.py).

    python manage.py test core.tests_gzip --settings=tabele.settings_test
"""
import gzip

from django.contrib.auth.models import User
from django.db import connection
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext

from core.gzip import PageGZipMiddleware

BIG = "<p>Minerva — замовлення й пошта</p>" * 200


class GzipTests(TestCase):
    def run_mw(self, response, accept="gzip, deflate"):
        req = RequestFactory().get("/x/", HTTP_ACCEPT_ENCODING=accept)
        return PageGZipMiddleware(lambda r: response)(req)

    def test_html_and_json_are_compressed(self):
        r = self.run_mw(HttpResponse(BIG, content_type="text/html; charset=utf-8"))
        self.assertEqual(r["Content-Encoding"], "gzip")
        self.assertEqual(gzip.decompress(r.content).decode(), BIG)
        self.assertIn("Accept-Encoding", r["Vary"])
        r = self.run_mw(HttpResponse('{"a": "' + "x" * 2000 + '"}', content_type="application/json"))
        self.assertEqual(r["Content-Encoding"], "gzip")

    def test_without_accept_encoding_unchanged(self):
        r = self.run_mw(HttpResponse(BIG, content_type="text/html"), accept="")
        self.assertFalse(r.has_header("Content-Encoding"))

    def test_streaming_files_and_pdf_untouched(self):
        """NDJSON «синхронізувати всю пошту» має йти рядок за рядком; PDF і файли — як є (з Content-Length)."""
        r = self.run_mw(StreamingHttpResponse(iter([b'{"n":1}\n'] * 50), content_type="application/x-ndjson"))
        self.assertFalse(r.has_header("Content-Encoding"))
        r = self.run_mw(StreamingHttpResponse(iter([BIG.encode()]), content_type="text/html"))
        self.assertFalse(r.has_header("Content-Encoding"))
        r = self.run_mw(HttpResponse(b"%PDF-1.4 " + b"0" * 5000, content_type="application/pdf"))
        self.assertFalse(r.has_header("Content-Encoding"))
        import io
        r = self.run_mw(FileResponse(io.BytesIO(b"x" * 5000), content_type="text/plain"))
        self.assertFalse(r.has_header("Content-Encoding"))

    def test_in_settings_first(self):
        from django.conf import settings
        self.assertEqual(settings.MIDDLEWARE[0], "core.gzip.PageGZipMiddleware")
        r = self.client.get("/admin/login/", HTTP_ACCEPT_ENCODING="gzip")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Encoding"], "gzip")


class MenuModulesTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.root = User.objects.create_superuser("root", "r@x.y", "p")

    def test_inactive_module_hidden_with_one_query(self):
        from django.contrib import admin
        from core.models import ModuleRegistry
        ModuleRegistry.objects.update_or_create(app_label="offers", defaults={"name": "Angebote", "is_active": False})
        request = RequestFactory().get("/admin/")
        request.user = self.root
        with CaptureQueriesContext(connection) as ctx:
            labels = {a["app_label"] for a in admin.site.get_app_list(request)}
        self.assertNotIn("offers", labels)
        self.assertIn("sales", labels)
        registry = [q for q in ctx.captured_queries if "core_moduleregistry" in q["sql"]]
        self.assertEqual(len(registry), 1)
