"""«🔍 Що бачить і що може змінити ключ» (api/key_insight.py) і журнал API людською мовою / «📊 Аналіз журналу»
(api/log_analysis.py).

python manage.py test api.tests_key_insight --settings=tabele.settings_test
"""
import json
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, RequestFactory

from api import key_insight, key_limits, log_analysis
from api.models import APIKey, APIRequestLog, Webhook
from inventory.models import InventoryTransaction, Product
from sales.models import SalesOrder

from . import tests as api_tests
from .tests import APITestBase

ORDER = api_tests.OrderTests.ORDER          # модуль, не клас: інакше тести OrderTests запустились би вдруге

OLD = datetime(2026, 1, 1, 12, 0, tzinfo=dt_timezone.utc)


def person(username, role="manager", **profile):
    user = User.objects.create_user(username, f"{username}@x.y", "p", is_staff=True)
    user.profile.role = role
    for k, v in profile.items():
        setattr(user.profile, k, v)
    user.profile.save()
    return User.objects.get(pk=user.pk)


class InsightBase(APITestBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.receive(self.p1, 5)
        cache.clear()
        APIRequestLog.objects.all().delete()
        self.admin = Client()
        self.admin.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))

    def set(self, **fields):
        APIKey.objects.filter(pk=self.key.pk).update(**fields)
        self.key.refresh_from_db()

    def rows(self):
        return {(r["method"], r["path"]): r for r in key_insight.capabilities(self.key)}

    def preview(self, path, query=""):
        r = self.admin.post(f"/admin/api/apikey/{self.key.pk}/insight/preview/", {"path": path, "query": query})
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def simulate(self, method, path, body=None):
        r = self.admin.post(f"/admin/api/apikey/{self.key.pk}/insight/simulate/",
                            {"method": method, "path": path, "body": json.dumps(body) if body is not None else ""})
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def shop(self):
        from shop.models import Shop, ShopListing
        shop = Shop.objects.create(name="Sevskiy", slug="webshop", url="https://web.example.com")
        ShopListing.objects.create(shop=shop, product=self.p1)
        return shop


class CapabilityTests(InsightBase):
    def test_scopes_decide_what_is_allowed(self):
        self.set(scopes=["stock:read"])
        rows = self.rows()
        self.assertEqual(rows[("GET", "/api/v1/stock/")]["access"], "allowed")
        self.assertIn("available", rows[("GET", "/api/v1/stock/")]["returns"]["fields"])
        count = rows[("POST", "/api/v1/stock/count/")]
        self.assertEqual((count["access"], count["kind"]), ("denied", "write"))
        self.assertIn("stock:write", count["access_text"])
        self.assertIn("Коригування", count["effect"])                         # що змінює — людською мовою
        check_ = rows[("POST", "/api/v1/stock/check/")]
        self.assertEqual((check_["access"], check_["kind"], check_["scope"]), ("allowed", "read_post", "stock:read"))
        self.assertEqual(rows[("POST", "/api/v1/orders/")]["access"], "denied")
        # право — як api.permissions.required_scope: дія APIView (login) — читання, база знань — knowledge:*
        self.assertEqual(rows[("POST", "/api/v1/shop/staff/login/")]["scope"], "products:read")
        self.assertEqual(rows[("POST", "/api/v1/knowledge/status/")]["scope"], "knowledge:write")
        self.assertEqual(rows[("POST", "/api/v1/shop/pair/")]["access"], "nokey")
        self.assertEqual(rows[("DELETE", "/api/v1/orders/{pk}/")]["access"], "unsupported")
        sm = key_insight.summary(list(rows.values()))
        self.assertEqual(sm["write"], 0)
        self.assertGreater(sm["denied"], 10)

    def test_fields_and_writable_fields(self):
        self.set(fields="sku, available")
        rows = self.rows()
        stock = rows[("GET", "/api/v1/stock/")]["returns"]
        self.assertEqual(stock["fields"], ["sku", "available"])
        self.assertIn("2 з", " ".join(stock["notes"]))
        accepts = rows[("POST", "/api/v1/orders/")]["accepts"]["fields"]       # тіло — OrderCreateSerializer
        self.assertIn("lines", accepts)
        self.assertIn("order_number", accepts)
        self.assertIn("tx_type", rows[("POST", "/api/v1/stock-movements/")]["accepts"]["fields"])

    def test_shop_key_is_narrowed(self):
        shop = self.shop()                                        # «Джерело замовлень» ключа = код магазину
        rows = self.rows()
        patch = rows[("PATCH", "/api/v1/orders/{pk}/")]
        self.assertEqual(sorted(patch["accepts"]["fields"]), ["payment_method", "payment_reference", "payment_status"])
        self.assertTrue(any("лише замовлення магазину" in n for n in patch["notes"]))
        products = rows[("GET", "/api/v1/products/")]["returns"]
        self.assertNotIn("purchase_price", products["fields"])
        self.assertTrue(any("purchase_price" in n for n in products["notes"]))
        restr = " ".join(r["title"] for r in key_insight.restrictions(self.key))
        self.assertIn(f"Ключ сайту магазину «{shop}»", restr)

    def test_page_and_card(self):
        page = self.admin.get(f"/admin/api/apikey/{self.key.pk}/insight/")
        self.assertContains(page, "Що бачить і що може змінити ключ")
        self.assertContains(page, "/api/v1/stock/count/")
        self.assertContains(page, "👁 Приклад даних")
        self.assertNotContains(page, self.key.key)                              # секрет ключа не показується
        card = self.admin.get(f"/admin/api/apikey/{self.key.pk}/change/")
        self.assertContains(card, "Таблиця можливостей, приклад даних і симуляція")
        self.assertContains(card, "Аналіз журналу ключа")


class PreviewTests(InsightBase):
    def test_shaped_data_without_side_effects(self):
        self.receive(self.p2, 3)
        self.set(fields="sku, available", items_per_day=1, limit_day=100, last_used=OLD)
        cache.clear()
        APIRequestLog.objects.all().delete()
        before = key_limits.usage(self.key)
        d = self.preview("/api/v1/stock/", "ordering=sku&page_size=3")
        self.assertEqual(d["status"], 200)
        data = json.loads(d["json"])
        self.assertEqual(data["results"], [{"sku": "AMP-100", "available": 5}])     # поля й «записів за добу»
        self.assertEqual(data["quota"]["hidden"], 1)
        self.assertIn("приховано 1", " ".join(d["notes"]))
        self.assertEqual(key_limits.usage(self.key), before)                         # ліміти й лічильник — ті самі
        self.key.refresh_from_db()
        self.assertEqual(self.key.last_used, OLD)
        self.assertFalse(APIRequestLog.objects.exists())                             # журнал API — без перегляду
        from core.models import AuditLog
        self.assertTrue(AuditLog.objects.filter(object_repr="webshop", extra__what="👁 Приклад даних як ключ").exists())
        # той самий запит від програми — рахується як завжди
        self.assertEqual(self.client.get("/api/v1/stock/?ordering=sku").status_code, 200)
        after = key_limits.usage(self.key)
        self.assertEqual((after["items_per_day"], after["limit_day"]), (1, 1))
        self.assertEqual(APIRequestLog.objects.count(), 1)

    def test_preview_flag_cannot_be_injected(self):
        self.set(limit_day=100, last_used=OLD)
        name = key_limits.PREVIEW_ATTR
        r = self.client.get(f"/api/v1/stock/?{name}=1&preview=1", HTTP_X_MINERVA_KEY_PREVIEW="1",
                            **{"HTTP_" + name.upper(): "1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(APIRequestLog.objects.count(), 1)                          # записано в журнал
        self.key.refresh_from_db()
        self.assertNotEqual(self.key.last_used, OLD)
        self.assertEqual(key_limits.usage(self.key)["limit_day"], 1)
        request = RequestFactory().get(f"/api/v1/stock/?{name}=1", HTTP_X_MINERVA_KEY_PREVIEW="1")
        self.assertFalse(key_limits.is_preview(request))
        self.assertFalse(key_limits.is_preview(SimpleNamespace(_request=request)))

    def test_only_safe_get_endpoints(self):
        self.assertIn("перегляд не відкриває", self.preview("/api/v1/shop/search/assist/")["error"])
        self.assertIn("перегляд не відкриває", self.preview("/api/v1/knowledge/changes/")["error"])
        self.assertIn("немає", self.preview("/api/v1/stock/check/")["error"])          # POST-дія — не GET
        self.assertIn("немає", self.preview("/admin/")["error"])
        self.assertEqual(self.admin.get(f"/admin/api/apikey/{self.key.pk}/insight/preview/").status_code, 405)
        self.set(scopes=["orders:read"])
        self.assertEqual(self.preview("/api/v1/stock/")["status"], 403)               # права — як у ключа
        self.set(is_active=False)
        self.assertEqual(self.preview("/api/v1/orders/")["status"], 401)

    def test_one_record_and_secrets_masked(self):
        d = self.preview("/api/v1/stock/AMP-100/")
        self.assertEqual(json.loads(d["json"])["sku"], "AMP-100")
        self.set(scopes=["webhooks:read"])
        hook = Webhook.objects.create(name="shop", url="https://web.example.com/hook", api_key=self.key)
        d = self.preview("/api/v1/webhooks/")
        self.assertEqual(json.loads(d["json"])[0]["name"], "shop")
        self.assertNotIn(hook.secret, d["json"])                                     # ключу — повний, у перегляді — ні


class SimulationTests(InsightBase):
    def counts(self):
        return (SalesOrder.objects.count(), InventoryTransaction.objects.count(), Product.objects.count(),
                Webhook.objects.count())

    def test_order_create_validates_without_creating(self):
        before = self.counts()
        d = self.simulate("POST", "/api/v1/orders/", ORDER)
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["status"], 201)
        self.assertTrue(any("WS-1001" in e for e in d["effects"]))
        self.assertTrue(any("AMP-100" in e for e in d["effects"]))
        bad = self.simulate("POST", "/api/v1/orders/", {"order_number": "X-1", "lines": []})
        self.assertEqual((bad["ok"], bad["status"]), (False, 400))
        self.assertTrue(any(e.startswith("lines") for e in bad["errors"]))
        unknown = self.simulate("POST", "/api/v1/orders/", {"order_number": "X-2", "lines": [{"sku": "NOPE", "qty": 1}]})
        self.assertIn("invalid_lines", unknown["verdict"])
        self.assertEqual(self.counts(), before)

    def test_stock_writes(self):
        before = self.counts()
        d = self.simulate("POST", "/api/v1/stock/count/", [{"sku": "AMP-100", "qty": 7}])
        self.assertTrue(d["ok"], d)
        self.assertIn("5 → 7", d["effects"][0])
        d = self.simulate("POST", "/api/v1/stock-movements/", {"sku": "AMP-100", "tx_type": "Incoming", "qty": 3})
        self.assertTrue(d["ok"], d)
        self.assertTrue(any("5 → 8" in e for e in d["effects"]))
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.client.get("/api/v1/stock/AMP-100/").json()["on_hand"], 5)

    def test_order_update_and_cancel(self):
        self.client.post("/api/v1/orders/", ORDER, format="json")
        order = SalesOrder.objects.get()
        self.shop()                                                  # тепер ключ — ключ сайту магазину webshop
        d = self.simulate("PATCH", f"/api/v1/orders/{order.pk}/", {"payment_status": "paid", "total_price": 1})
        self.assertTrue(d["ok"], d)
        self.assertTrue(any("payment_status" in e for e in d["effects"]))
        self.assertTrue(any("total_price" in n for n in d["notes"]))               # ключ сайту — відкинуто
        d = self.simulate("POST", f"/api/v1/orders/{order.pk}/cancel/", {})
        self.assertTrue(d["ok"], d)
        self.assertTrue(any("cancelled" in e for e in d["effects"]))
        order.refresh_from_db()
        self.assertNotEqual(order.status, "cancelled")
        self.assertNotEqual(order.payment_status, "paid")

    def test_webhook_and_refusals(self):
        self.set(scopes=["webhooks:read", "webhooks:write", "stock:read"])
        before = self.counts()
        d = self.simulate("POST", "/api/v1/webhooks/", {"name": "shop", "url": "https://web.example.com/hook"})
        self.assertTrue(d["ok"], d)
        self.assertEqual(self.counts(), before)
        d = self.simulate("POST", "/api/v1/orders/", ORDER)
        self.assertEqual((d["status"], d["ran"]), (403, False))
        self.set(scopes=["purchases:write"])
        d = self.simulate("POST", "/api/v1/purchase-orders/", {"external_id": "B-1"})
        self.assertIsNone(d["ok"])
        self.assertIn("Не перевіряється", d["verdict"])
        self.set(scopes=["stock:write"])
        self.assertFalse(self.admin.post(f"/admin/api/apikey/{self.key.pk}/insight/simulate/", {
            "method": "POST", "path": "/api/v1/stock/count/", "body": "{не json"}).json()["ok"])


class SimulationModelTests(InsightBase):
    def test_products_customers_webhooks(self):
        from crm.models import Customer
        self.set(scopes=["products:write", "customers:write", "webhooks:write", "orders:write"])
        d = self.simulate("PATCH", f"/api/v1/products/{self.p1.pk}/", {"sale_price": "120.00"})
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["effects"][0], "sale_price: «99» → «120»")
        self.assertEqual(self.simulate("PATCH", "/api/v1/products/999/", {"name": "x"})["status"], 404)
        d = self.simulate("POST", "/api/v1/customers/", {"name": "Max Mustermann", "email": "kunde@example.com"})
        self.assertTrue(d["ok"], d)
        self.assertFalse(Customer.objects.exists())
        hook = Webhook.objects.create(name="shop", url="https://web.example.com/hook", api_key=self.key)
        d = self.simulate("DELETE", f"/api/v1/webhooks/{hook.pk}/")
        self.assertEqual((d["ok"], d["status"]), (True, 204))
        self.assertTrue(Webhook.objects.filter(pk=hook.pk).exists())
        self.assertEqual(self.simulate("DELETE", "/api/v1/orders/1/")["status"], 405)
        self.assertIn("Не перевіряється", self.simulate("POST", f"/api/v1/webhooks/{hook.pk}/test/")["verdict"])


class LogTests(InsightBase):
    def last(self):
        return APIRequestLog.objects.order_by("-pk").first()

    def test_plain_lines(self):
        self.client.get("/api/v1/products/?with_stock=1&page_size=500")
        self.assertEqual(log_analysis.plain_line(self.last()),
                         "webshop прочитав список товарів зі складом — 2 записи (сторінка 1, page_size 500)")
        self.client.post("/api/v1/orders/", ORDER, format="json")
        row = self.last()
        self.assertIn("lines", row.result["fields"])                                 # назви полів, без значень
        self.assertNotIn("Max Mustermann", json.dumps(row.result, ensure_ascii=False))
        self.assertIn("webshop створив замовлення WS-1001", log_analysis.plain_line(row))
        self.client.post("/api/v1/orders/", ORDER, format="json")
        self.assertIn("повторно надіслав замовлення WS-1001", log_analysis.plain_line(self.last()))
        self.receive(self.p1, 2)
        self.assertIn("прихід 2 × AMP-100", log_analysis.plain_line(self.last()))
        self.set(items_per_day=1)
        self.client.get("/api/v1/stock/?ordering=sku")
        self.client.get("/api/v1/stock/CABLE-M/")
        self.assertEqual(log_analysis.plain_line(self.last()),
                         "webshop отримав відмову 429 (читання залишку товару CABLE-M): денний ліміт різних записів")
        page = self.admin.get(f"/admin/api/apirequestlog/{self.last().pk}/change/")
        self.assertContains(page, "денний ліміт різних записів")
        listing = self.admin.get("/admin/api/apirequestlog/")
        self.assertContains(listing, "прочитав список товарів зі складом")
        self.assertContains(listing, "📊 Аналіз журналу")

    def test_identifiers_kept_in_full(self):
        Product.objects.bulk_create([Product(sku=f"BULK-{i:04d}", name="Bulk") for i in range(600)])
        self.client.get("/api/v1/products/?page_size=500")
        row = self.last()
        self.assertEqual((row.items, len(row.object_ids)), (500, 500))
        self.assertTrue(all(i.startswith("sku:") for i in row.object_ids))
        self.assertIn("… +490", row.objects_text)                                     # коротко — як раніше
        response = SimpleNamespace(status_code=200, data={"results": [{"sku": f"X{i}"} for i in range(2100)]})
        request = RequestFactory().get("/api/v1/stock/")
        row = key_limits.record(request, response, 0, key=self.key)
        self.assertEqual((row.items, len(row.object_ids)), (2100, key_limits.OBJECTS_MAX))
        page = self.admin.get(f"/admin/api/apirequestlog/{row.pk}/change/")
        self.assertContains(page, "ще 100 записів не збережено")


class AnalysisTests(InsightBase):
    def test_aggregates_page_and_csv(self):
        self.client.get("/api/v1/stock/?ordering=sku")
        self.client.get("/api/v1/stock/AMP-100/")
        self.client.post("/api/v1/orders/", ORDER, format="json")
        self.client.get("/api/v1/stock/NOPE/")
        qs = log_analysis.filtered({"key": str(self.key.pk), "days": "7"})
        start, end, _ = log_analysis.period({"days": "7"})
        a = log_analysis.analyse(qs, start, end)
        self.assertEqual((a["totals"]["n"], a["totals"]["reads"], a["totals"]["writes"], a["totals"]["errors"]),
                         (4, 3, 1, 1))
        self.assertEqual(a["totals"]["sent"], 3)                                      # 2 у списку + 1 картка
        records = {(r["entity"], r["value"]): r for r in a["records"]["rows"]}
        amp = records[("product", "AMP-100")]
        self.assertEqual(amp["n"], 2)
        self.assertEqual(amp["url"], f"/admin/inventory/product/{self.p1.pk}/change/")
        self.assertIn(("order", "WS-1001"), records)
        self.assertEqual(len(a["writes"]), 1)
        self.assertIn("створив замовлення WS-1001", a["writes"][0].plain)
        self.assertEqual(a["errors"][0]["status"], 404)
        self.assertEqual(sum(d["reads"] + d["writes"] + d["errors"] for d in a["days"]), 4)

        page = self.admin.get(f"/admin/api/apirequestlog/analysis/?key={self.key.pk}&days=7")
        self.assertContains(page, "Аналіз журналу API — webshop")
        self.assertContains(page, "AMP-100")
        self.assertContains(page, "chart.umd.min.js")
        self.assertEqual(self.admin.get("/admin/api/apirequestlog/analysis/?key=unknown&days=1").status_code, 200)
        csv_ = self.admin.get(f"/admin/api/apirequestlog/analysis/?key={self.key.pk}&days=7&export=csv")
        self.assertEqual(csv_["Content-Type"], "text/csv; charset=utf-8")
        lines = csv_.content.decode("utf-8-sig").strip().splitlines()
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith("Коли;Ключ;IP"))
        self.assertIn("sku:AMP-100", "\n".join(lines))
        only_writes = self.admin.get(f"/admin/api/apirequestlog/analysis/?key={self.key.pk}&days=7&rw=write&export=csv")
        self.assertEqual(len(only_writes.content.decode("utf-8-sig").strip().splitlines()), 2)
        self.client.get("/api/v1/ping/", HTTP_USER_AGENT="=HYPERLINK(\"https://evil.example\")")
        csv_ = self.admin.get(f"/admin/api/apirequestlog/analysis/?key={self.key.pk}&days=1&export=csv")
        self.assertIn("'=HYPERLINK", csv_.content.decode("utf-8-sig"))                 # не формула в Excel

    def test_period(self):
        today = datetime(2026, 10, 30).date()
        self.assertEqual(log_analysis.period({"days": "1"}, today)[:2], (today, today))
        self.assertEqual(log_analysis.period({"days": "30"}, today)[0], today - timedelta(days=29))
        self.assertEqual(log_analysis.period({"from": "2026-10-01", "to": "2026-10-05"}, today)[2], 5)
        self.assertEqual(log_analysis.period({"from": "bad"}, today)[2], 7)


class PermissionTests(InsightBase):
    def test_view_only_staff_and_outsiders(self):
        vera = person("vera", allowed_modules=["api"], module_operations={"api": ["view"]})
        self.assertTrue(vera.has_perm("api.view_apikey"))
        self.assertFalse(vera.has_perm("api.change_apikey"))
        c = Client()
        c.force_login(vera)
        page = c.get(f"/admin/api/apikey/{self.key.pk}/insight/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'id="ki-pv-form"')                                # без права — без перегляду
        self.assertEqual(c.post(f"/admin/api/apikey/{self.key.pk}/insight/preview/",
                                {"path": "/api/v1/stock/"}).status_code, 403)
        self.assertEqual(c.post(f"/admin/api/apikey/{self.key.pk}/insight/simulate/",
                                {"method": "POST", "path": "/api/v1/orders/", "body": "{}"}).status_code, 403)
        self.assertEqual(c.get("/admin/api/apirequestlog/analysis/").status_code, 200)

        outsider = User.objects.create_user("max", "max@x.y", "p")
        type(outsider.profile).objects.filter(pk=outsider.profile.pk).update(allowed_modules=["api"])
        User.objects.filter(pk=outsider.pk).update(is_staff=False)     # не співробітник (сигнал профілю — в обхід)
        outsider = User.objects.get(pk=outsider.pk)
        c = Client()
        c.force_login(outsider)
        for url in (f"/admin/api/apikey/{self.key.pk}/insight/", "/admin/api/apirequestlog/analysis/"):
            r = c.get(url)
            self.assertEqual(r.status_code, 302)
            self.assertIn("/login/", r["Location"])
        r = c.post(f"/admin/api/apikey/{self.key.pk}/insight/preview/", {"path": "/api/v1/stock/"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Client().get(f"/admin/api/apikey/{self.key.pk}/insight/").status_code, 302)
