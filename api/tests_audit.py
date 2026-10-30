"""Аудит безпеки API (жовтень 2026): ключ сайту магазину, ціни нижче мінімальної кількості, вебхуки (права й SSRF),
сесія адмінки в API, «записів за добу» для /stock/check/, IP за проксі.

python manage.py test api.tests_audit --settings=tabele.settings_test
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings
from rest_framework.test import APIClient

from api import webhooks
from api.models import APIKey, Webhook, WebhookDelivery
from inventory.models import InventorySettings, Location, Product
from inventory.services.stock import create_movement
from sales.models import SalesOrder


def person(username, role="manager", **profile):
    user = User.objects.create_user(username, f"{username}@x.y", "p", is_staff=True)
    user.profile.role = role
    for k, v in profile.items():
        setattr(user.profile, k, v)
    user.profile.save()
    return User.objects.get(pk=user.pk)


class _Resp:
    def __init__(self, status=200, text="ok"):
        self.status_code, self.text = status, text


class ShopKeyTests(TestCase):
    def setUp(self):
        from shop.models import Shop, ShopListing
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.p = Product.objects.create(sku="AN-1", name="Antenne", sale_price=Decimal("20.00"),
                                        purchase_price=Decimal("7.00"))
        self.cable = Product.objects.create(sku="CA-1", name="Kabel", unit_type="meter", sale_price=Decimal("3.00"))
        self.secret = Product.objects.create(sku="SECRET-9", name="Intern", purchase_price=Decimal("1.00"))
        create_movement(product=self.p, tx_type="Incoming", qty=50)
        self.shop = Shop.objects.create(name="Sevskiy", slug="webshop", url="https://web.example.com")
        ShopListing.objects.create(shop=self.shop, product=self.p)
        ShopListing.objects.create(shop=self.shop, product=self.cable)
        self.key = APIKey.objects.create(name="site", shop=self.shop,
                                         scopes=["orders:read", "orders:write", "products:read"])
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION=f"Token {self.key.key}")
        self.dk = SalesOrder.objects.create(source="digikey", order_number="DK-1", email="buyer@dk.example",
                                            total_price=Decimal("500"))

    def own_order(self, number="WS-1", lines=None):
        r = self.c.post("/api/v1/orders/", {"order_number": number, "client": "Firma", "email": "a@b.de",
                                            "shop": True, "lines": lines or [{"sku": "AN-1", "qty": 2}]},
                        format="json")
        return r

    def test_site_key_sees_and_changes_only_own_orders(self):
        r = self.own_order()
        self.assertEqual(r.status_code, 201, r.content)
        oid = r.json()["id"]
        ids = [o["id"] for o in self.c.get("/api/v1/orders/").json()["results"]]
        self.assertEqual(ids, [oid])                                            # DigiKey-замовлення не видно
        self.assertEqual(self.c.get(f"/api/v1/orders/{self.dk.pk}/").status_code, 404)
        self.assertEqual(self.c.patch(f"/api/v1/orders/{self.dk.pk}/", {"status": "cancelled"},
                                      format="json").status_code, 404)
        self.assertEqual(self.c.post(f"/api/v1/orders/{self.dk.pk}/cancel/").status_code, 404)
        self.dk.refresh_from_db()
        self.assertEqual(self.dk.status, SalesOrder._meta.get_field("status").default)

    def test_site_key_patch_only_payment_fields(self):
        oid = self.own_order().json()["id"]
        r = self.c.patch(f"/api/v1/orders/{oid}/", {"payment_status": "paid", "payment_reference": "PP-1",
                                                    "total_price": "1.00", "affects_stock": False}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertEqual((d["payment_status"], d["payment_reference"]), ("paid", "PP-1"))
        self.assertEqual(d["ignored_fields"], ["affects_stock", "total_price"])
        order = SalesOrder.objects.get(pk=oid)
        self.assertEqual((order.total_price, order.affects_stock), (Decimal("40.00"), True))

    def test_site_key_products_only_shop_without_purchase_price(self):
        items = self.c.get("/api/v1/products/").json()["results"]
        self.assertEqual(sorted(i["sku"] for i in items), ["AN-1", "CA-1"])
        self.assertNotIn("purchase_price", items[0])
        general = APIKey.objects.create(name="crm", scopes=["products:read"])       # ключ без магазину — як раніше
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Token {general.key}")
        items = c.get("/api/v1/products/").json()["results"]
        self.assertEqual(len(items), 3)
        self.assertIn("purchase_price", items[0])

    def test_quantity_below_first_price_tier_rejected(self):
        r = self.own_order("WS-2", [{"sku": "CA-1", "qty": 0.5}])        # перша ціна — від 1 м
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.json()["lines"][0]["code"], "below_min_qty")
        self.assertFalse(SalesOrder.objects.filter(order_number__contains="WS-2").exists())


class WebhookAuditTests(TestCase):
    def setUp(self):
        from shop.models import Shop, ShopListing
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.p = Product.objects.create(sku="AN-1", name="Antenne", sale_price=Decimal("20"))
        self.other = Product.objects.create(sku="INT-1", name="Intern")
        self.shop = Shop.objects.create(name="Sevskiy", slug="webshop", url="https://web.example.com")
        ShopListing.objects.create(shop=self.shop, product=self.p)

    def client_for(self, key):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        return c

    def test_events_need_scopes_and_public_urls(self):
        key = APIKey.objects.create(name="hooks", scopes=["webhooks:read", "webhooks:write"])
        c = self.client_for(key)
        r = c.post("/api/v1/webhooks/", {"name": "x", "url": "https://hooks.example/a", "events": ["order.created"]},
                   format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn("orders:read", str(r.json()))
        for url in ("http://127.0.0.1:9/", "http://169.254.169.254/latest", "http://10.1.2.3/x",
                    "http://scraper-worker:8888/run", "http://localhost/x"):
            r = c.post("/api/v1/webhooks/", {"name": "x", "url": url, "events": []}, format="json")
            self.assertEqual(r.status_code, 400, (url, r.content))
        r = c.post("/api/v1/webhooks/", {"name": "x", "url": "https://hooks.example/a", "events": []}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        hid = r.json()["id"]
        with mock.patch("requests.post", return_value=_Resp(200, "internal answer")):
            self.assertEqual(c.post(f"/api/v1/webhooks/{hid}/test/").status_code, 200)
        delivery = c.get(f"/api/v1/webhooks/{hid}/deliveries/").json()[0]
        self.assertNotIn("response_body", delivery)                   # тіло відповіді отримувача ключу не віддається
        sent = []
        with mock.patch("requests.post", side_effect=lambda *a, **k: sent.append(k) or _Resp()):
            webhooks.emit("order.created", {"id": 1}, source="digikey")
        self.assertEqual(sent, [])                                    # «усі події», але без orders:read — ні

    def test_shop_key_hook_gets_only_own_shop(self):
        key = APIKey.objects.create(name="site", shop=self.shop,
                                    scopes=["orders:read", "products:read", "webhooks:read", "webhooks:write"])
        r = self.client_for(key).post("/api/v1/webhooks/", {"name": "site", "url": "https://web.example.com/h",
                                                            "events": []}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        hook = Webhook.objects.get(pk=r.json()["id"])
        self.assertEqual(hook.order_sources, "webshop")
        sent = []
        with mock.patch("requests.post", side_effect=lambda url, data, **k: sent.append(data) or _Resp()):
            webhooks.emit("stock.changed", {"items": [{"sku": "AN-1", "available": 3},
                                                       {"sku": "INT-1", "available": 9}]})
            webhooks.emit("stock.changed", {"items": [{"sku": "INT-1", "available": 9}]})
            webhooks.emit("purchase_order.updated", {"purchase_order": {}})
        self.assertEqual(len(sent), 1)
        self.assertIn('"AN-1"', sent[0].decode() if isinstance(sent[0], bytes) else sent[0])
        self.assertNotIn("INT-1", sent[0].decode() if isinstance(sent[0], bytes) else sent[0])

    def test_private_target_not_called_and_no_redirects(self):
        key = APIKey.objects.create(name="hooks", scopes=["webhooks:read", "webhooks:write", "orders:read"])
        bad = Webhook.objects.create(name="bad", url="http://10.0.0.5/x", api_key=key)   # обхід перевірки форми
        admin_hook = Webhook.objects.create(name="lan crm", url="http://192.168.2.50/hook")  # адміністратор — можна
        calls = []
        with mock.patch("requests.post", side_effect=lambda url, data, **k: calls.append((url, k)) or _Resp()):
            webhooks.emit("order.created", {"id": 1}, source="manual")
        self.assertEqual([u for u, _ in calls], ["http://192.168.2.50/hook"])
        self.assertIs(calls[0][1]["allow_redirects"], False)
        d = WebhookDelivery.objects.get(webhook=bad)
        self.assertEqual(d.status, WebhookDelivery.FAILED)
        self.assertIn("10.0.0.5", d.response_body)
        self.assertTrue(admin_hook.deliveries.filter(status=WebhookDelivery.SUCCESS).exists())

    def test_session_needs_view_right_and_secret_masked(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        Webhook.objects.create(name="h", url="https://hooks.example/a", secret="whsec_supersecret_value_123")
        c = APIClient()
        c.force_login(person("reader", "readonly"))
        self.assertEqual(c.get("/api/v1/customers/").status_code, 403)
        c.force_login(person("olga"))                                   # менеджер: CRM — так, API — ні
        self.assertEqual(c.get("/api/v1/customers/").status_code, 200)
        self.assertEqual(c.get("/api/v1/webhooks/").status_code, 403)
        c.force_login(person("boss", "admin"))
        data = c.get("/api/v1/webhooks/").json()
        self.assertNotIn("whsec_supersecret_value_123", str(data))


class KeyLimitAuditTests(TestCase):
    def setUp(self):
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        for i in range(4):
            Product.objects.create(sku=f"P-{i}", name=f"P{i}")

    def test_stock_check_respects_items_per_day_and_fields(self):
        key = APIKey.objects.create(name="lim", scopes=["stock:read"], items_per_day=1, fields="sku")
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        r = c.post("/api/v1/stock/check/", {"items": [{"sku": f"P-{i}", "qty": 1} for i in range(4)]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        items = r.json()["items"]
        self.assertEqual(items, [{"sku": "P-0"}])

    def test_allowed_ips_cannot_be_spoofed_from_outside(self):
        key = APIKey.objects.create(name="ip", scopes=["stock:read"], allowed_ips="203.0.113.5")
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        self.assertEqual(c.get("/api/v1/stock/", REMOTE_ADDR="198.51.100.9",
                               HTTP_X_REAL_IP="203.0.113.5").status_code, 401)
        self.assertEqual(c.get("/api/v1/stock/", REMOTE_ADDR="172.17.0.1",          # наш проксі (Docker)
                               HTTP_X_REAL_IP="203.0.113.5").status_code, 200)


class ClientIpTests(TestCase):
    def test_headers_only_from_trusted_proxy(self):
        from core.utils import client_ip
        rf = RequestFactory()
        self.assertEqual(client_ip(rf.get("/", REMOTE_ADDR="198.51.100.9", HTTP_X_REAL_IP="1.2.3.4")),
                         "198.51.100.9")
        self.assertEqual(client_ip(rf.get("/", REMOTE_ADDR="172.18.0.1", HTTP_X_REAL_IP="1.2.3.4")), "1.2.3.4")
        self.assertEqual(client_ip(rf.get("/", REMOTE_ADDR="127.0.0.1",
                                          HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.9")), "203.0.113.9")
        with override_settings(TRUSTED_PROXIES=["127.0.0.1/32"]):
            self.assertEqual(client_ip(rf.get("/", REMOTE_ADDR="172.18.0.1", HTTP_X_REAL_IP="1.2.3.4")),
                             "172.18.0.1")
