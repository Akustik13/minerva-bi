"""CRM: подія хронології для нового замовлення; RFM і «Тип» — лише продажі, сума у валюті Minerva (crm/utils.py).

    python manage.py test crm --settings=tabele.settings_test
"""
import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from crm import utils
from crm.models import Customer, CustomerTimeline
from sales.models import SalesOrder
from shop.models import ShopSettings

TODAY = datetime.date(2026, 10, 30)


class TimelineSignalTests(TestCase):
    def test_new_order_gets_timeline_event_once(self):
        o = SalesOrder.objects.create(source="webshop", order_number="T-1", client="Muster GmbH",
                                      email="kunde@example.com", total_price=Decimal("12.50"), currency="USD")
        self.assertTrue(o.customer_key)                     # ключ і на самому об'єкті
        customer = Customer.objects.get(external_key=o.customer_key)
        events = CustomerTimeline.objects.filter(customer=customer, event_type="order", related_order_id=o.pk)
        self.assertEqual(events.count(), 1)
        self.assertIn("12.50 USD", events.get().body)
        o.status = "processing"
        o.save()                                            # повторне збереження — без дубля й ключ лишається
        self.assertEqual(events.count(), 1)
        self.assertEqual(SalesOrder.objects.get(pk=o.pk).customer_key, customer.external_key)


@mock.patch("django.utils.timezone.localdate", return_value=TODAY)
class SaleOnlyStatsTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.default_currency = "EUR"
        ss.is_onboarding_complete = True
        ss.save()
        s = ShopSettings.get()
        s.fx_rates = {"USD": 0.5}
        s.save()
        self.c = Customer.objects.create(name="Muster", company="Muster GmbH", email="m@example.com")
        self.n = 0

    def order(self, total, currency="EUR", days_ago=10, **kw):
        self.n += 1
        values = dict(source="webshop", order_number=f"R{self.n}", customer_key=self.c.external_key,
                      email=self.c.email, total_price=Decimal(total), currency=currency,
                      order_date=TODAY - datetime.timedelta(days=days_ago))
        values.update(kw)
        return SalesOrder.objects.create(**values)

    def fresh(self):
        return Customer.objects.get(pk=self.c.pk)

    def test_quotes_cancelled_and_test_orders_do_not_count(self, _today):
        self.order("100")
        self.order("9000", status="cancelled")
        self.order("9000", document_type="QUOTE", affects_stock=False)
        self.order("9000", affects_stock=False)             # тестовий магазин
        self.order("9000", days_ago=1, document_type="SAMPLE")
        c = self.fresh()
        self.assertEqual(c.total_orders(), 1)
        self.assertEqual(c.total_revenue(), Decimal("100"))
        self.assertEqual(utils.order_counts([c]), {c.pk: 1})
        self.assertFalse(c.is_repeat_customer())
        self.assertEqual(c.rfm_score(), {"R": 5, "F": 1, "M": 2, "score": 8, "segment": "⭐ Potential"})

    def test_monetary_converts_currencies(self, _today):
        self.order("800", "EUR")
        self.order("400", "USD")                            # 200 EUR → разом 1 000 → M = 4
        self.order("50000", "JPY")                          # курсу немає — не додається
        c = self.fresh()
        self.assertEqual(c.total_revenue(), Decimal("1000"))
        self.assertEqual(c.rfm_score()["M"], 4)
        self.assertEqual(c.order_stats().revenue.missing, {"JPY": Decimal("50000")})
        self.assertEqual(c.avg_order_value(), Decimal("500.00"))   # 2 замовлення з курсом
        self.assertIn("без курсу", c.card_stats()["rev"])

    def test_list_filter_and_columns(self, _today):
        for i in range(2):
            self.order("10", days_ago=200 + i)
        quote_only = Customer.objects.create(name="Quote only", email="q@example.com")
        SalesOrder.objects.create(source="webshop", order_number="Q1", customer_key=quote_only.external_key,
                                  document_type="QUOTE", affects_stock=False, order_date=TODAY)
        SalesOrder.objects.create(source="webshop", order_number="Q2", customer_key=quote_only.external_key,
                                  document_type="QUOTE", affects_stock=False, order_date=TODAY)
        user = User.objects.create_user("boss", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(user)
        r = self.client.get("/admin/crm/customer/", {"rfm_segment": "at_risk"})
        self.assertEqual({c.name for c in r.context["cl"].result_list}, {"Muster"})
        r = self.client.get("/admin/crm/customer/", {"repeat": "yes"})
        self.assertEqual({c.name for c in r.context["cl"].result_list}, {"Muster"})
        self.assertEqual(utils.rfm_scores([quote_only])[quote_only.pk]["segment"], "💤 Hibernating")
