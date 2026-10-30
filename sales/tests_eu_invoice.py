"""Номери EU Invoice не повторюються: номер видає сервер під блокуванням, у замовлення — один раз.

    python manage.py test sales --settings=tabele.settings_test
"""
import json
import shutil
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from sales.eu_invoice_numbers import InvoiceNumberTaken, assign_invoice_number
from sales.models import SalesOrder, SalesSettings


class AssignInvoiceNumberTests(TestCase):
    def setUp(self):
        SalesOrder.objects.create(source="webshop", order_number="A-1", eu_invoice_number=10231)
        SalesOrder.objects.create(source="webshop", order_number="A-2", eu_invoice_number=10235)
        self.order = SalesOrder.objects.create(source="webshop", order_number="B-1")

    def test_next_number_is_reserved_for_the_order(self):
        self.assertEqual(assign_invoice_number(self.order), 10236)
        self.order.refresh_from_db()
        self.assertEqual(self.order.eu_invoice_number, 10236)
        other = SalesOrder.objects.create(source="webshop", order_number="B-2")
        self.assertEqual(assign_invoice_number(other), 10237)           # підказка «максимум + 1» уже інша

    def test_regenerating_keeps_the_number(self):
        first = assign_invoice_number(self.order)
        self.assertEqual(assign_invoice_number(self.order), first)
        self.assertEqual(assign_invoice_number(self.order, requested=10231), first)   # чужий номер — не діє
        self.assertEqual(assign_invoice_number(self.order, requested=99999), first)

    def test_number_of_another_order_is_refused(self):
        with self.assertRaises(InvoiceNumberTaken) as ctx:
            assign_invoice_number(self.order, requested=10235)
        self.assertIn("A-2", str(ctx.exception))
        self.order.refresh_from_db()
        self.assertIsNone(self.order.eu_invoice_number)

    def test_free_number_from_the_form_is_accepted(self):
        self.assertEqual(assign_invoice_number(self.order, requested=10233), 10233)

    def test_allocation_locks_the_settings_row(self):
        SalesSettings.get()
        with mock.patch.object(SalesSettings.objects, "select_for_update",
                               wraps=SalesSettings.objects.select_for_update) as lock:
            assign_invoice_number(self.order)
        lock.assert_called_once_with()


class GenerateViewTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        ss = SystemSettings.objects.get_or_create(pk=1)[0]
        ss.is_onboarding_complete = True
        ss.save()
        self.client.force_login(User.objects.create_user("boss", password="x", is_staff=True, is_superuser=True))
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=Path(media))
        override.enable()
        self.addCleanup(override.disable)
        SalesOrder.objects.create(source="webshop", order_number="A-1", eu_invoice_number=7)
        self.order = SalesOrder.objects.create(source="webshop", order_number="B-1")
        self.url = f"/admin/sales/salesorder/{self.order.pk}/eu-invoice/generate/"

    def post(self, **body):
        # PDF (reportlab) не потрібен — перевіряємо номер; модуль підмінено цілком
        gen = mock.Mock(return_value=b"%PDF-1.4")
        fake = types.SimpleNamespace(generate_eu_invoice=gen)
        with mock.patch.dict(sys.modules, {"sales.eu_invoice_pdf": fake}):
            r = self.client.post(self.url, json.dumps({"buyer_vat_id": "DE815236628", **body}),
                                 content_type="application/json")
        return r, gen

    def test_number_comes_from_the_server_not_from_the_default_1(self):
        r, gen = self.post()                                              # без номера — раніше ставилось 1
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["X-Invoice-Number"], "8")
        self.assertEqual(gen.call_args.kwargs["invoice_number"], 8)
        self.order.refresh_from_db()
        self.assertEqual((self.order.eu_invoice_number, self.order.buyer_vat_id), (8, "DE815236628"))
        r, gen = self.post(invoice_number=8)                              # повторна генерація — той самий
        self.assertEqual(r["X-Invoice-Number"], "8")

    def test_number_of_another_order_is_refused_with_a_clear_message(self):
        r, gen = self.post(invoice_number=7)
        self.assertEqual(r.status_code, 409)
        self.assertIn("уже має замовлення A-1", r.json()["error"])
        gen.assert_not_called()
        self.order.refresh_from_db()
        self.assertIsNone(self.order.eu_invoice_number)

    def test_bad_number_is_rejected(self):
        for value in ("abc", 0, -3):
            r, gen = self.post(invoice_number=value)
            self.assertEqual(r.status_code, 400, value)
        gen.assert_not_called()
