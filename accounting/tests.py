"""Номер рахунку-фактури (Invoice.number) видається під блокуванням лічильника — без повторів.

    python manage.py test accounting --settings=tabele.settings_test
"""
from datetime import date
from unittest import mock

from django.db import IntegrityError, models
from django.test import TestCase

from accounting.models import CompanySettings, Invoice


class InvoiceNumberTests(TestCase):
    def setUp(self):
        self.cfg = CompanySettings.get()
        self.year = date.today().year

    def counter(self):
        return CompanySettings.objects.get(pk=1).next_number

    def test_numbers_follow_the_counter(self):
        first, second = Invoice.objects.create(), Invoice.objects.create()
        self.assertEqual((first.number, second.number), (f"INV-{self.year}-0001", f"INV-{self.year}-0002"))
        self.assertEqual(self.counter(), 3)
        first.notes = "змінено"
        first.save()                                                   # номер не змінюється
        first.refresh_from_db()
        self.assertEqual(first.number, f"INV-{self.year}-0001")
        self.assertEqual(self.counter(), 3)

    def test_taken_number_is_skipped_when_the_counter_was_set_back(self):
        Invoice.objects.create()
        CompanySettings.objects.filter(pk=1).update(next_number=1)
        self.assertEqual(Invoice.objects.create().number, f"INV-{self.year}-0002")
        self.assertEqual(self.counter(), 3)

    def test_failed_save_gives_the_number_back(self):
        invoice = Invoice()
        with mock.patch.object(models.Model, "save", side_effect=IntegrityError("boom")):
            with self.assertRaises(IntegrityError):
                invoice.save()
        self.assertEqual(invoice.number, "")
        self.assertEqual(self.counter(), 1)
        invoice.save()
        self.assertEqual(invoice.number, f"INV-{self.year}-0001")

    def test_allocation_locks_the_counter_row(self):
        with mock.patch.object(CompanySettings.objects, "select_for_update",
                               wraps=CompanySettings.objects.select_for_update) as lock:
            Invoice.objects.create()
        lock.assert_called_once_with()

    def test_update_fields_without_number_still_stores_it(self):
        invoice = Invoice.objects.create()
        Invoice.objects.filter(pk=invoice.pk).update(number="")       # давній запис без номера
        invoice.number = ""
        invoice.status = Invoice.Status.SENT
        invoice.save(update_fields=["status"])
        invoice.refresh_from_db()
        self.assertEqual((invoice.status, invoice.number), (Invoice.Status.SENT, f"INV-{self.year}-0002"))
