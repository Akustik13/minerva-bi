"""🤖 Агент відправки — перевірка замовлення й вибір перевізника (shipping/services/ship_advice.py), картка на сторінці
нового відправлення. Перевізники не викликаються: тарифи — готові рядки, як їх віддає «Порівняти тарифи».

    python manage.py test shipping.tests_ship_advice --settings=tabele.settings_test
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.utils import timezone

from inventory.models import Product
from sales.models import SalesOrder, SalesOrderLine
from shipping.models import Carrier, Shipment
from shipping.services import ship_advice


def rates(*rows):
    """rows: (carrier, [(code, name, price, currency, days), ...]) → відповідь «Порівняти тарифи»."""
    out = []
    for carrier, products in rows:
        out.append({'carrier_id': carrier.pk, 'carrier_name': carrier.name, 'carrier_type': carrier.carrier_type,
                    'note': None, 'error': None,
                    'products': [{'code': c, 'name': n, 'price': p, 'currency': cur, 'transit_days': d}
                                 for c, n, p, cur, d in products]})
    return out


class Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        self.ups = Carrier.objects.create(name='UPS', carrier_type='ups', is_active=True)
        self.dhl = Carrier.objects.create(name='DHL Express', carrier_type='dhl', is_active=True)
        self.jum = Carrier.objects.create(name='Jumingo', carrier_type='jumingo', is_active=True)
        self.ant = Product.objects.create(sku='AN220213-01C', name='Antenna', net_weight_g=Decimal('45'),
                                          hs_code='85177900')
        self.n = 0

    def order(self, country='DE', courier='', email='a@acme.example', **kw):
        self.n += 1
        o = SalesOrder.objects.create(source='webshop', order_number=f'90{self.n}', client='ACME', email=email,
                                      addr_street='Hauptstr. 1', addr_city='Berlin', addr_zip='10115',
                                      addr_country=country, ship_phone='+49 30 1', shipping_courier=courier, **kw)
        SalesOrderLine.objects.create(order=o, product=self.ant, sku_raw=self.ant.sku, qty=Decimal('2'))
        return o

    def shipped(self, order, carrier, service, country='DE'):
        return Shipment.objects.create(order=order, carrier=carrier, status=Shipment.Status.DELIVERED,
                                       carrier_service=service, recipient_country=country)

    def offers(self):
        return rates((self.ups, [('65', 'UPS Express Saver', 30.0, 'EUR', 2), ('11', 'UPS Standard', 18.0, 'EUR', 4)]),
                     (self.dhl, [('P', 'DHL Express Worldwide', 41.0, 'EUR', 1)]),
                     (self.jum, [('t1', 'DPD Classic', 12.5, 'EUR', 3), ('s-2', 'Hermes Shop', 9.0, 'EUR', 7)]))


class CheckTests(Base):
    def test_ready_order(self):
        o = self.order()
        self.assertEqual([c for c in ship_advice.checks(o) if c['level'] == 'block'], [])

    def test_blocks_and_warnings(self):
        o = self.order(country='US', status='cancelled')
        SalesOrder.objects.filter(pk=o.pk).update(addr_street='', addr_city='', ship_phone='')
        o.refresh_from_db()
        Product.objects.filter(pk=self.ant.pk).update(hs_code='')
        levels = {c['text']: c['level'] for c in ship_advice.checks(o)}
        self.assertEqual(levels['замовлення скасовано'], 'block')
        self.assertEqual(levels['немає адреси доставки'], 'block')
        self.assertTrue(any(t.startswith('поза ЄС') for t in levels))
        self.assertTrue(any(t.startswith('немає телефону') for t in levels))
        self.assertTrue(any(t.startswith('бракує AN220213-01C') for t in levels))


class RecommendTests(Base):
    def test_cheapest_fast_by_default(self):
        a = ship_advice.recommend(self.order(), self.offers())
        self.assertEqual((a['rule'], a['pick']['name']), ('fast_cheap', 'DPD Classic'))
        self.assertEqual(a['cheapest']['name'], 'Hermes Shop')          # найдешевший, але 7 днів
        self.assertEqual(a['fastest']['name'], 'DHL Express Worldwide')

    def test_customer_choice_wins(self):
        a = ship_advice.recommend(self.order(courier='UPS'), self.offers())
        self.assertEqual((a['rule'], a['pick']['name']), ('customer', 'UPS Standard'))
        self.assertIn('клієнт вибрав «UPS»', a['reason'])
        self.assertIn('у замовленні не сказано', a['reason'])
        self.assertIn('дорожче за найдешевший', a['extra'])
        # послуга цього перевізника — як минулого разу цьому клієнту
        self.shipped(self.order(), self.ups, 'UPS Express Saver')
        a = ship_advice.recommend(self.order(courier='UPS'), self.offers())
        self.assertEqual(a['pick']['name'], 'UPS Express Saver')
        self.assertIn('як минулого разу', a['reason'])

    def test_history_of_customer_then_country(self):
        past = self.order()
        self.shipped(past, self.dhl, 'DHL Express Worldwide')
        a = ship_advice.recommend(self.order(), self.offers())
        self.assertEqual((a['rule'], a['pick']['carrier_type']), ('history_customer', 'dhl'))
        self.assertIn('цьому клієнту (1 раз.)', a['reason'])
        other = self.order(email='b@other.example')
        self.shipped(other, self.ups, 'UPS Express Saver', country='AT')
        a = ship_advice.recommend(self.order(country='AT', email='c@new.example'), self.offers())
        self.assertEqual((a['rule'], a['pick']['name']), ('history_country', 'UPS Express Saver'))
        self.assertIn('в AT', a['reason'])

    def test_old_and_failed_shipments_ignored(self):
        past = self.order()
        s = self.shipped(past, self.dhl, 'DHL Express Worldwide')
        Shipment.objects.filter(pk=s.pk).update(created_at=timezone.now() - timedelta(days=400))
        Shipment.objects.create(order=past, carrier=self.dhl, status=Shipment.Status.ERROR,
                                carrier_service='DHL Express Worldwide', recipient_country='DE')
        self.assertEqual(ship_advice.recommend(self.order(), self.offers())['rule'], 'fast_cheap')

    def test_no_rates_and_errors(self):
        self.assertIsNone(ship_advice.recommend(self.order(), []))
        bad = [{'carrier_id': self.ups.pk, 'carrier_type': 'ups', 'error': 'API ключ не налаштовано',
                'products': [{'code': '65', 'name': 'x', 'price': 1}]}]
        self.assertIsNone(ship_advice.recommend(self.order(), bad))

    def test_currency_without_rate_not_cheapest(self):
        offers = rates((self.ups, [('65', 'UPS Express Saver', 5.0, 'XYZ', 2)]),
                       (self.jum, [('t1', 'DPD Classic', 12.5, 'EUR', 3)]))
        a = ship_advice.recommend(self.order(), offers)
        self.assertEqual(a['pick']['name'], 'DPD Classic')


class PageTests(Base):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser('boss', 'b@x.y', 'p')

    def test_advice_in_rates_answer(self):
        from django.contrib import admin
        ma = admin.site._registry[Shipment]
        req = RequestFactory().post('/')
        req.user = self.user
        o = self.order(courier='DHL')
        a = ma._ship_advice(req, str(o.pk), self.offers())
        self.assertEqual(a['pick']['carrier_type'], 'dhl')
        self.assertIsNone(ma._ship_advice(req, '', self.offers()))
        req.user = User.objects.create_user('clerk', 'c@x.y', 'p')
        self.assertIsNone(ma._ship_advice(req, str(o.pk), self.offers()))

    def test_create_page_shows_agent(self):
        o = self.order()
        self.client.force_login(self.user)
        page = self.client.get(f'/admin/shipping/shipment/create/{o.pk}/')
        self.assertContains(page, '🤖 Агент відправки')
        self.assertContains(page, 'бракує AN220213-01C')
        self.assertContains(page, f"order_id: '{o.pk}'")
