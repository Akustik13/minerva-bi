"""📥 Вхідні посилки на сторінці «Відправлення» (Shipment.direction = in, shipping/services/incoming.py): напрямок у
списку, «📡 Додати трекінг» з закупівлею, картка посилки й закупівля з її посилками.

python manage.py test shipping.tests_incoming --settings=tabele.settings_test
"""
from django.contrib.auth.models import User
from django.test import TestCase

from inventory.models import PurchaseOrder, Supplier
from sales.models import SalesOrder
from shipping.models import Carrier, Shipment
from shipping.services import incoming, ship_advice

TN = '1Z2R5D130407163813'


class IncomingTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        self.jum = Carrier.objects.create(name='Jumingo', carrier_type='jumingo', is_active=True, is_default=True)
        self.ups = Carrier.objects.create(name='UPS', carrier_type='ups', is_active=True, sender_company='Sevskiy GmbH',
                                          sender_country='DE', sender_city='München')
        self.sup = Supplier.objects.create(name='Raynool', addr_country='CN')
        self.po = PurchaseOrder.objects.create(supplier=self.sup, status='ordered')
        self.user = User.objects.create_superuser('boss', 'b@x.y', 'p')
        self.client.force_login(self.user)

    def test_create_picks_carrier_and_fills_po(self):
        s = incoming.create(TN, purchase_order=self.po)
        self.assertEqual((s.direction, s.carrier_id, s.sender_company, s.sender_country, s.recipient_company),
                         (Shipment.IN, self.ups.pk, 'Raynool', 'CN', 'Sevskiy GmbH'))
        self.po.refresh_from_db()
        self.assertEqual((self.po.tracking_number, self.po.shipping_service), (TN, 'UPS'))
        with self.assertRaises(incoming.Duplicate):
            incoming.create(TN.lower())
        self.assertEqual(incoming.carrier_for('123456', 'dhl').pk, self.jum.pk)   # DHL немає — за замовчуванням

    def test_add_tracking_incoming_with_po(self):
        page = self.client.get(f'/admin/shipping/shipment/add-tracking/?direction=in&po={self.po.pk}')
        self.assertContains(page, '📥 До нас — посилка від постачальника')
        self.assertContains(page, f'value="{self.po.pk}" data-supplier="Raynool" selected')
        r = self.client.post('/admin/shipping/shipment/add-tracking/', {
            'direction': 'in', 'purchase_order': self.po.pk, 'tracking_number': TN, 'carrier': ''})
        s = Shipment.objects.get()
        self.assertRedirects(r, f'/admin/shipping/shipment/{s.pk}/detail/', fetch_redirect_response=False)
        self.assertEqual((s.direction, s.purchase_order_id, s.carrier_id), (Shipment.IN, self.po.pk, self.ups.pk))
        r = self.client.post('/admin/shipping/shipment/add-tracking/', {'direction': 'in', 'tracking_number': TN})
        self.assertEqual(Shipment.objects.count(), 1)                 # той самий номер вдруге не заводиться

    def test_add_tracking_outgoing_without_carrier(self):
        self.client.post('/admin/shipping/shipment/add-tracking/', {'tracking_number': TN, 'carrier': ''})
        s = Shipment.objects.get()
        self.assertEqual((s.direction, s.carrier_id), (Shipment.OUT, self.ups.pk))

    def test_list_direction_switch(self):
        incoming.create(TN, purchase_order=self.po)
        Shipment.objects.create(carrier=self.ups, tracking_number='1ZOUT0000001', status='in_transit',
                                recipient_name='ACME')
        page = self.client.get('/admin/shipping/shipment/?direction__exact=in')
        self.assertContains(page, '📥 До нас')
        self.assertContains(page, f'🛒 {self.po.code}')
        self.assertNotContains(page, '1ZOUT0000001')
        page = self.client.get('/admin/shipping/shipment/?direction__exact=out')
        self.assertContains(page, '1ZOUT0000001')
        self.assertContains(page, 'status__exact=in_transit&amp;direction__exact=out')

    def test_detail_and_po_page(self):
        s = incoming.create(TN, purchase_order=self.po)
        page = self.client.get(f'/admin/shipping/shipment/{s.pk}/detail/')
        self.assertContains(page, '📥 Вхідна посилка')
        self.assertNotContains(page, 'Посилку доставлено')
        Shipment.objects.filter(pk=s.pk).update(status='delivered')
        page = self.client.get(f'/admin/shipping/shipment/{s.pk}/detail/')
        self.assertContains(page, '✅ Посилку доставлено — відкрийте закупівлю')
        page = self.client.get(f'/admin/inventory/purchaseorder/{self.po.pk}/change/')
        self.assertContains(page, f'📥 #{s.pk} · UPS · {TN}')
        self.assertContains(page, f'add-tracking/?direction=in&amp;po={self.po.pk}')

    def test_incoming_not_in_customer_history(self):
        incoming.create(TN, purchase_order=self.po)
        Shipment.objects.filter(tracking_number=TN).update(recipient_country='DE')
        order = SalesOrder.objects.create(source='webshop', order_number='77001', addr_country='DE')
        self.assertEqual(ship_advice.history(order)['country'], {})
