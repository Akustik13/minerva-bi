"""
Тести REST API.  Запуск:  python manage.py test api
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from api.models import APIKey
from inventory.models import (
    InventorySettings, InventoryTransaction, Location, Product,
)
from sales.models import SalesOrder

ALL_SCOPES = ["orders:read", "orders:write", "products:read", "products:write",
              "stock:read", "stock:write", "shipments:read"]


class APITestBase(TestCase):
    def setUp(self):
        s = InventorySettings.get()
        s.deduct_on = InventorySettings.DeductOn.CREATION
        s.use_reservation = False
        s.allow_negative_stock = True
        s.default_location = "MAIN"
        s.save()
        self.loc = Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.p1 = Product.objects.create(sku="AMP-100", name="Amplifier", sale_price=Decimal("99.00"),
                                         reorder_point=5)
        self.p2 = Product.objects.create(sku="CABLE-M", name="Cable", unit_type="meter")
        self.key = APIKey.objects.create(name="webshop", scopes=ALL_SCOPES, default_source="webshop")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.key.key}")

    def receive(self, product, qty):
        return self.client.post("/api/v1/stock-movements/", {
            "sku": product.sku, "tx_type": "Incoming", "qty": qty}, format="json")


class AuthTests(APITestBase):
    def test_ping(self):
        r = self.client.get("/api/v1/ping/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["key"], "webshop")

    def test_no_token(self):
        self.assertEqual(APIClient().get("/api/v1/stock/").status_code, 401)

    def test_missing_scope(self):
        self.key.scopes = ["stock:read"]
        self.key.save()
        r = self.receive(self.p1, 1)
        self.assertEqual(r.status_code, 403)
        self.assertIn("stock:write", r.json()["detail"])
        # check — POST, але потребує лише stock:read
        r = self.client.post("/api/v1/stock/check/", {"items": [{"sku": "AMP-100", "qty": 1}]}, format="json")
        self.assertEqual(r.status_code, 200)


class StockTests(APITestBase):
    def test_incoming_and_stock(self):
        r = self.receive(self.p1, 10)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["available_after"], 10)

        r = self.client.get("/api/v1/stock/AMP-100/")
        d = r.json()
        self.assertEqual((d["on_hand"], d["available"], d["in_stock"], d["low_stock"]), (10, 10, True, False))
        self.assertEqual(d["locations"][0]["location"], "MAIN")

    def test_idempotent_movement(self):
        body = {"sku": "AMP-100", "tx_type": "Incoming", "qty": 3, "external_key": "grn-1"}
        self.assertEqual(self.client.post("/api/v1/stock-movements/", body, format="json").status_code, 201)
        self.assertEqual(self.client.post("/api/v1/stock-movements/", body, format="json").status_code, 200)
        self.assertEqual(InventoryTransaction.objects.filter(product=self.p1).count(), 1)

    def test_fractional_rules(self):
        r = self.client.post("/api/v1/stock-movements/",
                             {"sku": "AMP-100", "tx_type": "Incoming", "qty": "1.5"}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/v1/stock-movements/",
                             {"sku": "CABLE-M", "tx_type": "Incoming", "qty": "12.5"}, format="json")
        self.assertEqual(r.status_code, 201)

    def test_negative_stock_blocked(self):
        s = InventorySettings.get()
        s.allow_negative_stock = False
        s.save()
        r = self.client.post("/api/v1/stock-movements/",
                             {"sku": "AMP-100", "tx_type": "Outgoing", "qty": 1}, format="json")
        self.assertEqual(r.status_code, 409)

    def test_count(self):
        self.receive(self.p1, 10)
        r = self.client.post("/api/v1/stock/count/", [{"sku": "AMP-100", "qty": 7}], format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["results"][0]["delta"], -3)
        self.assertEqual(self.client.get("/api/v1/stock/AMP-100/").json()["on_hand"], 7)

    def test_filters_and_check(self):
        self.receive(self.p1, 3)  # нижче reorder_point=5
        r = self.client.get("/api/v1/stock/?low_stock=true")
        self.assertEqual([x["sku"] for x in r.json()["results"]], ["AMP-100"])
        r = self.client.get("/api/v1/stock/?in_stock=false")
        self.assertEqual([x["sku"] for x in r.json()["results"]], ["CABLE-M"])
        r = self.client.post("/api/v1/stock/check/", {"items": [
            {"sku": "AMP-100", "qty": 2}, {"sku": "NOPE", "qty": 1}]}, format="json")
        self.assertFalse(r.json()["ok"])
        self.assertEqual([i["ok"] for i in r.json()["items"]], [True, False])

    def test_products_with_stock(self):
        self.receive(self.p1, 4)
        r = self.client.get("/api/v1/products/?with_stock=1&sku=AMP-100")
        self.assertEqual(r.json()["results"][0]["available"], 4)


class OrderTests(APITestBase):
    ORDER = {
        "order_number": "WS-1001",
        "client": "Max Mustermann",
        "email": "max@example.com",
        "addr_street": "Hauptstr. 1", "addr_city": "Berlin", "addr_zip": "10115", "addr_country": "de",
        "currency": "EUR",
        "lines": [{"sku": "AMP-100", "qty": 2}],
    }

    def test_create_order_deducts_stock(self):
        self.receive(self.p1, 10)
        r = self.client.post("/api/v1/orders/", self.ORDER, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        d = r.json()
        self.assertTrue(d["created"])
        self.assertEqual(d["source"], "webshop")
        self.assertEqual(d["addr_country"], "DE")
        self.assertEqual(d["total_price"], 198.0)
        self.assertEqual(d["lines"][0]["product_sku"], "AMP-100")
        self.assertEqual(self.client.get("/api/v1/stock/AMP-100/").json()["available"], 8)

        # Повтор (ретрай вебхука) не дублює замовлення
        r = self.client.post("/api/v1/orders/", self.ORDER, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["created"])
        self.assertEqual(SalesOrder.objects.count(), 1)

    def test_unknown_sku(self):
        body = dict(self.ORDER, lines=[{"sku": "NOPE", "qty": 1}])
        r = self.client.post("/api/v1/orders/", body, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "invalid_lines")

    def test_check_stock_rejects(self):
        body = dict(self.ORDER, check_stock=True)
        r = self.client.post("/api/v1/orders/", body, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(SalesOrder.objects.count(), 0)

    def test_cancel_restocks(self):
        self.receive(self.p1, 10)
        oid = self.client.post("/api/v1/orders/", self.ORDER, format="json").json()["id"]
        r = self.client.post(f"/api/v1/orders/{oid}/cancel/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "cancelled")
        self.assertEqual(self.client.get("/api/v1/stock/AMP-100/").json()["available"], 10)
        # повторне скасування не повертає товар вдруге
        self.client.post(f"/api/v1/orders/{oid}/cancel/", {}, format="json")
        self.assertEqual(self.client.get("/api/v1/stock/AMP-100/").json()["available"], 10)

    def test_lookup_by_number_and_shipments(self):
        self.client.post("/api/v1/orders/", self.ORDER, format="json")
        r = self.client.get("/api/v1/orders/?order_number=WS-1001")
        self.assertEqual(r.json()["count"], 1)
        r = self.client.get("/api/v1/shipments/?order_number=WS-1001")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 0)


class PaginationTests(APITestBase):
    def test_page_size(self):
        r = self.client.get("/api/v1/stock/?page_size=1")
        self.assertEqual(len(r.json()["results"]), 1)
        self.assertIsNotNone(r.json()["next"])


# ── Вебхуки ───────────────────────────────────────────────────────────────────

import json
from unittest import mock

from api.models import Webhook, WebhookDelivery
from api import webhooks


class _Resp:
    def __init__(self, code):
        self.status_code, self.text = code, "ok"


class WebhookTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.key.scopes = ALL_SCOPES + ["webhooks:read", "webhooks:write"]
        self.key.save()
        self.sent = []

    def fake_post(self, code=200):
        def _post(url, data, headers, timeout):
            self.sent.append({"url": url, "body": data.decode(), "headers": headers})
            return _Resp(code)
        return mock.patch("requests.post", side_effect=_post)

    def register(self, **kw):
        body = {"name": "shop", "url": "https://shop.example/hook", **kw}
        r = self.client.post("/api/v1/webhooks/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def test_register_and_ping_signed(self):
        hook = self.register()
        self.assertTrue(hook["secret"].startswith("whsec_"))
        with self.fake_post():
            r = self.client.post(f"/api/v1/webhooks/{hook['id']}/test/")
        self.assertEqual(r.status_code, 200, r.content)
        sent = self.sent[0]
        self.assertEqual(sent["headers"]["X-Minerva-Event"], "ping")
        self.assertTrue(webhooks.verify(hook["secret"], sent["body"], sent["headers"]["X-Minerva-Signature"]))
        self.assertFalse(webhooks.verify("wrong", sent["body"], sent["headers"]["X-Minerva-Signature"]))

    def test_stock_changed_batched_per_transaction(self):
        self.register(events=["stock.changed"])
        with self.fake_post(), self.captureOnCommitCallbacks(execute=True):
            r = self.client.post("/api/v1/stock/count/", [
                {"sku": "AMP-100", "qty": 7}, {"sku": "CABLE-M", "qty": 3}], format="json")
            self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.sent), 1)  # одна подія на транзакцію
        items = json.loads(self.sent[0]["body"])["data"]["items"]
        self.assertEqual({i["sku"]: i["available"] for i in items}, {"AMP-100": 7, "CABLE-M": 3})

    def test_order_events_and_source_filter(self):
        self.register(events=["order.created", "order.status_changed"], order_sources="webshop")
        with self.fake_post(), self.captureOnCommitCallbacks(execute=True):
            oid = self.client.post("/api/v1/orders/", OrderTests.ORDER, format="json").json()["id"]
        with self.fake_post(), self.captureOnCommitCallbacks(execute=True):
            self.client.patch(f"/api/v1/orders/{oid}/", {"status": "processing"}, format="json")
        events = [json.loads(s["body"]) for s in self.sent]
        self.assertEqual([e["event"] for e in events], ["order.created", "order.status_changed"])
        self.assertEqual(events[1]["data"]["previous_status"], "received")
        # замовлення з іншого джерела — без вебхука
        with self.fake_post(), self.captureOnCommitCallbacks(execute=True):
            self.client.post("/api/v1/orders/", dict(OrderTests.ORDER, source="digikey"), format="json")
        self.assertEqual(len(self.sent), 2)

    def test_retry_then_fail(self):
        hook = Webhook.objects.create(name="h", url="https://shop.example/hook")
        with self.fake_post(500):
            (d,) = webhooks.emit("ping", {}, only=hook)
        d.refresh_from_db()
        self.assertEqual((d.status, d.attempts), ("pending", 1))
        self.assertGreater(d.next_attempt_at, d.created_at)
        WebhookDelivery.objects.update(next_attempt_at=d.created_at)
        with self.fake_post(200):
            self.assertEqual(webhooks.send_due(), (1, 0))
        d.refresh_from_db()
        self.assertEqual(d.status, "success")

    def test_key_sees_only_own_webhooks(self):
        Webhook.objects.create(name="admin-hook", url="https://x.example/")
        self.register()
        self.assertEqual([h["name"] for h in self.client.get("/api/v1/webhooks/").json()], ["shop"])


class CorsTests(APITestBase):
    def test_preflight_and_headers(self):
        r = APIClient().options("/api/v1/stock/", HTTP_ORIGIN="null",
                                HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
                                HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization")
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r["Access-Control-Allow-Origin"], "*")
        self.assertIn("Authorization", r["Access-Control-Allow-Headers"])
        self.assertNotIn("Access-Control-Allow-Credentials", r)
        r = self.client.get("/api/v1/ping/", HTTP_ORIGIN="https://shop.example")
        self.assertEqual(r["Access-Control-Allow-Origin"], "*")

    def test_no_cors_outside_api(self):
        r = APIClient().get("/admin/login/", HTTP_ORIGIN="https://shop.example")
        self.assertFalse(r.has_header("Access-Control-Allow-Origin"))
