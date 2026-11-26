"""🛒 Замовлення JLCPCB → закупівля Minerva (jlcpcb/services/purchase.py) і 📦 трек-номер без ручного вводу
(jlcpcb/services/tracking.py): одна закупівля на замовлення, оприбуткування одним шляхом, доставлена посилка →
«Доставлено», історія без руху складу."""
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from inventory.models import InventoryTransaction, Product, PurchaseOrder, Supplier
from jlcpcb.models import JLCConfig, JLCOrder, JLCOrderLine
from jlcpcb.services import purchase, tracking


def _batch(status='shipped', **kw):
    """Як W2026111923107730: 4 файли, кожен — свій товар."""
    prods = [Product.objects.create(sku=s, name=s) for s in ('AN120202-01H', 'AN220206-001', 'AN220214-01A', 'AN220207-001')]
    order = JLCOrder.objects.create(
        jlc_order_id='W2026111923107730', jlc_order_number='W2026111923107730',
        description='AN120202-01H_2L_FR4_0.8x160x77.5mm_Y443', quantity=30, product=prods[0],
        mapping_status='matched', local_status=status, tracking_carrier='CAUPS', currency='USD', **kw)
    for fname, qty, price, prod in (
            ('AN120202-01H_2L_FR4_0.8x160x77.5mm_Y443', 30, '36.31', prods[0]),
            ('AN220206-001_Rev002_2L_FR4_1.6x172.5x196_Y446', 10, '33.31', prods[1]),
            ('AN220214-01A_2L_FR4_1x90x150_Y444', 10, '15.56', prods[2]),
            ('AN220207-001_Rev002_2L_FR4_1.6x210x272.5_Y445', 10, '53.78', prods[3])):
        JLCOrderLine.objects.create(order=order, file_name=fname, quantity=qty, price=Decimal(price), product=prod)
    return order, prods


class PurchaseSyncTests(TestCase):
    def test_one_purchase_with_lines_no_duplicates(self):
        order, prods = _batch()
        po = purchase.sync(order)
        order.refresh_from_db()
        self.assertEqual(order.purchase_order, po)
        self.assertEqual((po.supplier.name, po.status, po.external_source, po.external_id),
                         ('JLCPCB', 'ordered', 'jlcpcb', 'W2026111923107730'))
        lines = list(po.lines.order_by('pk'))
        self.assertEqual([(l.product.sku, l.qty_ordered, l.unit_price) for l in lines],
                         [('AN120202-01H', 30, Decimal('1.21')), ('AN220206-001', 10, Decimal('3.33')),
                          ('AN220214-01A', 10, Decimal('1.56')), ('AN220207-001', 10, Decimal('5.38'))])
        self.assertEqual(po.total_price, Decimal('138.96'))
        self.assertIn('AN220214-01A', po.ordered_skus)
        purchase.sync(order)                                    # повтор — та сама закупівля
        self.assertEqual(PurchaseOrder.objects.filter(external_source='jlcpcb').count(), 1)
        self.assertEqual(po.lines.count(), 4)
        # «Очікується» на складі бачить плати JLC
        from inventory.services.stock import incoming_info
        self.assertEqual(incoming_info([prods[2].pk])[prods[2].pk]['qty'], 10)
        self.assertEqual(Supplier.objects.filter(name='JLCPCB').count(), 1)

    def test_old_receipt_mirrored_without_new_stock(self):
        order, prods = _batch(status='delivered')
        JLCOrder.objects.filter(pk=order.pk).update(received_qty=30)    # колись оприбутковано «замовлення» = 1-ша позиція
        order.refresh_from_db()
        PurchaseOrder.objects.all().delete()
        po = purchase.sync(order, history=False)
        first = po.lines.get(external_sku='AN120202-01H')
        self.assertEqual(first.qty_received, 30)
        self.assertEqual(InventoryTransaction.objects.count(), 0)        # позначка, не новий рух складу

    def test_history_delivered_before_purchase(self):
        order, _ = _batch(status='delivered', delivered_date=timezone.localdate() - timezone.timedelta(days=60))
        po = purchase.sync(order)
        self.assertEqual(po.status, 'received')
        self.assertTrue(all(l.qty_received == l.qty_ordered for l in po.lines.all()))
        self.assertEqual(InventoryTransaction.objects.count(), 0)
        self.assertIn('без руху складу', po.notes)

    def test_recent_delivered_stays_open_for_receiving(self):
        order, _ = _batch(status='delivered', delivered_date=timezone.localdate())
        po = purchase.sync(order)
        self.assertEqual(po.status, 'ordered')                          # свіже — не історія, можна оприбуткувати
        self.assertEqual(len(purchase.receive(order)), 4)

    def test_ignored_cancels_open_purchase(self):
        order, _ = _batch()
        po = purchase.sync(order)
        JLCOrder.objects.filter(pk=order.pk).update(mapping_status='ignored')
        order.refresh_from_db()
        purchase.sync(order)
        po.refresh_from_db()
        self.assertEqual(po.status, 'cancelled')

    def test_sync_all_creates_for_every_order(self):
        _batch()
        JLCOrder.objects.create(jlc_order_id='W1', jlc_order_number='W1', quantity=5, local_status='in_production',
                                description='X1_2L', total_price=Decimal('10'))
        JLCOrder.objects.create(jlc_order_id='W2', jlc_order_number='W2', mapping_status='track_only')
        st = purchase.sync_all()
        self.assertEqual(st['created'], 2)
        po = PurchaseOrder.objects.get(external_id='W1')
        self.assertEqual((po.lines.get().external_line_id, po.lines.get().qty_ordered), ('jlc:order', 5))


class DeliverTests(TestCase):
    def test_deliver_receives_all_lines_once(self):
        order, prods = _batch()
        got = purchase.deliver(order, source='тест')
        order.refresh_from_db()
        self.assertEqual(order.local_status, 'delivered')
        self.assertEqual(sorted(got), sorted([('AN120202-01H', Decimal('30')), ('AN220206-001', Decimal('10')),
                                              ('AN220214-01A', Decimal('10')), ('AN220207-001', Decimal('10'))]))
        self.assertEqual(InventoryTransaction.objects.filter(tx_type='Incoming').count(), 4)
        self.assertEqual(order.purchase_order.status, 'received')
        self.assertTrue(all(l.is_received for l in order.lines.all()))
        self.assertEqual(purchase.receive(order), [])                    # удруге — нічого
        self.assertEqual(InventoryTransaction.objects.count(), 4)

    def test_no_auto_receive_leaves_purchase_open(self):
        cfg = JLCConfig.get()
        cfg.auto_receive_on_delivered = False
        cfg.save()
        order, _ = _batch()
        self.assertEqual(purchase.deliver(order), [])
        order.refresh_from_db()
        po = order.purchase_order
        self.assertEqual((order.local_status, po.status), ('delivered', 'ordered'))
        self.assertIsNotNone(po.delivered_reported_at)
        self.assertTrue(purchase.has_open_stock_lines(order))
        line = order.lines.get(file_name__startswith='AN220214')
        self.assertEqual(purchase.receive(order, line=line), [('AN220214-01A', Decimal('10'))])
        po.refresh_from_db()
        self.assertEqual(po.status, 'partial')

    def test_parcel_delivered_marks_order_delivered(self):
        from shipping.models import Carrier, Shipment
        order, _ = _batch()
        ups = Carrier.objects.create(name='UPS', carrier_type='ups', is_active=True)
        sh = Shipment.objects.create(carrier=ups, tracking_number='1ZF1F5420410259015', status='in_transit',
                                     jlc_order=order, direction='in')
        with self.captureOnCommitCallbacks(execute=True):
            sh.status = 'delivered'
            sh.delivered_at = timezone.now()
            sh.save()
        order.refresh_from_db()
        self.assertEqual(order.local_status, 'delivered')
        self.assertEqual(order.purchase_order.status, 'received')
        self.assertEqual(InventoryTransaction.objects.filter(tx_type='Incoming').count(), 4)


class TrackingTests(TestCase):
    def test_from_raw_and_text(self):
        raw = {'orderItem': [{'pcbItem': {'fileName': 'X'}}], 'logistics': {'expressNo': '1zf1f5420410259015',
                                                                            'expressCompany': 'UPS'}}
        self.assertEqual(tracking.from_raw(raw), ('1ZF1F5420410259015', 'UPS'))
        self.assertIsNone(tracking.from_raw({'batchNum': 'W2026111923107730', 'price': '12'}))
        self.assertEqual(tracking.from_text('Your order W2026111923107730 shipped. Tracking number: 1234567890'),
                         ['1234567890'])
        self.assertEqual(tracking.from_text('UPS 1ZF1F5420410259015 · call +49 89 1234567'), ['1ZF1F5420410259015'])

    def test_mail_sets_tracking_and_shipped(self):
        from email_assistant.models import EmailAccount, EmailMessage
        from django.contrib.auth.models import User
        order, _ = _batch(status='in_production')
        JLCOrder.objects.filter(pk=order.pk).update(tracking_carrier='')
        u = User.objects.create_user('m', 'm@x.de', 'x')
        acc = EmailAccount.objects.create(user=u, email_address='m@x.de')
        with self.captureOnCommitCallbacks(execute=True):
            EmailMessage.objects.create(
                account=acc, from_email='support@jlcpcb.com', subject='Your JLCPCB order has been shipped',
                body_text='Order W2026111923107730 was shipped via UPS. Tracking No: 1ZF1F5420410259015',
                sent_at=timezone.now(), message_id='<t1@jlc>', imap_uid=1, imap_folder_name='INBOX')
        order.refresh_from_db()
        self.assertEqual((order.tracking_number, order.local_status, order.tracking_carrier),
                         ('1ZF1F5420410259015', 'shipped', 'UPS'))
        self.assertEqual(order.purchase_order.tracking_number, '1ZF1F5420410259015')
