"""Журнал запитів і обмеження ключів API (api/key_limits.py).
python manage.py test api --settings=tabele.settings_test"""
from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone

from api.admin import APIKeyForm
from api.models import APIKey, APIRequestLog

from .tests import APITestBase


class KeyBase(APITestBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.receive(self.p1, 5)
        APIRequestLog.objects.all().delete()

    def set(self, **fields):
        APIKey.objects.filter(pk=self.key.pk).update(**fields)
        self.key.refresh_from_db()

    def log(self):
        return APIRequestLog.objects.order_by("-pk").first()


class LogTests(KeyBase):
    def test_who_from_where_what(self):
        r = self.client.get("/api/v1/stock/?search=AMP&page_size=5", HTTP_X_REAL_IP="203.0.113.5",
                            HTTP_USER_AGENT="shop-sync/1.0")
        self.assertEqual(r.status_code, 200)
        row = self.log()
        self.assertEqual((row.key, row.key_name, row.ip, row.method, row.path, row.status),
                         (self.key, "webshop", "203.0.113.5", "GET", "/api/v1/stock/", 200))
        self.assertEqual((row.view, row.scope, row.items, row.objects_text), ("StockViewSet.list", "stock:read", 1, "AMP-100"))
        self.assertEqual((row.query, row.user_agent), ("search=AMP&page_size=5", "shop-sync/1.0"))
        self.client.get("/api/v1/ping/", HTTP_X_FORWARDED_FOR="1.1.1.1, 198.51.100.7")
        self.assertEqual(self.log().ip, "198.51.100.7")                     # останній — той, кого бачив проксі
        self.client.get("/api/v1/stock/?token=abc")
        self.assertEqual(self.log().query, "token=%2A%2A%2A")               # секрети в адресі не пишуться

    def test_unknown_key_and_no_key(self):
        self.client.credentials(HTTP_AUTHORIZATION="Token deadbeefdeadbeefdeadbeef")
        self.assertEqual(self.client.get("/api/v1/stock/").status_code, 401)
        row = self.log()
        self.assertIsNone(row.key)
        self.assertEqual(row.status, 401)
        self.assertTrue(row.key_hint.startswith("dead") and row.key_hint.endswith("beef"))
        self.assertNotIn("deadbeefdeadbeef", row.key_hint)
        self.client.credentials()
        self.client.get("/api/v1/stock/")
        self.assertEqual(APIRequestLog.objects.count(), 1)                  # без ключа — не пишемо

    def test_log_modes(self):
        self.set(log_mode=APIKey.LOG_CHANGES)
        self.client.get("/api/v1/stock/")
        self.assertFalse(APIRequestLog.objects.exists())
        self.receive(self.p1, 1)
        self.assertEqual(self.log().method, "POST")
        self.set(log_mode=APIKey.LOG_ERRORS)
        self.client.get("/api/v1/stock/NOPE/")
        self.assertEqual(self.log().status, 404)
        n = APIRequestLog.objects.count()
        self.receive(self.p1, 1)
        self.set(log_mode=APIKey.LOG_OFF)
        self.client.get("/api/v1/stock/NOPE/")
        self.assertEqual(APIRequestLog.objects.count(), n)

    def test_cleanup(self):
        self.client.get("/api/v1/ping/")
        APIRequestLog.objects.update(created_at=timezone.now() - timedelta(days=90))
        self.client.get("/api/v1/ping/")
        call_command("api_log_cleanup", stdout=StringIO())
        self.assertEqual(APIRequestLog.objects.count(), 1)


class LimitTests(KeyBase):
    def test_allowed_ips(self):
        self.set(allowed_ips="10.0.0.0/24\n198.51.100.7")
        self.assertEqual(self.client.get("/api/v1/ping/", HTTP_X_REAL_IP="10.0.0.42").status_code, 200)
        r = self.client.get("/api/v1/ping/", HTTP_X_REAL_IP="203.0.113.5")
        self.assertEqual(r.status_code, 401)
        self.assertIn("203.0.113.5", r.json()["detail"])
        row = self.log()
        self.assertEqual((row.key, row.status, row.ip), (self.key, 401, "203.0.113.5"))
        self.assertIn("Дозволених IP", row.note)
        self.assertFalse(APIKeyForm(data={"name": "x", "allowed_ips": "10.0.0.300", "log_mode": "all"}).is_valid())

    def test_hour_and_day_limits(self):
        self.set(limit_hour=2)
        for _ in range(2):
            self.assertEqual(self.client.get("/api/v1/ping/").status_code, 200)
        r = self.client.get("/api/v1/ping/")
        self.assertEqual(r.status_code, 429)
        self.assertIn("2 запитів за годину", r.json()["detail"])
        self.assertTrue(int(r["Retry-After"]) > 0)
        self.assertIn("за годину", self.log().note)
        self.set(limit_hour=None, limit_day=3)
        self.assertEqual(self.client.get("/api/v1/ping/").status_code, 200)  # сьогодні 2 (відмова не рахується) → 3
        self.assertEqual(self.client.get("/api/v1/ping/").status_code, 429)

    def test_own_minute_limit(self):
        self.set(limit_minute=1)
        cache.clear()                                                       # запит із setUp уже в хвилинному ліміті
        self.assertEqual(self.client.get("/api/v1/ping/").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/ping/").status_code, 429)

    def test_fields_only(self):
        self.set(fields="sku, available")
        r = self.client.get("/api/v1/stock/")
        self.assertEqual(r.json()["results"][0], {"sku": "AMP-100", "available": 5})
        self.assertEqual(set(self.client.get("/api/v1/stock/AMP-100/").json()), {"sku", "available"})
        self.assertEqual(self.client.get("/api/v1/ping/").json()["key"], "webshop")   # не записи — як є

    def test_items_per_day(self):
        self.receive(self.p2, 3)
        self.set(items_per_day=1)
        r = self.client.get("/api/v1/stock/?ordering=sku")
        body = r.json()
        self.assertEqual([i["sku"] for i in body["results"]], ["AMP-100"])
        self.assertEqual(body["quota"], {"items_per_day": 1, "used": 1, "hidden": 1})
        self.assertEqual(r["X-Items-Remaining"], "0")
        self.assertIn("приховано 1", self.log().note)
        self.assertEqual((self.log().items, self.log().objects_text), (1, "AMP-100"))   # те, що ключ отримав
        self.assertEqual(self.client.get("/api/v1/stock/AMP-100/").status_code, 200)   # уже бачений
        r = self.client.get("/api/v1/stock/CABLE-M/")
        self.assertEqual((r.status_code, r.json()["code"]), (429, "daily_items_limit"))
        self.assertEqual((self.log().status, self.log().items, self.log().objects_text), (429, 0, ""))


class AdminTests(KeyBase):
    def test_pages(self):
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.set(limit_day=100, items_per_day=10, allowed_ips="10.0.0.0/24")
        self.client.get("/api/v1/stock/", HTTP_X_REAL_IP="10.0.0.9")
        admin = self.client_class()
        admin.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        page = admin.get(f"/admin/api/apikey/{self.key.pk}/change/")
        self.assertContains(page, "Обмеження")
        self.assertContains(page, "<code>10.0.0.9</code>")
        self.assertContains(page, "за сьогодні: <b>1</b> з 100")
        listing = admin.get("/admin/api/apikey/")
        self.assertContains(listing, "100/добу")
        self.assertContains(listing, "1 запитів")
        log = admin.get(f"/admin/api/apirequestlog/?key__id__exact={self.key.pk}&st=ok")
        self.assertContains(log, "/api/v1/stock/")
        self.assertEqual(admin.get("/admin/api/apirequestlog/?st=err&rw=write").status_code, 200)
