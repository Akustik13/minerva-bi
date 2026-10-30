"""Аудит безпеки магазину (жовтень 2026): друга оплата під час SEPA, посилання з листів без «Адреса сайту»,
гостьове замовлення з чужим email.

python manage.py test shop.tests_audit --settings=tabele.settings_test
"""
from decimal import Decimal

from django.test import TestCase

from . import accounts, payments
from .models import Shop, ShopCustomer, ShopPayment
from .tests_payments import StripeTestCase, signed


class DoubleChargeTests(StripeTestCase):
    def test_no_second_checkout_while_payment_processing(self):
        order = self.order()
        self.assertEqual(self.checkout().status_code, 201)
        ShopPayment.objects.filter(order=order).update(status=ShopPayment.PROCESSING)   # SEPA ще йде
        r = self.checkout()
        self.assertEqual((r.status_code, r.json()["code"]), (409, "payment_processing"))
        self.assertEqual(ShopPayment.objects.filter(order=order).count(), 1)

    def test_paid_state_read_under_lock(self):
        order = self.order()
        stale = type(order).objects.get(pk=order.pk)
        type(order).objects.filter(pk=order.pk).update(payment_status="paid")   # оплачено між читанням і кліком
        with self.assertRaises(payments.PaymentError) as e:
            payments.start_checkout(self.shop, stale, "https://web.example.com/ok", "https://web.example.com/no")
        self.assertEqual(e.exception.code, "already_paid")

    def test_failed_sepa_does_not_override_other_paid_payment(self):
        order = self.order()
        self.checkout()
        first = ShopPayment.objects.get(order=order)
        ShopPayment.objects.create(order=order, shop=self.shop, session_id="cs_other", amount=first.amount,
                                   currency=first.currency, status=ShopPayment.PAID)
        type(order).objects.filter(pk=order.pk).update(payment_status="paid")
        body, sig = signed({"type": "checkout.session.async_payment_failed",
                            "data": {"object": {"id": first.session_id}}})
        payments.handle_webhook(self.shop, body, sig)
        order.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual((first.status, order.payment_status), (ShopPayment.FAILED, "paid"))

    def test_return_url_needs_site_address(self):
        Shop.objects.filter(pk=self.shop.pk).update(url="")
        self.shop.refresh_from_db()
        with self.assertRaises(payments.PaymentError) as e:
            payments.check_return_url(self.shop, "https://attacker.example/ok")
        self.assertEqual(e.exception.code, "return_url_not_allowed")


class AccountLinkTests(TestCase):
    def test_link_needs_site_address(self):
        shop = Shop.objects.create(name="S", slug="s1", url="")
        with self.assertRaises(accounts.AccountError) as e:
            accounts.check_link(shop, "https://attacker.example/konto")
        self.assertEqual(e.exception.code, "link_not_allowed")
        shop.url = "https://web.example.com"
        self.assertEqual(accounts.check_link(shop, "https://web.example.com/konto"), "https://web.example.com/konto")

    def test_guest_order_with_foreign_email_does_not_fill_profile(self):
        from django.utils import timezone

        from sales.models import SalesOrder
        shop = Shop.objects.create(name="S", slug="s2", url="https://web.example.com", accounts_enabled=True)
        victim = ShopCustomer.objects.create(shop=shop, email="victim@firma.de", email_verified_at=timezone.now())
        order = SalesOrder.objects.create(source="s2", order_number="G-1", email="victim@firma.de",
                                          total_price=Decimal("10"))
        accounts.record_order(order, shop, billing={"company": "Evil GmbH", "street": "Fake 1", "zip": "1",
                                                    "city": "X", "country": "DE"})
        victim.refresh_from_db()
        self.assertEqual((victim.street, victim.company), ("", ""))
        self.assertEqual(order.shop_info.customer_id, victim.pk)      # замовлення в кабінеті — як раніше
