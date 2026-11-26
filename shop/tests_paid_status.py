"""Оплачене замовлення магазину → «В обробці» (Shop.paid_order_status, shop/paid_status.py):
python manage.py test shop.tests_paid_status --settings=tabele.settings_test"""
from django.utils import timezone

from sales.models import SalesOrder

from . import paid_status
from .tests_payments import StripeTestCase, signed
from rest_framework.test import APIClient


class PaidStatusTests(StripeTestCase):
    def hook_paid(self):
        s = dict(self.fake.sessions["cs_test_1"], status="complete", payment_status="paid", payment_intent="pi_1")
        body, sig = signed({"id": "evt_1", "type": "checkout.session.completed", "data": {"object": s}})
        with self.captureOnCommitCallbacks(execute=True):
            r = APIClient().post("/api/v1/payments/stripe/webhook/webshop/", body, content_type="application/json",
                                 HTTP_STRIPE_SIGNATURE=sig)
        self.assertEqual(r.status_code, 200, r.content)

    def site_order(self, number="WS-7", method="prepayment"):
        r = self.c.post("/api/v1/orders/", {
            "order_number": number, "client": "Firma", "email": "einkauf@firma.de", "addr_country": "DE", "shop": True,
            "payment_method": method, "lines": [{"sku": "AN-1", "qty": 1}], "vat_rate": "19"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return SalesOrder.objects.get(shop_info__site_ref=number)

    def mark_paid(self, o):
        o.payment_status = "paid"
        with self.captureOnCommitCallbacks(execute=True):
            o.save()
        o.refresh_from_db()

    def test_default_is_processing(self):
        self.assertEqual(self.shop.paid_order_status, "processing")

    def test_stripe_paid_sets_processing(self):
        o = self.order()
        self.checkout()
        self.assertEqual(o.status, "received")
        self.hook_paid()
        o.refresh_from_db()
        self.assertEqual((o.payment_status, o.status, o.status_source), ("paid", "processing", paid_status.SOURCE))

    def test_off_keeps_received(self):
        self.shop.paid_order_status = ""
        self.shop.save()
        o = self.order()
        self.checkout()
        self.hook_paid()
        o.refresh_from_db()
        self.assertEqual((o.payment_status, o.status), ("paid", "received"))

    def test_manual_payment_in_card(self):
        o = self.site_order()
        self.assertEqual(o.status, "received")
        self.mark_paid(o)
        self.assertEqual(o.status, "processing")

    def test_other_statuses_untouched(self):
        o = self.site_order()
        o.status = "cancelled"
        o.save()
        self.mark_paid(o)
        self.assertEqual(o.status, "cancelled")

    def test_back_to_received_after_payment_kept(self):
        o = self.site_order()
        self.mark_paid(o)
        self.assertEqual(o.status, "processing")
        o.status = "received"          # людина повернула — оплата не нова, правило не діє
        with self.captureOnCommitCallbacks(execute=True):
            o.save()
        o.refresh_from_db()
        self.assertEqual(o.status, "received")

    def test_not_shop_order(self):
        o = SalesOrder.objects.create(source="manual", order_number="M-1", order_date=timezone.now(),
                                      payment_status="pending")
        self.mark_paid(o)
        self.assertEqual(o.status, "received")
        self.assertEqual(paid_status.apply(o.pk), "not_shop")

    def test_apply_results(self):
        o = self.site_order()
        self.assertEqual(paid_status.apply(o.pk), "skip")   # ще не оплачено
        SalesOrder.objects.filter(pk=o.pk).update(payment_status="paid")
        self.assertEqual(paid_status.apply(o.pk), "done")
        self.assertEqual(paid_status.apply(o.pk), "skip")   # уже «В обробці»
