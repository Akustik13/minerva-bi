"""Kundennummer, номери PO / OC / CI / PI / PL (shop/numbering.py), реєстрація зі сторінки «Дякуємо», документи
в кабінеті. python manage.py test shop --settings=tabele.settings_test"""
import shutil
import tempfile
from pathlib import Path

from django.core import mail
from django.test import override_settings

from inventory.models import InventoryTransaction
from sales.models import SalesOrder

from . import numbering
from .models import CustomerNumber, ShopCustomer, ShopOrder, ShopSettings
from .tests_accounts import PASSWORD, REG, AccountTestCase, token_from


class NumberingTests(AccountTestCase):
    def order(self, number, company="Firma GmbH", email="anna@firma.de", **extra):
        body = {"order_number": number, "client": company, "contact_name": "Anna", "email": email,
                "addr_country": "DE", "shop": True, "lines": [{"sku": "AN-1", "qty": 2}], "vat_rate": "19", **extra}
        return self.c.post("/api/v1/orders/", body, format="json")

    def test_order_numbers_per_customer(self):
        a = self.order("WS-261026-AAAA")
        self.assertEqual(a.status_code, 201, a.content)
        self.assertEqual(a.json()["order_number"], "PO-31728-001")
        self.assertEqual(self.order("WS-261026-BBBB").json()["order_number"], "PO-31728-002")
        self.assertEqual(self.order("WS-261026-CCCC", company="Andere AG", email="x@andere.de").json()["order_number"],
                         "PO-31729-001")
        again = self.order("WS-261026-AAAA")   # повтор з тим самим номером сайту — те саме замовлення
        self.assertEqual((again.status_code, again.json()["order_number"], again.json()["created"]),
                         (200, "PO-31728-001", False))
        self.assertEqual(SalesOrder.objects.count(), 3)
        order = SalesOrder.objects.get(order_number="PO-31728-001")
        self.assertEqual(order.shop_info.site_ref, "WS-261026-AAAA")
        keys = list(InventoryTransaction.objects.filter(external_key__startswith="so:").values_list("external_key", flat=True))
        self.assertTrue(keys and all(k.startswith("so:webshop:PO-") for k in keys), keys)   # склад — з новим номером

    def test_start_number_and_documents(self):
        ShopSettings.get()
        ShopSettings.objects.filter(pk=1).update(customer_number_start=50000)
        order = SalesOrder.objects.get(order_number=self.order("WS-1").json()["order_number"])
        self.assertEqual(order.order_number, "PO-50000-001")
        self.assertEqual(numbering.doc_number(order, "OC"), "OC-50000-001")
        self.assertEqual(numbering.doc_number(order, "OC"), "OC-50000-001")   # закріплено за замовленням
        second = SalesOrder.objects.get(order_number=self.order("WS-2").json()["order_number"])
        self.assertEqual(numbering.doc_number(second, "OC"), "OC-50000-002")
        self.assertEqual(numbering.doc_number(second, "CI"), "CI-50000-001")
        self.assertEqual(numbering.doc_number(second, "PI"), "PI-50000-001")
        self.assertEqual(ShopOrder.objects.get(order=second).doc_numbers,
                         {"OC": "OC-50000-002", "CI": "CI-50000-001", "PI": "PI-50000-001"})
        plain = SalesOrder.objects.create(source="manual", order_number="M-1", client="X")
        self.assertEqual(numbering.doc_number(plain, "CI"), "")   # не веб-магазин — як раніше

    def test_account_number_claim_and_documents(self):
        number = self.order("WS-9", email="einkauf@firma.de").json()["order_number"]   # гість, інша адреса
        reg = dict(REG, claim_order=number)
        self.assertEqual(self.post("register/", reg).status_code, 202)
        token = token_from(mail.outbox[-1], "verify")
        session = self.post("verify/", {"token": token, "password": PASSWORD}).json()["session"]
        rows = self.get("me/orders/", session).json()["results"]
        self.assertEqual([o["number"] for o in rows], [number])   # замовлення перенесено в кабінет
        me = self.get("me/", session).json()
        self.assertEqual(me["customer_number"], 31728)   # той самий клієнт CRM (Firma GmbH)
        self.assertEqual(ShopCustomer.objects.get().claimed_orders, [])
        r = self.c.patch("/api/v1/shop/account/me/", {"payment_method": "invoice"}, format="json",
                         HTTP_X_CUSTOMER_SESSION=session)
        self.assertEqual(r.json()["payment_method"], "invoice")
        bad = self.c.patch("/api/v1/shop/account/me/", {"payment_method": "cash"}, format="json",
                           HTTP_X_CUSTOMER_SESSION=session)
        self.assertEqual(bad.json()["fields"], ["payment_method"])

        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        with override_settings(MEDIA_ROOT=media):
            folder = Path(media) / "orders" / "webshop" / number
            folder.mkdir(parents=True)
            (folder / f"OrderConfirmation-{number}.pdf").write_bytes(b"%PDF-1.4 oc")
            (folder / "Invoice_CI-31728-001.pdf").write_bytes(b"%PDF-1.4 ci")
            (folder / f"CustomsDeclaration-{number}.pdf").write_bytes(b"%PDF-1.4 internal")
            (folder / "notiz.txt").write_text("intern")
            detail = self.get(f"me/orders/{number}/", session).json()
            self.assertEqual([(d["kind"], d["name"]) for d in detail["documents"]],
                             [("OC", f"OrderConfirmation-{number}.pdf"), ("CI", "Invoice_CI-31728-001.pdf")])
            r = self.get(f"me/orders/{number}/documents/OrderConfirmation-{number}.pdf/", session)
            self.assertEqual((r.status_code, b"".join(r.streaming_content)), (200, b"%PDF-1.4 oc"))
            r.close()
            for name in (f"CustomsDeclaration-{number}.pdf", "notiz.txt", "..%2F..%2Fsecret.pdf"):
                self.assertEqual(self.get(f"me/orders/{number}/documents/{name}/", session).status_code, 404, name)

    def test_claim_needs_own_shop_and_unowned_order(self):
        number = self.order("WS-7").json()["order_number"]
        self.assertEqual(self.post("register/", dict(REG, email="b@firma.de", claim_order="PO-99999-001")).status_code, 202)
        token = token_from(mail.outbox[-1], "verify")
        session = self.post("verify/", {"token": token, "password": PASSWORD}).json()["session"]
        self.assertEqual(self.get("me/orders/", session).json()["results"], [])   # чужий номер — нічого
        self.assertEqual(SalesOrder.objects.get(order_number=number).shop_info.customer, None)
        self.assertEqual(self.get("me/", session).json()["customer_number"], 31729)   # без замовлень — новий номер
        self.assertEqual(CustomerNumber.objects.get(number=31729).crm_customer, None)


class TestShopNumberingTests(AccountTestCase):
    """Тестовий магазин: TEST-PO-0001 …, без складу й без справжніх Kundennummern."""
    def test_separate_numbers_and_no_stock(self):
        from rest_framework.test import APIClient
        from api.models import APIKey
        from .models import Shop, ShopListing
        test = Shop.objects.create(name="Test", slug="testshop", is_test=True, accounts_enabled=True,
                                   url="https://web.example.com")
        ShopListing.objects.create(shop=test, product=self.p)
        key = APIKey.objects.create(name="t", shop=test, scopes=["products:read", "orders:write", "orders:read",
                                                                  "accounts:read", "accounts:write"])
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        body = {"order_number": "WS-T1", "client": "Firma GmbH", "contact_name": "Anna", "email": "anna@firma.de",
                "addr_country": "DE", "shop": True, "lines": [{"sku": "AN-1", "qty": 2}], "vat_rate": "19"}
        r = c.post("/api/v1/orders/", body, format="json")
        self.assertEqual(r.json()["order_number"], "TEST-PO-0001")
        order = SalesOrder.objects.get(order_number="TEST-PO-0001")
        self.assertFalse(order.affects_stock)   # без складу й поза статистикою
        self.assertFalse(InventoryTransaction.objects.filter(external_key__startswith="so:testshop:").exists())
        self.assertEqual(numbering.doc_number(order, "OC"), "TEST-OC-0001")
        self.assertEqual(CustomerNumber.objects.count(), 0)   # справжні номери не витрачено
        real = self.c.post("/api/v1/orders/", dict(body, order_number="WS-R1"), format="json").json()
        self.assertEqual(real["order_number"], "PO-31728-001")   # справжня нумерація — як була
        test.refresh_from_db()
        self.assertEqual(test.test_counters, {"PO": 1, "OC": 1})
        account = ShopCustomer.objects.create(shop=test, email="t@firma.de", name="T", company="Firma GmbH")
        from django.utils import timezone
        ShopCustomer.objects.filter(pk=account.pk).update(email_verified_at=timezone.now())
        account.refresh_from_db()
        self.assertEqual(numbering.display_number(account), "TEST-1")
