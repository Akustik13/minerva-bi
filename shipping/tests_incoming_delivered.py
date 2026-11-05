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


class Base(TestCase):
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


class IncomingDeliveredTests(Base):
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


class CatchUpTests(Base):
    """Посилку доставили до того, як увімкнули налаштування (статус змінився без сигналу) — «Перевірити зараз»."""

    def old_delivered(self):
        from django.utils import timezone
        Shipment.objects.filter(pk=self.parcel.pk).update(status='delivered', delivered_at=timezone.now())

    def test_pending_and_catch_up(self):
        self.old_delivered()
        self.assertEqual([s.pk for s in incoming.pending_delivered()], [self.parcel.pk])
        out = incoming.catch_up()                                   # «лише повідомити» — нічого не робить
        self.assertEqual((out['mode'], out['done']), ('notify', []))
        self.mail.assert_not_called()
        self.mode(InventorySettings.INCOMING_RECEIVE)
        out = incoming.catch_up()
        self.assertEqual(len(out['done']), 1)
        self.assertIn('✅', incoming.outcome_text(out['done'][0][1]))
        self.line.refresh_from_db()
        self.assertEqual(self.line.qty_received, Decimal('1000'))
        self.assertEqual(incoming.pending_delivered(), [])
        self.assertEqual(incoming.catch_up()['done'], [])           # удруге — нічого

    def test_po_check_button_refreshes_tracking(self):
        from django.contrib.auth.models import User
        self.client.force_login(User.objects.create_superuser('boss', 'b@x.y', 'p'))
        self.mode(InventorySettings.INCOMING_RECEIVE)

        def track(parcel, dry_run=False):
            parcel.status = 'delivered'
            parcel.save()
            return True, [{'tracker': 'ups'}]
        page = self.client.get(f'/admin/inventory/purchaseorder/{self.po.pk}/change/').content.decode()
        self.assertIn('🔄 Перевірити посилки зараз', page)
        with mock.patch('shipping.services.tracking_engine.track_with_fallback', side_effect=track), \
                self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(f'/admin/inventory/purchaseorder/{self.po.pk}/parcels/check/', follow=True)
        self.line.refresh_from_db()
        self.assertEqual(self.line.qty_received, Decimal('1000'))   # оприбутковано один раз
        self.assertEqual(self.po.receipts.count(), 1)
        self.assertContains(r, 'Доставлено (оновлено)')
        self.assertEqual(self.client.get(f'/admin/inventory/purchaseorder/{self.po.pk}/parcels/check/')
                         .status_code, 302)                         # GET нічого не робить

    def test_settings_panel_and_button(self):
        from django.contrib.auth.models import User
        self.client.force_login(User.objects.create_superuser('boss', 'b@x.y', 'p'))
        self.old_delivered()
        page = self.client.get('/admin/inventory/inventorysettings/', follow=True).content.decode()
        self.assertIn(self.po.code, page)
        self.assertNotIn('📥 Застосувати до цих посилок зараз', page)   # «лише повідомити» — без кнопки
        self.mode(InventorySettings.INCOMING_RECEIVE)
        page = self.client.get('/admin/inventory/inventorysettings/', follow=True).content.decode()
        self.assertIn('📥 Застосувати до цих посилок зараз', page)
        r = self.client.post('/admin/inventory/inventorysettings/incoming/catch-up/', follow=True)
        self.assertContains(r, f'✅ {self.po.code}: оприбутковано на склад')
        self.line.refresh_from_db()
        self.assertEqual(self.line.qty_received, Decimal('1000'))
