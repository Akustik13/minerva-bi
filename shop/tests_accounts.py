"""Тести особистого кабінету покупців:  python manage.py test shop --settings=tabele.settings_test"""
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from api.models import APIKey
from inventory.models import InventorySettings, Location, Product
from inventory.services.stock import create_movement

from . import accounts
from .models import Shop, ShopCustomer, ShopCustomerSession, ShopListing, ShopOrder

LINK = "https://web.example.com/dist/de/shop/konto/index.html"
PASSWORD = "Antenne-2026!x"
REG = {"email": "Anna@Firma.de", "password": PASSWORD, "company": "Firma GmbH", "name": "Anna Muster",
       "phone": "+49 89 1", "confirm_b2b": True, "lang": "de", "link": LINK}


def token_from(message, kind):
    return re.search(kind + r"=([A-Za-z0-9_-]+)", message.body).group(1)


class AccountTestCase(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()  # ліміт запитів API на ключ (кеш спільний для всіх тестів)
        self.addCleanup(cache.clear)
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
        ns.email_host, ns.email_from = "smtp.example.com", "Shop <shop@example.com>"
        ns.save()
        InventorySettings.get()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.shop = Shop.objects.create(name="Sevskiy", slug="webshop", is_default=True, accounts_enabled=True,
                                        url="https://web.example.com", mail_footer="Sevskiy GmbH · München")
        self.p = Product.objects.create(sku="AN-1", name="Antenne", sale_price=Decimal("20.00"))
        create_movement(product=self.p, tx_type="Incoming", qty=500)
        ShopListing.objects.create(shop=self.shop, product=self.p)
        self.key = APIKey.objects.create(name="site", shop=self.shop, scopes=[
            "products:read", "orders:write", "orders:read", "accounts:read", "accounts:write"])
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION=f"Token {self.key.key}")

    def post(self, path, data, session=None, **kw):
        extra = {"HTTP_X_CUSTOMER_SESSION": session} if session else {}
        return self.c.post(f"/api/v1/shop/account/{path}", data, format="json", **extra, **kw)

    def get(self, path, session):
        return self.c.get(f"/api/v1/shop/account/{path}", HTTP_X_CUSTOMER_SESSION=session)

    def register_and_verify(self, remember=False):
        self.assertEqual(self.post("register/", REG).status_code, 202)
        token = token_from(mail.outbox[-1], "verify")
        r = self.post("verify/", {"token": token, "password": PASSWORD, "remember": remember})
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()["session"]


class RegistrationTests(AccountTestCase):
    def test_register_sends_verification_and_login_needs_it(self):
        r = self.post("register/", REG)
        self.assertEqual(r.status_code, 202, r.content)
        c = ShopCustomer.objects.get()
        self.assertEqual(c.email, "anna@firma.de")
        self.assertFalse(c.is_verified)
        self.assertNotIn(PASSWORD, c.password)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(LINK + "#verify=", mail.outbox[0].body)
        self.assertIn("Anna Muster", mail.outbox[0].body)
        self.assertEqual(self.shop.__class__.objects.get().account_links, {"de": LINK})
        r = self.post("login/", {"email": "anna@firma.de", "password": PASSWORD})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "email_not_verified"))

    def test_verify_requires_password_then_logs_in(self):
        self.post("register/", REG)
        token = token_from(mail.outbox[0], "verify")
        r = self.post("verify/", {"token": token, "password": "wrong-password-1"})
        self.assertEqual(r.json()["code"], "invalid_credentials")
        r = self.post("verify/", {"token": token, "password": PASSWORD})
        self.assertEqual(r.status_code, 200)
        session = r.json()["session"]
        self.assertEqual(self.get("me/", session).json()["company"], "Firma GmbH")
        # посилання одноразове
        self.assertEqual(self.post("verify/", {"token": token, "password": PASSWORD}).json()["code"], "invalid_token")

    def test_validation(self):
        r = self.post("register/", {**REG, "password": "short"})
        self.assertEqual(r.json()["code"], "weak_password")
        self.assertIn("password_numeric", self.post("register/", {**REG, "password": "1234567890123"}).json()["problems"])
        self.assertIn("password_common", self.post("register/", {**REG, "password": "password1234"}).json()["problems"])
        r = self.post("register/", {**REG, "email": "nope", "company": ""})
        self.assertEqual(set(r.json()["fields"]), {"email", "company"})
        self.assertEqual(self.post("register/", {**REG, "confirm_b2b": False}).json()["code"], "confirm_b2b")
        self.assertEqual(self.post("register/", {**REG, "link": "https://evil.example.org/x"}).json()["code"],
                         "link_not_allowed")
        self.assertEqual(self.post("register/", {**REG, "link": "http://web.example.com/x"}).json()["code"],
                         "invalid_link")
        self.assertFalse(ShopCustomer.objects.exists())

    def test_existing_verified_email_gets_notice_not_takeover(self):
        session = self.register_and_verify()
        mail.outbox.clear()
        ShopCustomer.objects.update(last_mail_at=None)
        r = self.post("register/", {**REG, "password": "Another-pass-99"})
        self.assertEqual(r.status_code, 202)  # однакова відповідь
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("#verify=", mail.outbox[0].body)
        self.assertEqual(self.post("login/", {"email": REG["email"], "password": "Another-pass-99"}).status_code, 401)
        self.assertEqual(self.get("me/", session).status_code, 200)

    def test_unverified_reregistration_latest_wins(self):
        self.post("register/", {**REG, "password": "Attacker-pass-1"})
        ShopCustomer.objects.update(last_mail_at=None)
        self.post("register/", REG)
        self.assertEqual(ShopCustomer.objects.count(), 1)
        old, new = token_from(mail.outbox[0], "verify"), token_from(mail.outbox[1], "verify")
        self.assertEqual(self.post("verify/", {"token": old, "password": "Attacker-pass-1"}).json()["code"],
                         "invalid_token")
        self.assertEqual(self.post("verify/", {"token": new, "password": PASSWORD}).status_code, 200)

    def test_resend_is_throttled(self):
        self.post("register/", REG)
        self.post("resend/", {"email": REG["email"], "link": LINK})
        self.assertEqual(len(mail.outbox), 1)  # хвилина ще не минула
        ShopCustomer.objects.update(last_mail_at=timezone.now() - timedelta(minutes=2))
        self.assertEqual(self.post("resend/", {"email": REG["email"], "link": LINK}).status_code, 202)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(self.post("resend/", {"email": "nobody@x.de", "link": LINK}).status_code, 202)


class LoginTests(AccountTestCase):
    def test_login_logout(self):
        self.register_and_verify()
        r = self.post("login/", {"email": " ANNA@firma.de ", "password": PASSWORD, "remember": True})
        self.assertEqual(r.status_code, 200)
        session = r.json()["session"]
        self.assertTrue(r.json()["remember"])
        self.assertEqual(self.post("logout/", {}, session=session).status_code, 200)
        self.assertEqual(self.get("me/", session).json()["code"], "not_authenticated")

    def test_lockout_after_failures(self):
        self.register_and_verify()
        for _ in range(accounts.MAX_FAILED_LOGINS):
            self.assertEqual(self.post("login/", {"email": REG["email"], "password": "bad-password-x"}).status_code, 401)
        r = self.post("login/", {"email": REG["email"], "password": PASSWORD})
        self.assertEqual((r.status_code, r.json()["code"]), (429, "locked"))
        self.assertIn("Retry-After", r)
        ShopCustomer.objects.update(locked_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.post("login/", {"email": REG["email"], "password": PASSWORD}).status_code, 200)

    def test_unknown_email_same_answer(self):
        r = self.post("login/", {"email": "nobody@x.de", "password": PASSWORD})
        self.assertEqual((r.status_code, r.json()["code"]), (401, "invalid_credentials"))

    def test_session_expiry_and_disabled(self):
        session = self.register_and_verify()
        ShopCustomerSession.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.get("me/", session).status_code, 401)
        self.assertFalse(ShopCustomerSession.objects.exists())
        session = self.post("login/", {"email": REG["email"], "password": PASSWORD}).json()["session"]
        ShopCustomer.objects.update(is_active=False)
        self.assertEqual(self.get("me/", session).status_code, 401)

    def test_status(self):
        self.assertEqual(self.c.get("/api/v1/shop/account/").json(), {"enabled": True, "password_min_length": 10})
        Shop.objects.update(accounts_enabled=False)
        self.assertFalse(self.c.get("/api/v1/shop/account/").json()["enabled"])

    def test_permissions(self):
        self.key.scopes = ["products:read"]
        self.key.save()
        self.assertEqual(self.post("login/", {"email": "a@b.de", "password": "x"}).status_code, 403)
        self.key.scopes = ["accounts:read", "accounts:write"]
        self.key.save()
        Shop.objects.update(accounts_enabled=False)
        self.assertEqual(self.post("login/", {"email": "a@b.de", "password": "x"}).json()["code"], "accounts_disabled")

    def test_other_shop_cannot_use_session(self):
        session = self.register_and_verify()
        other = Shop.objects.create(name="Market", slug="market", accounts_enabled=True)
        key = APIKey.objects.create(name="m", shop=other, scopes=["accounts:read", "accounts:write"])
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        self.assertEqual(c.get("/api/v1/shop/account/me/", HTTP_X_CUSTOMER_SESSION=session).status_code, 401)


class PasswordTests(AccountTestCase):
    def test_forgot_and_reset(self):
        session = self.register_and_verify()
        mail.outbox.clear()
        ShopCustomer.objects.update(last_mail_at=None)
        self.assertEqual(self.post("password/forgot/", {"email": "nobody@x.de", "link": LINK}).status_code, 202)
        self.assertEqual(len(mail.outbox), 0)
        self.post("password/forgot/", {"email": REG["email"], "link": LINK})
        token = token_from(mail.outbox[0], "reset")
        self.assertEqual(self.post("password/reset/", {"token": token, "password": "short"}).json()["code"],
                         "weak_password")
        r = self.post("password/reset/", {"token": token, "password": "Neues-Passwort-7"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.get("me/", session).status_code, 401)  # старі входи завершено
        self.assertIn("geändert", mail.outbox[-1].subject)
        self.assertEqual(self.post("password/reset/", {"token": token, "password": "Neues-Passwort-8"}).json()["code"],
                         "invalid_token")
        self.assertEqual(self.post("login/", {"email": REG["email"], "password": "Neues-Passwort-7"}).status_code, 200)

    def test_reset_verifies_pending_account(self):
        self.post("register/", REG)
        ShopCustomer.objects.update(last_mail_at=None)
        self.post("password/forgot/", {"email": REG["email"], "link": LINK})
        r = self.post("password/reset/", {"token": token_from(mail.outbox[-1], "reset"), "password": "Neues-Passwort-7"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(ShopCustomer.objects.get().is_verified)

    def test_change_password_keeps_current_session(self):
        session = self.register_and_verify()
        other = self.post("login/", {"email": REG["email"], "password": PASSWORD}).json()["session"]
        r = self.post("me/password/", {"current_password": "bad", "new_password": "Neues-Passwort-7"}, session=session)
        self.assertEqual(r.json()["code"], "invalid_password")
        r = self.post("me/password/", {"current_password": PASSWORD, "new_password": "Neues-Passwort-7"}, session=session)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.get("me/", session).status_code, 200)
        self.assertEqual(self.get("me/", other).status_code, 401)


class ProfileCartOrderTests(AccountTestCase):
    def test_profile_update(self):
        session = self.register_and_verify()
        r = self.c.patch("/api/v1/shop/account/me/", {
            "company": "Firma GmbH", "name": "Anna", "vat_id": "de 123456789", "street": "Weg 1", "zip": "80331",
            "city": "München", "country": "de", "shipping": {"separate": True, "name": "Lager", "street": "Hof 2",
                                                             "zip": "80333", "city": "München", "country": "DE"}},
            format="json", HTTP_X_CUSTOMER_SESSION=session)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["vat_id"], r.json()["address"]["country"]), ("DE123456789", "DE"))
        r = self.c.patch("/api/v1/shop/account/me/", {"company": "F", "name": "A", "country": "Germany",
                                                      "shipping": {"separate": True}},
                         format="json", HTTP_X_CUSTOMER_SESSION=session)
        self.assertIn("country", r.json()["fields"])
        self.assertIn("shipping.street", r.json()["fields"])

    def test_cart(self):
        session = self.register_and_verify()
        r = self.c.put("/api/v1/shop/account/me/cart/", {"items": {"AN-1": 3, "rq:XY-2": 1.5}}, format="json",
                       HTTP_X_CUSTOMER_SESSION=session)
        self.assertEqual(r.json()["items"], {"AN-1": 3, "rq:XY-2": 1.5})
        self.assertEqual(self.get("me/cart/", session).json()["items"]["AN-1"], 3)
        bad = self.c.put("/api/v1/shop/account/me/cart/", {"items": {"<script>": 1}}, format="json",
                         HTTP_X_CUSTOMER_SESSION=session)
        self.assertEqual(bad.json()["code"], "invalid_cart")

    def order(self, number, session=None, email="anna@firma.de", **extra):
        body = {"order_number": number, "client": "Firma GmbH", "contact_name": "Anna", "email": email,
                "addr_country": "DE", "shop": True, "lines": [{"sku": "AN-1", "qty": 2}], "vat_rate": "19",
                "billing": {"company": "Firma GmbH", "name": "Anna", "street": "Weg 1", "zip": "80331",
                            "city": "München", "country": "de"}, **extra}
        if session:
            body["customer_session"] = session
        return self.c.post("/api/v1/orders/", body, format="json")

    def test_guest_orders_attached_on_verify(self):
        self.assertFalse(self.order("WS-1", email="ANNA@firma.de").json()["account_linked"])
        session = self.register_and_verify()
        rows = self.get("me/orders/", session).json()["results"]
        self.assertEqual([o["number"] for o in rows], ["WS-1"])

    def test_order_with_session_and_detail(self):
        session = self.register_and_verify()
        r = self.order("WS-2", session=session, email="einkauf@firma.de")
        self.assertTrue(r.json()["account_linked"], r.content)
        d = self.get("me/orders/WS-2/", session).json()
        self.assertEqual((d["net"], d["vat_rate"], d["status"]), (40.0, 19.0, "received"))
        self.assertEqual(d["gross"], round(40 * 1.19, 2))
        self.assertEqual(d["lines"][0]["sku"], "AN-1")
        self.assertEqual(d["billing"]["country"], "DE")
        self.assertEqual(self.get("me/orders/NOPE/", session).status_code, 404)
        c = ShopCustomer.objects.get()
        self.assertEqual((c.street, c.city), ("Weg 1", "München"))  # профіль заповнено з першого замовлення
        self.assertIsNotNone(c.crm_customer)

    def test_foreign_order_not_visible(self):
        session = self.register_and_verify()
        self.order("WS-3", email="someone@else.de")
        self.assertEqual(self.get("me/orders/", session).json()["count"], 0)
        self.assertEqual(self.get("me/orders/WS-3/", session).status_code, 404)

    def test_quote_in_account(self):
        session = self.register_and_verify()
        r = self.c.post("/api/v1/orders/", {"order_number": "AN-1", "client": "Firma GmbH", "email": "x@y.de",
                                            "quote": True, "customer_session": session,
                                            "lines": [{"sku": "UNKNOWN-1", "qty": 5}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        row = self.get("me/orders/", session).json()["results"][0]
        self.assertEqual((row["kind"], row["net"], row["items"]), ("quote", None, 1))
        d = self.get("me/orders/AN-1/", session).json()
        self.assertEqual([(l["sku"], l["qty"]) for l in d["lines"]], [("UNKNOWN-1", 5.0)])

    def test_delete_account_keeps_orders(self):
        session = self.register_and_verify()
        self.order("WS-4", session=session)
        r = self.c.delete("/api/v1/shop/account/me/", {"password": "bad"}, format="json",
                          HTTP_X_CUSTOMER_SESSION=session)
        self.assertEqual(r.json()["code"], "invalid_password")
        r = self.c.delete("/api/v1/shop/account/me/", {"password": PASSWORD}, format="json",
                          HTTP_X_CUSTOMER_SESSION=session)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(ShopCustomer.objects.exists())
        self.assertIsNone(ShopOrder.objects.get().customer)

    def test_cleanup(self):
        self.post("register/", REG)
        ShopCustomer.objects.update(created_at=timezone.now() - timedelta(days=31))
        self.assertEqual(accounts.cleanup()["unverified"], 1)


class AccountAdminTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))

    def test_pages_and_actions(self):
        self.register_and_verify()
        c = ShopCustomer.objects.get()
        for url in ["/admin/shop/shopcustomer/", "/admin/shop/shopcustomer/?state=pending",
                    f"/admin/shop/shopcustomer/{c.pk}/change/", f"/admin/shop/shop/{self.shop.pk}/change/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        r = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        self.assertContains(r, "Акаунтів: 1")
        self.assertContains(r, "shop@example.com")
        mail.outbox.clear()
        self.client.post("/admin/shop/shopcustomer/", {"action": "action_send_reset", "_selected_action": [c.pk]})
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(LINK + "#reset=", mail.outbox[0].body)
        self.client.post("/admin/shop/shopcustomer/", {"action": "action_disable", "_selected_action": [c.pk]})
        self.assertFalse(ShopCustomer.objects.get().is_active)
        self.assertFalse(ShopCustomerSession.objects.exists())

    def test_test_mail(self):
        self.client.post(f"/admin/shop/shop/{self.shop.pk}/test-mail/")
        self.assertEqual(mail.outbox[-1].to, ["adm@example.com"])
        self.assertIn("Shop <shop@example.com>", mail.outbox[-1].from_email)
