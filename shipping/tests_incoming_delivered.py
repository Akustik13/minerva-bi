"""📥 Вхідну посилку доставлено (shipping/services/incoming.on_delivered, InventorySettings.incoming_delivered):
лише повідомлення / «прибуло» / оприбуткування закупівлі один раз; кілька посилок — коли доставлені всі.
Пошту й Telegram підмінено.

python manage.py test shipping.tests_incoming_delivered --settings=tabele.settings_test
"""
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from inventory.models import InventorySettings, Product, PurchaseOrder, PurchaseOrderLine, Supplier
from shipping.models import Carrier, Shipment
from shipping.services import incoming

TN, TN2 = '1Z2R5D130407163813', '1Z2R5D130407163999'


class IncomingDeliveredTests(TestCase):
    def setUp(self):
        from config.models import NotificationSettings, SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        ns = NotificationSettings.get()
        ns.email_enabled, ns.telegram_enabled = True, True
        ns.save()
        self.ups = Carrier.objects.create(name='UPS', carrier_type='ups', is_active=True, sender_company='Sevskiy GmbH')
        self.sup = Supplier.objects.create(name='Raynool Technology', addr_country='CN')
        self.product = Product.objects.create(sku='AN220213-01C', name='Antenna')
        self.po = PurchaseOrder.objects.create(supplier=self.sup, status='ordered')
        self.line = PurchaseOrderLine.objects.create(purchase_order=self.po, product=self.product,
                                                     qty_ordered=Decimal('1000'))
        self.parcel = incoming.create(TN, purchase_order=self.po)
        Shipment.objects.filter(pk=self.parcel.pk).update(status='in_transit')
        self.mail = mock.patch('dashboard.notifications._send_event_email').start()
        self.tg = mock.patch('dashboard.notifications._send_telegram').start()
        self.addCleanup(mock.patch.stopall)

    def mode(self, value):
        s = InventorySettings.get()
        s.incoming_delivered = value
        s.save()

    def deliver(self, parcel=None):
        s = Shipment.objects.get(pk=(parcel or self.parcel).pk)
        s.status = 'delivered'
        with self.captureOnCommitCallbacks(execute=True):
            s.save()

    def test_notify_only(self):
        self.deliver()
        self.mail.assert_called_once()
        subject, html = self.mail.call_args.args[1], self.mail.call_args.args[2]
        self.assertIn(f'📥 Прийшла посилка {self.po.code} від', subject)
        self.assertIn('AN220213-01C × 1000', html)
        self.assertIn('оприбуткуйте на сторінці закупівлі', html)
        self.assertIn(f'/admin/inventory/purchaseorder/{self.po.pk}/change/', self.tg.call_args.args[1])
        self.po.refresh_from_db()
        self.line.refresh_from_db()
        self.assertEqual((self.po.status, self.line.qty_received, self.po.receipts.count()), ('ordered', 0, 0))

    def test_receive_once(self):
        self.mode(InventorySettings.INCOMING_RECEIVE)
        self.deliver()
        self.po.refresh_from_db()
        self.line.refresh_from_db()
        self.assertEqual((self.po.status, self.line.qty_received), ('received', Decimal('1000')))
        receipt = self.po.receipts.get()
        self.assertEqual((receipt.delivery_key, receipt.booked), (f'parcel:{self.parcel.pk}', True))
        from inventory.models import InventoryTransaction
        self.assertEqual(sum(t.qty for t in InventoryTransaction.objects.filter(product=self.product)),
                         Decimal('1000'))                                      # прихід на склад
        self.assertIn('✅ Оприбутковано на склад: AN220213-01C × 1000', self.mail.call_args.args[2])
        out = incoming.on_delivered(Shipment.objects.get(pk=self.parcel.pk))   # повтор — без другого приходу
        self.assertIsNone(out['result'])                                      # закупівля вже отримана
        self.assertIn('нічого не змінено', self.mail.call_args.args[2])
        self.line.refresh_from_db()
        self.assertEqual(self.line.qty_received, Decimal('1000'))

    def test_arrived_marks_without_stock(self):
        self.mode(InventorySettings.INCOMING_ARRIVED)
        self.deliver()
        self.po.refresh_from_db()
        self.line.refresh_from_db()
        receipt = self.po.receipts.get()
        self.assertEqual((receipt.booked, self.line.qty_received, self.po.status), (False, 0, 'ordered'))
        self.assertIn('позначено «прибуло»', self.mail.call_args.args[2])

    def test_two_parcels_wait_for_all(self):
        self.mode(InventorySettings.INCOMING_RECEIVE)
        second = incoming.create(TN2, purchase_order=self.po)
        Shipment.objects.filter(pk=second.pk).update(status='in_transit')
        self.deliver()
        self.line.refresh_from_db()
        self.assertEqual(self.line.qty_received, 0)
        self.assertIn('ще 1 посилок у дорозі', self.mail.call_args.args[2])
        self.deliver(second)
        self.line.refresh_from_db()
        self.assertEqual(self.line.qty_received, Decimal('1000'))

    def test_off_keeps_generic_notification(self):
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
        ns.shipment_email, ns.shipment_on_delivered = True, True
        ns.save()
        self.mode(InventorySettings.INCOMING_OFF)
        self.deliver()
        self.assertEqual(self.po.receipts.count(), 0)
        self.assertIn('Відправлення #', self.mail.call_args.args[1])        # загальне сповіщення, як раніше

    def test_outgoing_not_touched(self):
        self.mode(InventorySettings.INCOMING_RECEIVE)
        out = Shipment.objects.create(carrier=self.ups, tracking_number='1ZOUT0000001', status='in_transit',
                                      recipient_name='ACME')
        self.assertEqual(incoming.on_delivered(out), {})
