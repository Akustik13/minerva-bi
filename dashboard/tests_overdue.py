"""Прострочені дедлайни: доставлене, відправлене, скасоване й Angebote — не прострочені (лист, задачі, Minerva AI).

python manage.py test dashboard.tests_overdue --settings=tabele.settings_test
"""
import datetime
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from dashboard.notifications import _get_overdue_orders
from sales.models import SalesOrder
from tasks.models import Task

PAST = datetime.date(2026, 8, 10)


def order(number, status, **kw):
    return SalesOrder.objects.create(source="digikey", order_number=number, status=status, affects_stock=True,
                                     shipping_deadline=PAST, order_date=PAST, **kw)


class OverdueTests(TestCase):
    def setUp(self):
        self.waiting = order("100", "processing")
        order("100601957", "delivered")                                   # як у листі: доставлено, без дати відправки
        order("101952542", "delivered")
        order("102", "shipped")
        order("103", "cancelled")
        order("104", "received", document_type="QUOTE")

    def test_alert_lists_only_orders_still_to_ship(self):
        self.assertEqual([o["order_number"] for o in _get_overdue_orders()], ["100"])

    def test_tasks_only_for_waiting_and_stale_ones_closed(self):
        delivered = SalesOrder.objects.get(order_number="100601957")
        stale = Task.objects.create(order=delivered, task_type=Task.TaskType.DEADLINE_ALERT, title="old",
                                    status=Task.Status.PENDING)
        call_command("check_tasks", stdout=StringIO())
        open_alerts = Task.objects.filter(task_type=Task.TaskType.DEADLINE_ALERT, status=Task.Status.PENDING)
        self.assertEqual([t.order_id for t in open_alerts], [self.waiting.pk])
        stale.refresh_from_db()
        self.assertEqual(stale.status, Task.Status.DONE)

    def test_minerva_ai_overview(self):
        from ai_assistant.tools import _get_financial_overview
        data = _get_financial_overview({}, None)
        self.assertEqual(([o["id"] for o in data["overdue_orders"]], data["overdue_count"]), ([self.waiting.pk], 1))
