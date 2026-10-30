"""Склад у листах-сповіщеннях — ті самі числа, що на дашборді (inventory/services/stock.py).

python manage.py test dashboard.tests_stock_notifications --settings=tabele.settings_test
"""
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from dashboard import notifications
from inventory.models import InventorySettings, Location, Product
from inventory.services.stock import create_movement
from sales.models import SalesOrder, SalesOrderLine


class StockInNotificationsTests(TestCase):
    def setUp(self):
        s = InventorySettings.get()
        s.deduct_on = InventorySettings.DeductOn.CREATION
        s.use_reservation = False
        s.low_stock_alert_enabled = True
        s.save()
        Location.objects.create(code="MAIN", location_type=Location.LocationType.FINISHED)
        self.ft = Product.objects.create(sku="FT-1", name="Filter")
        create_movement(product=self.ft, tx_type="Incoming", qty=100)

    def order(self, number, qty, status="received", days_ago=10):
        o = SalesOrder.objects.create(source="manual", order_number=number, status=status,
                                      order_date=date.today() - timedelta(days=days_ago))
        SalesOrderLine.objects.create(order=o, product=self.ft, qty=Decimal(qty))
        return o

    def test_cancelled_sales_do_not_make_stock_critical(self):
        o = self.order("C-1", 60)
        o.status = "cancelled"
        o.save()
        skus = [i["sku"] for i in notifications._get_critical_stock()]
        self.assertNotIn("FT-1", skus)

    def test_alerts_follow_low_stock_setting(self):
        self.order("A-1", 95)                      # доступно 5 за місячних продажів ~32 — критично
        self.assertIn("FT-1", [i["sku"] for i in notifications._get_critical_stock()])
        s = InventorySettings.get()
        s.low_stock_alert_enabled = False
        s.save()
        self.assertEqual(notifications._get_critical_stock(), [])

    def test_new_order_email_does_not_count_own_deduction_as_shortage(self):
        o = self.order("N-1", 100)                 # бере весь залишок — але він є
        from inventory.services.stock import order_lines_cover
        line = o.lines.get()
        self.assertGreaterEqual(order_lines_cover(o)[line.pk]["free"], Decimal("100"))
