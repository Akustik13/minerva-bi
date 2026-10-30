"""Закупівлі зовнішньої CRM через REST API (/purchase-orders/, inventory/services/purchases.py).
python manage.py test api --settings=tabele.settings_test"""
import importlib.util
import io
import json
import os
import shutil
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

from django.core.cache import cache

from api.models import Webhook
from inventory.models import IncomingShipment, InventorySettings, PurchaseOrder, PurchaseReceipt, Supplier

from .tests import ALL_SCOPES, APITestBase

URL = "/api/v1/purchase-orders/"
ORDER = {
    "external_id": "B-2026-0042",
    "supplier": {"name": "Acme Components GmbH", "email": "sales@acme.example", "country": "de"},
    "supplier_order_no": "AB-7781",
    "order_date": "2026-10-20",
    "expected_date": "2026-11-05",
    "currency": "EUR",
    "lines": [{"line_id": "1", "sku": "AMP-100", "qty": 10, "unit_price": "12.50"},
              {"line_id": "2", "sku": "CABLE-M", "qty": 25}],
}


class PurchaseBase(APITestBase):
    def setUp(self):
        super().setUp()
        cache.clear()   # ліміт запитів ключа (120/хв) — pk ключа повторюється між тестами
        self.key.scopes = ALL_SCOPES + ["purchases:read", "purchases:write"]
        self.key.default_source = "crm"
        self.key.save()

    def post(self, body=ORDER, url=URL):
        return self.client.post(url, body, format="json")

    def stock(self, sku="AMP-100"):
        return self.client.get(f"/api/v1/stock/{sku}/").json()


class PurchaseAPITests(PurchaseBase):
    def test_create_and_repeat_updates_same_order(self):
        r = self.post()
        self.assertEqual(r.status_code, 201, r.content)
        po = r.json()
        self.assertEqual((po["source"], po["external_id"], po["status"]), ("crm", "B-2026-0042", "ordered"))
        self.assertEqual(po["supplier"]["name"], "Acme Components GmbH")
        self.assertEqual(po["supplier"]["country"], "DE")
        self.assertEqual(po["total"], 125)
        self.assertEqual([l["qty_open"] for l in po["lines"]], [10, 25])
        self.assertEqual(self.stock()["incoming"], 10)          # склад бачить «очікується»

        again = dict(ORDER, expected_date="2026-11-08",
                     lines=[{"line_id": "1", "sku": "AMP-100", "qty": 12}])
        r = self.post(again)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.json()["created"])
        self.assertEqual(PurchaseOrder.objects.count(), 1)
        self.assertEqual(Supplier.objects.count(), 1)
        self.assertEqual(r.json()["expected_date"], "2026-11-08")
        self.assertEqual([l["qty_ordered"] for l in r.json()["lines"]], [12, 25])   # не згадані — як були
        self.assertEqual(self.client.get(URL + "ext:B-2026-0042/").json()["id"], po["id"])
        self.assertEqual(self.client.get(URL + po["id"] + "/").json()["external_id"], "B-2026-0042")

    def test_validation(self):
        r = self.post(dict(ORDER, external_id=""))
        self.assertEqual((r.status_code, r.json()["code"]), (400, "external_id_required"))
        r = self.post({k: v for k, v in ORDER.items() if k != "supplier"})
        self.assertEqual(r.json()["code"], "supplier_required")
        self.assertEqual(PurchaseOrder.objects.count(), 0)               # відкотилось
        r = self.post(dict(ORDER, lines=[{"sku": "AMP-100", "qty": 1}, {"sku": "amp-100", "qty": 2}]))
        self.assertEqual(r.json()["code"], "duplicate_line")
        r = self.post(dict(ORDER, status="received"))
        self.assertEqual(r.json()["code"], "bad_status")

    def test_unknown_sku_warns_or_strict(self):
        body = dict(ORDER, lines=[{"sku": "NOT-IN-MINERVA", "qty": 3}])
        r = self.post(body)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["warnings"][0]["code"], "sku_not_found")
        self.assertFalse(r.json()["lines"][0]["in_minerva"])
        r = self.post(dict(body, external_id="B-2", strict=True))
        self.assertEqual((r.status_code, r.json()["code"]), (404, "sku_not_found"))

    def test_tracking_one_shipment_per_number(self):
        self.post(dict(ORDER, tracking_number="1Z999", carrier="UPS"))
        r = self.client.post(URL + "ext:B-2026-0042/shipments/", {
            "tracking_number": "1z999", "status": "customs", "expected_date": "2026-11-03"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(IncomingShipment.objects.count(), 1)
        self.assertEqual(r.json()["expected_date"], "2026-11-03")
        self.post(dict(ORDER, tracking_number="1Z999"))               # повтор замовлення без статусу
        self.assertEqual(IncomingShipment.objects.get().status, "customs")
        self.assertEqual(r.json()["shipments"][0]["carrier"], "UPS")

    def test_delivery_once_then_duplicate(self):
        self.post(dict(ORDER, tracking_number="1Z999"))
        r = self.client.post(URL + "ext:B-2026-0042/deliveries/", {
            "tracking_number": "1Z999", "delivered_at": "2026-11-04T10:15:00+01:00"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertFalse(body["delivery"]["duplicate"])
        self.assertEqual(body["status"], "received")
        self.assertEqual(body["received_date"], "2026-11-04")
        self.assertEqual(body["shipments"][0]["status"], "delivered")
        self.assertEqual(self.stock()["on_hand"], 10)
        self.assertEqual(self.stock()["incoming"], 0)
        r = self.client.post(URL + "ext:B-2026-0042/deliveries/", {"tracking_number": "1Z999"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["delivery"]["duplicate"])
        self.assertEqual(self.stock()["on_hand"], 10)                   # без дубля на складі
        self.assertEqual(PurchaseReceipt.objects.count(), 1)

    def test_partial_deliveries_and_over_receipt(self):
        self.post()
        part = {"delivery_id": "D-1", "lines": [{"line_id": "1", "qty": 4}]}
        r = self.client.post(URL + "ext:B-2026-0042/deliveries/", part, format="json")
        self.assertEqual((r.status_code, r.json()["status"]), (201, "partial"))
        self.assertTrue(self.client.post(URL + "ext:B-2026-0042/deliveries/", part,
                                         format="json").json()["delivery"]["duplicate"])
        self.assertEqual(self.stock()["on_hand"], 4)
        r = self.client.post(URL + "ext:B-2026-0042/deliveries/",
                             {"delivery_id": "D-2", "lines": [{"sku": "AMP-100", "qty": 7}]}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "over_receipt"))
        r = self.client.post(URL + "ext:B-2026-0042/deliveries/",
                             {"lines": [{"sku": "AMP-100", "qty": 6}]}, format="json")
        self.assertEqual(r.json()["code"], "delivery_id_required")
        r = self.client.post(URL + "ext:B-2026-0042/deliveries/",
                             {"delivery_id": "D-2", "allow_over": True,
                              "lines": [{"sku": "AMP-100", "qty": 7}, {"line_id": "2", "qty": 25}]}, format="json")
        self.assertEqual((r.status_code, r.json()["status"]), (201, "received"))
        self.assertEqual(self.stock()["on_hand"], 11)
        r = self.client.patch(URL + "ext:B-2026-0042/", {"lines": [{"line_id": "1", "qty": 5}]}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "qty_below_received"))

    def test_received_line_keeps_its_sku_and_over_receipt_keeps_incoming_positive(self):
        self.post()
        r = self.client.post(URL + "ext:B-2026-0042/deliveries/", {"delivery_id": "D-1", "allow_over": True,
                             "lines": [{"line_id": "1", "qty": 12}]}, format="json")
        self.assertEqual((r.status_code, r.json()["status"]), (201, "partial"))   # позиція 2 ще не прийшла
        self.assertEqual((self.stock()["on_hand"], self.stock()["incoming"]), (12, 0))   # не −2
        r = self.client.patch(URL + "ext:B-2026-0042/", {"lines": [{"line_id": "1", "sku": "CABLE-M", "qty": 12}]},
                              format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "line_received"))
        self.assertEqual(self.stock()["on_hand"], 12)                             # отримане лишилось при AMP-100
        self.assertEqual(self.client.get(URL + "ext:B-2026-0042/").json()["lines"][0]["sku"], "AMP-100")
        r = self.client.patch(URL + "ext:B-2026-0042/", {"lines": [{"line_id": "2", "sku": "AMP-100", "qty": 3}]},
                              format="json")
        self.assertEqual(r.status_code, 200, r.content)                           # не отримана — можна

    def test_arrived_mode_marks_only(self):
        s = InventorySettings.get()
        s.api_delivery_mode = InventorySettings.API_DELIVERY_ARRIVED
        s.save()
        self.post(dict(ORDER, tracking_number="1Z999"))
        r = self.client.post(URL + "deliveries/", {"external_id": "B-2026-0042", "tracking_number": "1Z999"},
                             format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertFalse(r.json()["delivery"]["booked"])
        self.assertEqual(r.json()["status"], "ordered")
        self.assertEqual(r.json()["shipments"][0]["status"], "arrived")
        self.assertIsNotNone(r.json()["delivered_reported_at"])
        self.assertEqual(self.stock()["on_hand"], 0)
        self.assertEqual(self.stock()["incoming"], 10)

    def test_external_id_with_slash_and_shipment_delivered(self):
        self.post(dict(ORDER, external_id="2026/42"))
        r = self.client.post(URL + "shipments/", {"external_id": "2026/42", "tracking_number": "DHL-1",
                                                   "status": "delivered"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "received")
        self.assertEqual(self.client.get(URL, {"external_id": "2026/42"}).json()["count"], 1)

    def test_minerva_orders_are_read_only(self):
        po = PurchaseOrder.objects.create(supplier=Supplier.objects.create(name="Own"), status="ordered")
        self.assertEqual(self.client.get(URL + po.code + "/").status_code, 200)
        r = self.client.patch(URL + po.code + "/", {"notes": "x"}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (403, "not_external"))

    def test_cancel_and_list_filters(self):
        self.post()
        self.post(dict(ORDER, external_id="B-2", lines=[{"sku": "CABLE-M", "qty": 5}]))
        r = self.client.post(URL + "ext:B-2/cancel/", {"reason": "supplier out of stock"}, format="json")
        self.assertEqual((r.json()["status"], r.json()["qty_open"]), ("cancelled", 0))
        self.assertEqual(self.stock("CABLE-M")["incoming"], 25)
        self.assertEqual(self.client.get(URL, {"open": "1"}).json()["count"], 1)
        self.assertEqual(self.client.get(URL, {"sku": "CABLE-M"}).json()["count"], 2)
        self.assertEqual(self.client.get(URL, {"changed_since": "2099-01-01"}).json()["count"], 0)
        self.assertEqual(self.client.get(URL, {"changed_since": "x"}).json()["code"], "bad_date")

    def test_scopes(self):
        self.key.scopes = ["stock:read"]
        self.key.save()
        self.assertEqual(self.client.get(URL).status_code, 403)
        self.key.scopes = ["purchases:read"]
        self.key.save()
        self.assertEqual(self.client.get(URL).status_code, 200)
        r = self.post()
        self.assertEqual(r.status_code, 403)
        self.assertIn("purchases:write", r.json()["detail"])


class _Resp:
    status_code, text = 200, "ok"


class PurchaseWebhookTests(PurchaseBase):
    def test_only_explicit_subscribers_get_purchase_events(self):
        sent = []

        def _post(url, data, headers, timeout, **kw):
            sent.append((url, json.loads(data.decode())))
            return _Resp()
        Webhook.objects.create(name="shop", url="https://shop.example/hook")                 # «усі події»
        Webhook.objects.create(name="crm", url="https://crm.example/hook", events=["purchase_order.updated"])
        with mock.patch("requests.post", side_effect=_post), self.captureOnCommitCallbacks(execute=True):
            self.post()
        self.assertEqual([u for u, _ in sent], ["https://crm.example/hook"])
        data = sent[0][1]["data"]
        self.assertEqual((data["changed_by"], data["purchase_order"]["external_id"]), ("crm", "B-2026-0042"))
        sent.clear()
        po = PurchaseOrder.objects.get()
        with mock.patch("requests.post", side_effect=_post), self.captureOnCommitCallbacks(execute=True):
            po.status = "partial"
            po.save()
        self.assertEqual(sent[0][1]["data"]["changed_by"], "minerva")
        sent.clear()
        with mock.patch("requests.post", side_effect=_post), self.captureOnCommitCallbacks(execute=True):
            po.notes = "only notes"
            po.save()
        self.assertEqual(sent, [])                                    # не відстежуване поле — без події

    def test_subscribe_needs_purchases_scope(self):
        self.key.scopes = ALL_SCOPES + ["webhooks:write"]
        self.key.save()
        r = self.client.post("/api/v1/webhooks/", {"name": "x", "url": "https://x.example/h",
                                                   "events": ["purchase_order.updated"]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.key.scopes += ["purchases:read"]
        self.key.save()
        r = self.client.post("/api/v1/webhooks/", {"name": "x", "url": "https://x.example/h",
                                                   "events": ["purchase_order.updated"]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(Webhook.objects.get().wants("purchase_order.updated"))
        self.assertFalse(Webhook(url="https://y.example").wants("purchase_order.updated"))


class PurchaseAdminTests(PurchaseBase):
    def test_admin_pages(self):
        from django.contrib.auth.models import User
        from config.models import SystemSettings
        self.post(dict(ORDER, tracking_number="1Z999"))
        self.client.post(URL + "ext:B-2026-0042/deliveries/", {"tracking_number": "1Z999"}, format="json")
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        po = PurchaseOrder.objects.get()
        page = self.client.get(f"/admin/inventory/purchaseorder/{po.pk}/change/")
        self.assertContains(page, "tracking:1z999")
        self.assertContains(page, "AMP-100 × 10,")
        self.assertContains(self.client.get("/admin/inventory/purchaseorder/"), "🔗 crm · B-2026-0042")
        self.assertContains(self.client.get("/admin/inventory/inventorysettings/1/change/"), "api_delivery_mode")
        self.assertContains(self.client.get("/dashboard/api/docs/examples/crm_purchases.py"), "def send_delivered")
        docs = self.client.get("/dashboard/api/docs/")
        self.assertContains(docs, "інтеграція магазину і CRM")
        self.assertContains(docs, "/dashboard/api/docs/examples/crm_purchases.py")    # посилання з README
        self.assertContains(docs, "purchase-orders/ext:B-TEST-1/deliveries/")


class _ClientResp:
    def __init__(self, r):
        self.status_code, self.headers, self.text, self._r = r.status_code, r.headers, r.content.decode(), r

    def json(self):
        return self._r.json()


class _ClientSession:
    """requests.Session прикладу → тестовий клієнт Django (без мережі)."""
    def __init__(self, client):
        self.client = client

    def request(self, method, url, timeout=None, json=None, params=None):
        parts = urlsplit(url)
        if method == "GET":
            return _ClientResp(self.client.get(parts.path, {**dict(parse_qsl(parts.query)), **(params or {})}))
        return _ClientResp(getattr(self.client, method.lower())(parts.path, json or {}, format="json"))


class CRMExampleTests(PurchaseBase):
    """api/examples/crm_purchases.py проти справжнього API: приклад із документації не розходиться з кодом."""
    def example(self):
        spec = importlib.util.spec_from_file_location(
            "crm_purchases_example", Path(__file__).resolve().parent / "examples" / "crm_purchases.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.session = _ClientSession(self.client)
        mod.STATE_FILE = os.path.join(tempfile.mkdtemp(), ".last_purchase_sync")
        self.addCleanup(shutil.rmtree, os.path.dirname(mod.STATE_FILE), True)
        return mod

    def run_example(self, mod, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            mod.main(*args)
        return out.getvalue()

    def test_demo_cycle(self):
        out = self.run_example(self.example(), "demo", "AMP-100", "--yes")
        self.assertIn("created=True", out)
        self.assertIn("created=False — дубля немає", out)
        self.assertIn("duplicate=False, booked=True", out)
        self.assertIn("duplicate=True — на склад нічого не додано", out)
        self.assertIn("409 over_receipt", out)
        po = PurchaseOrder.objects.get()
        self.assertEqual((po.external_source, po.status), ("crm", "received"))
        self.assertEqual(PurchaseReceipt.objects.count(), 2)              # LS-1 і LS-2, повтор і 409 — ні
        self.assertEqual(self.stock()["on_hand"], 10)
        self.assertIn("AMP-100: на складі 10, очікується 0", out)

    def test_commands_and_sync(self):
        mod = self.example()
        self.run_example(mod, "order", "2026/42", "AMP-100:3", "CABLE-M:5")
        self.run_example(mod, "track", "2026/42", "1Z777", "UPS", "2026-11-05")
        self.run_example(mod, "delivered", "2026/42", "LS-9", "AMP-100:3")
        self.run_example(mod, "delivered", "2026/42", "LS-9", "AMP-100:3")     # повтор
        po = PurchaseOrder.objects.get(external_id="2026/42")
        self.assertEqual((po.status, str(po.expected_date)), ("partial", "2026-11-05"))
        self.assertEqual(po.incoming_shipments.get().tracking_number, "1Z777")
        self.assertEqual(self.stock()["on_hand"], 3)
        self.assertIn('"external_id": "2026/42"', self.run_example(mod, "get", "2026/42"))

        self.assertIn("Змінених закупівель: 1 (з початку)", self.run_example(mod, "sync"))
        self.assertIn("Змінених закупівель: 0", self.run_example(mod, "sync"))   # лише змінене після минулого

        self.run_example(mod, "order", "B-7", "CABLE-M:2")
        self.run_example(mod, "cancel", "B-7", "Lieferant storniert")
        self.assertEqual(PurchaseOrder.objects.get(external_id="B-7").status, "cancelled")
