"""Тести онлайн-оплати Stripe (Stripe API імітується):  python manage.py test shop --settings=tabele.settings_test"""
import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from api.models import APIKey
from inventory.models import InventorySettings, Location, Product
from inventory.services.stock import create_movement
from sales.models import SalesOrder

from . import payments
from .models import Shop, ShopListing, ShopPayment

SECRET, HOOK = "sk_test_local_dummy", "whsec_local_dummy"
SITE = "https://web.example.com/dist/de/shop/kasse/index.html"


class FakeStripe:
    """Мінімальна імітація Stripe API: сесії Checkout, expire, account."""

    def __init__(self):
        self.sessions, self.calls = {}, []

    def __call__(self, method, url, data=None, headers=None, timeout=None):
        path = url.split("://", 1)[1].split("/", 1)[1]
        form = dict(data or [])
        self.calls.append((method, "/" + path, form, headers or {}))
        resp = mock.Mock()
        if method == "POST" and path == "v1/checkout/sessions":
            sid = f"cs_test_{len(self.sessions) + 1}"
            s = {"id": sid, "object": "checkout.session", "url": f"https://checkout.stripe.test/{sid}",
                 "status": "open", "payment_status": "unpaid", "livemode": False,
                 "expires_at": int(time.time()) + 86400, "payment_intent": None,
                 "amount_total": int(form["line_items[0][price_data][unit_amount]"])}
            self.sessions[sid] = s
            resp.status_code, resp.json.return_value = 200, s
        elif method == "POST" and path.endswith("/expire"):
            s = self.sessions[path.split("/")[3]]
            s["status"] = "expired"
            resp.status_code, resp.json.return_value = 200, s
        elif method == "GET" and path.startswith("v1/checkout/sessions/"):
            resp.status_code, resp.json.return_value = 200, self.sessions[path.split("/")[3]]
        elif method == "GET" and path == "v1/account":
            resp.status_code, resp.json.return_value = 200, {"id": "acct_1", "settings": {"dashboard": {"display_name": "Sevskiy"}}}
        else:
            resp.status_code, resp.json.return_value = 404, {"error": {"message": "not found"}}
        return resp


def signed(payload: dict, secret=HOOK, stamp=None):
    body = json.dumps(payload).encode()
    t = int(stamp or time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={t},v1={sig}"


class StripeTestCase(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.addCleanup(cache.clear)
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.shop = Shop.objects.create(name="Sevskiy", slug="webshop", is_default=True, url="https://web.example.com",
                                        stripe_enabled=True, stripe_secret_key=SECRET, stripe_webhook_secret=HOOK)
        p = Product.objects.create(sku="AN-1", name="Antenne", sale_price=Decimal("20.00"))
        create_movement(product=p, tx_type="Incoming", qty=100)
        ShopListing.objects.create(shop=self.shop, product=p)
        key = APIKey.objects.create(name="site", shop=self.shop, scopes=["orders:read", "orders:write", "products:read"])
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        self.fake = FakeStripe()
        patcher = mock.patch("shop.payments.requests.request", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def order(self, number="WS-1", vat="19"):
        r = self.c.post("/api/v1/orders/", {
            "order_number": number, "client": "Firma", "email": "einkauf@firma.de", "addr_country": "DE", "shop": True,
            "payment_method": "stripe", "payment_status": "pending", "shipping_cost": "6.90",
            "lines": [{"sku": "AN-1", "qty": 2}], "vat_rate": vat}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return SalesOrder.objects.get(order_number=number)

    def checkout(self, number="WS-1"):
        return self.c.post("/api/v1/shop/payments/stripe/checkout/", {
            "order_number": number, "success_url": SITE + "?zahlung=ok", "cancel_url": SITE + "?zahlung=abgebrochen",
            "lang": "de"}, format="json")


class CheckoutTests(StripeTestCase):
    def test_status(self):
        self.assertEqual(self.c.get("/api/v1/shop/payments/").json(), {"stripe": {"enabled": True, "mode": "test"}})

    def test_checkout_amount_and_reuse(self):
        self.order()
        r = self.checkout()
        self.assertEqual(r.status_code, 201, r.content)
        d = r.json()
        self.assertEqual(d["url"], "https://checkout.stripe.test/cs_test_1")
        # 2 × 20,00 + 6,90 доставка = 46,90 нетто; ПДВ 19 % = 8,91 → 55,81
        self.assertEqual(d["amount"], 55.81)
        method, path, form, headers = self.fake.calls[-1]
        self.assertEqual(form["line_items[0][price_data][unit_amount]"], "5581")
        self.assertEqual(form["metadata[order_number]"], "WS-1")
        self.assertEqual(form["customer_email"], "einkauf@firma.de")
        self.assertEqual(headers["Authorization"], f"Bearer {SECRET}")
        self.assertTrue(headers["Idempotency-Key"].startswith("minerva-webshop-"))
        # повторний запит — та сама відкрита сесія, без нової (без подвійної оплати)
        self.assertEqual(self.checkout().json()["session_id"], "cs_test_1")
        self.assertEqual(ShopPayment.objects.count(), 1)

    def test_amount_change_expires_old_session(self):
        o = self.order()
        self.checkout()
        o.shipping_cost = Decimal("0")
        o.save()
        self.assertEqual(self.checkout().json()["session_id"], "cs_test_2")
        self.assertEqual(ShopPayment.objects.get(session_id="cs_test_1").status, ShopPayment.EXPIRED)
        self.assertEqual(self.fake.sessions["cs_test_1"]["status"], "expired")

    def test_validation(self):
        self.order()
        r = self.c.post("/api/v1/shop/payments/stripe/checkout/", {
            "order_number": "WS-1", "success_url": "https://evil.example.org/x", "cancel_url": SITE}, format="json")
        self.assertEqual(r.json()["code"], "return_url_not_allowed")
        self.assertEqual(self.c.post("/api/v1/shop/payments/stripe/checkout/", {
            "order_number": "NOPE", "success_url": SITE, "cancel_url": SITE}, format="json").status_code, 404)
        Shop.objects.update(stripe_enabled=False)
        self.assertEqual(self.checkout().json()["code"], "stripe_disabled")

    def test_vat_unknown(self):
        o = self.order()
        o.shop_info.vat_rate = None
        o.shop_info.save()
        self.assertEqual(self.checkout().json()["code"], "vat_unknown")

    def test_stripe_error_message(self):
        self.order()
        Shop.objects.update(stripe_secret_key="sk_test_wrong")
        with mock.patch("shop.payments.requests.request") as req:
            req.return_value.status_code = 401
            req.return_value.json.return_value = {"error": {"message": "Invalid API Key provided"}}
            r = self.checkout()
        self.assertEqual((r.status_code, r.json()["code"]), (400, "stripe_error"))
        self.assertIn("Invalid API Key", r.json()["detail"])


class WebhookTests(StripeTestCase):
    def setUp(self):
        super().setUp()
        self.o = self.order()
        self.checkout()

    def hook(self, kind, obj, **kw):
        body, sig = signed({"id": "evt_1", "type": kind, "data": {"object": obj}}, **kw)
        return APIClient().post("/api/v1/payments/stripe/webhook/webshop/", body, content_type="application/json",
                                HTTP_STRIPE_SIGNATURE=sig)

    def test_paid(self):
        s = dict(self.fake.sessions["cs_test_1"], status="complete", payment_status="paid", payment_intent="pi_123")
        r = self.hook("checkout.session.completed", s)
        self.assertEqual(r.status_code, 200, r.content)
        self.o.refresh_from_db()
        self.assertEqual((self.o.payment_status, self.o.payment_reference), ("paid", "pi_123"))
        p = ShopPayment.objects.get()
        self.assertEqual((p.status, p.payment_intent), (ShopPayment.PAID, "pi_123"))
        self.assertEqual(self.hook("checkout.session.completed", s).status_code, 200)  # повтор — без змін
        # повернення коштів
        self.hook("charge.refunded", {"id": "ch_1", "payment_intent": "pi_123", "refunded": True})
        self.o.refresh_from_db()
        self.assertEqual(self.o.payment_status, "refunded")
        # після оплати нова сесія не створюється
        self.o.payment_status = "paid"
        self.o.save()
        self.assertEqual(self.checkout().json()["code"], "already_paid")

    def test_async_payment(self):
        s = dict(self.fake.sessions["cs_test_1"], status="complete", payment_status="unpaid", payment_intent="pi_9")
        self.hook("checkout.session.completed", s)
        self.assertEqual(ShopPayment.objects.get().status, ShopPayment.PROCESSING)
        self.hook("checkout.session.async_payment_failed", s)
        self.o.refresh_from_db()
        self.assertEqual(self.o.payment_status, "failed")

    def test_bad_signature_and_old_timestamp(self):
        s = dict(self.fake.sessions["cs_test_1"], status="complete", payment_status="paid")
        self.assertEqual(self.hook("checkout.session.completed", s, secret="whsec_other").status_code, 400)
        self.assertEqual(self.hook("checkout.session.completed", s, stamp=time.time() - 3600).status_code, 400)
        self.o.refresh_from_db()
        self.assertEqual(self.o.payment_status, "pending")

    def test_unknown_session_ignored(self):
        r = self.hook("checkout.session.completed", {"id": "cs_foreign", "payment_status": "paid"})
        self.assertEqual(r.json()["result"], "ignored")

    def test_sync_without_webhook(self):
        self.fake.sessions["cs_test_1"].update(status="complete", payment_status="paid", payment_intent="pi_5")
        r = self.c.post("/api/v1/shop/payments/stripe/sync/", {"order_number": "WS-1"}, format="json")
        self.assertEqual(r.json(), {"payment_status": "paid", "paid": True})


class AccountPayTests(StripeTestCase):
    def test_pay_from_account(self):
        from django.core import mail
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
        ns.email_host, ns.email_from = "smtp.example.com", "Shop <shop@example.com>"
        ns.save()
        self.shop.accounts_enabled = True
        self.shop.save()
        APIKey.objects.update(scopes=["orders:read", "orders:write", "products:read", "accounts:read", "accounts:write"])
        reg = {"email": "einkauf@firma.de", "password": "Antenne-2026!x", "company": "Firma", "name": "Anna",
               "confirm_b2b": True, "link": "https://web.example.com/dist/de/shop/konto/index.html"}
        self.c.post("/api/v1/shop/account/register/", reg, format="json")
        import re
        token = re.search(r"verify=([A-Za-z0-9_-]+)", mail.outbox[-1].body).group(1)
        session = self.c.post("/api/v1/shop/account/verify/", {"token": token, "password": reg["password"]},
                              format="json").json()["session"]
        self.order()  # гість з тим самим підтвердженим email → у кабінеті
        row = self.c.get("/api/v1/shop/account/me/orders/", HTTP_X_CUSTOMER_SESSION=session).json()["results"][0]
        self.assertTrue(row["payment_open"])
        r = self.c.post("/api/v1/shop/account/me/orders/WS-1/pay/", {"success_url": SITE, "cancel_url": SITE},
                        format="json", HTTP_X_CUSTOMER_SESSION=session)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(r.json()["url"].startswith("https://checkout.stripe.test/"))
        self.assertEqual(self.c.post("/api/v1/shop/account/me/orders/WS-X/pay/", {"success_url": SITE, "cancel_url": SITE},
                                     format="json", HTTP_X_CUSTOMER_SESSION=session).status_code, 404)


class StripeAdminTests(StripeTestCase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))

    def test_pages_and_key_form(self):
        self.order()
        self.checkout()
        p = ShopPayment.objects.get()
        for url in ["/admin/shop/shoppayment/", f"/admin/shop/shoppayment/{p.pk}/change/",
                    f"/admin/shop/shop/{self.shop.pk}/change/"]:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
        r = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        self.assertContains(r, "тестовий режим")
        self.assertContains(r, "/api/v1/payments/stripe/webhook/webshop/")
        self.assertNotContains(r, SECRET)  # ключ не показується
        self.client.post(f"/admin/shop/shop/{self.shop.pk}/stripe-check/")
        self.client.post("/admin/shop/shoppayment/", {"action": "action_sync", "_selected_action": [p.pk]})

    def test_check_key(self):
        self.assertIn("Sevskiy", payments.check_key(self.shop))


class PollingTests(StripeTestCase):
    def setUp(self):
        super().setUp()
        self.o = self.order()
        self.checkout()
        self.fake.sessions["cs_test_1"].update(status="complete", payment_status="paid", payment_intent="pi_7")

    def test_poll_until_webhook_works(self):
        from datetime import timedelta
        from django.utils import timezone
        self.assertEqual(payments.run_poll()[0][1]["paid"], 1)          # вебхука ще немає → перевірено
        self.o.refresh_from_db()
        self.assertEqual(self.o.payment_status, "paid")
        self.assertEqual(payments.run_poll(), [])                        # інтервал ще не минув
        Shop.objects.update(stripe_poll_last_at=timezone.now() - timedelta(minutes=20))
        self.assertEqual(payments.run_poll()[0][1]["checked"], 0)        # оплачене більше не перевіряється
        # вебхук прийшов → автоперевірка зупиняється
        body, sig = signed({"id": "evt", "type": "ping", "data": {"object": {}}})
        APIClient().post("/api/v1/payments/stripe/webhook/webshop/", body, content_type="application/json",
                         HTTP_STRIPE_SIGNATURE=sig)
        Shop.objects.update(stripe_poll_last_at=timezone.now() - timedelta(minutes=20))
        self.assertTrue(payments.webhook_active(Shop.objects.get()))
        self.assertEqual(payments.run_poll(), [])
        self.assertIn("вебхук працює", payments.poll_state(Shop.objects.get()))

    def test_poll_disabled(self):
        Shop.objects.update(stripe_poll_minutes=0)
        self.assertEqual(payments.run_poll(), [])

    def test_command(self):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command("check_stripe_payments", stdout=out)
        self.assertIn("оплачено 1", out.getvalue())


class CheckButtonsTests(StripeTestCase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        self.o = self.order()
        self.checkout()
        self.fake.sessions["cs_test_1"].update(status="complete", payment_status="paid", payment_intent="pi_8")

    def test_check_crm_customer(self):
        from crm.models import Customer
        c = Customer.objects.get(external_key=SalesOrder.objects.get(pk=self.o.pk).customer_key)
        r = self.client.get(f"/admin/crm/customer/{c.pk}/change/")
        self.assertContains(r, "Перевірити оплати")
        r = self.client.post(f"/admin/shop/shoppayment/check-crm/{c.pk}/", follow=True)
        self.assertContains(r, "оплачено 1")
        self.o.refresh_from_db()
        self.assertEqual(self.o.payment_status, "paid")
        self.assertEqual(self.client.get(f"/admin/shop/shoppayment/check-crm/{c.pk}/").status_code, 403)  # лише POST

    def test_check_all_and_list(self):
        r = self.client.get("/admin/shop/shoppayment/")
        self.assertContains(r, "Незавершених зараз: <b>1</b>")
        r = self.client.post("/admin/shop/shoppayment/check-open/", follow=True)
        self.assertContains(r, "оплачено 1")
        r = self.client.post("/admin/shop/shoppayment/check-open/", follow=True)
        self.assertContains(r, "Незавершених онлайн-оплат немає")

    def test_crm_list_action(self):
        from crm.models import Customer
        c = Customer.objects.get(external_key=SalesOrder.objects.get(pk=self.o.pk).customer_key)
        r = self.client.post("/admin/crm/customer/", {"action": "action_check_stripe_payments",
                                                      "_selected_action": [c.pk]}, follow=True)
        self.assertContains(r, "оплачено 1")
