"""Що в замовленні JLCPCB — усі позиції в сповіщеннях, списку й картці (jlcpcb/contents.py)."""
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from inventory.models import Product
from jlcpcb import contents
from jlcpcb.models import JLCConfig, JLCOrder, JLCOrderLine


def _batch():
    """Замовлення як W2026111223237251: 5 файлів, у двох товарів — по дві частини."""
    a = Product.objects.create(sku='AN220210-01C', name='Antenna A')
    b = Product.objects.create(sku='AN220203-02A', name='Antenna B')
    c = Product.objects.create(sku='AN220215-01A', name='Antenna C')
    order = JLCOrder.objects.create(
        jlc_order_id='W2026111223237251', jlc_order_number='W2026111223237251',
        description='AN220210-01C_Rev002_2L_FR4_140x140x1_Y440', quantity=5,
        product=a, local_status='shipped', tracking_carrier='CAUPS',
    )
    for fname, qty, prod in (
        ('AN220210-01C_Rev002_2L_FR4_140x140x1_Y440', 5, a),
        ('AN220203-02A_2L_FR4_1x180.6x180.6_part_2_Y437', 10, b),
        ('AN220215-01A_2L_FR4_0.8x110x110_board_1_Y434', 25, c),
        ('AN220203-02A_2L_FR4_1x180.6x180.6_part_1_Y436', 10, b),
        ('AN220215-01A_2L_FR4_0.8x110x110_board_2_Y435', 25, c),
    ):
        JLCOrderLine.objects.create(order=order, file_name=fname, quantity=qty, product=prod,
                                    status_key='shipped', produce_code=fname[-4:])
    return order


class ItemsTests(TestCase):
    def test_all_lines_grouped_with_variants(self):
        found = contents.items(_batch())
        self.assertEqual(
            [(it.name, it.qty, it.variant) for it in found],
            [('AN220210-01C', 5, ''),
             ('AN220203-02A', 10, 'part 1'), ('AN220203-02A', 10, 'part 2'),
             ('AN220215-01A', 25, 'board 1'), ('AN220215-01A', 25, 'board 2')],
        )
        self.assertEqual(contents.total_qty(found), 75)
        self.assertEqual(contents.compact(found),
                         'AN220210-01C ×5 · AN220203-02A ×10+10 · AN220215-01A ×25+25')

    def test_variant_of_strips_layers_material_size_code(self):
        self.assertEqual(contents.variant_of('AN220203-02A_2L_FR4_1x180.6x180.6_part_2_Y437', 'AN220203-02A'),
                         'part 2')
        self.assertEqual(contents.variant_of('AN120202-01H_2L_FR4_0.8x160x77.5mm_', 'AN120202-01H'), '')

    def test_same_variant_falls_back_to_produce_code(self):
        p = Product.objects.create(sku='FT220407', name='Filter')
        order = JLCOrder.objects.create(jlc_order_id='W1', product=p)
        JLCOrderLine.objects.create(order=order, file_name='FT220407_2L_FR4_10x10', quantity=5,
                                    product=p, produce_code='Y1')
        JLCOrderLine.objects.create(order=order, file_name='FT220407_2L_FR4_20x20', quantity=5,
                                    product=p, produce_code='Y2')
        self.assertEqual([it.variant for it in contents.items(order)], ['Y1', 'Y2'])

    def test_unlinked_line_named_by_sku_in_file_name(self):
        order = JLCOrder.objects.create(jlc_order_id='W2')
        JLCOrderLine.objects.create(order=order, file_name='AN999999-01A_2L_FR4_10x10_Y1', quantity=3)
        JLCOrderLine.objects.create(order=order, file_name='weird board', quantity=2)
        found = contents.items(order)
        self.assertEqual([(it.name, it.linked) for it in found],
                         [('AN999999-01A', False), ('weird board', False)])

    def test_raw_data_when_no_lines_yet(self):
        order = JLCOrder.objects.create(jlc_order_id='W3', quantity=7, raw_data={'orderItem': [
            {'pcbItem': {'fileName': 'AN1-01A_2L_FR4_10x10_Y1', 'count': 7, 'orderStatus': None}},
            {'pcbItem': {'fileName': 'AN2-01A_2L_FR4_10x10_Y2', 'count': 3}},
        ]})
        found = contents.items(order)
        self.assertEqual([(it.name, it.qty) for it in found], [('AN1-01A', 7), ('AN2-01A', 3)])

    def test_order_itself_without_lines_or_raw(self):
        p = Product.objects.create(sku='AN220213', name='Antenna')
        order = JLCOrder.objects.create(jlc_order_id='W4', quantity=4, product=p, description='x')
        found = contents.items(order)
        self.assertEqual([(it.name, it.qty, it.linked) for it in found], [('AN220213', 4, True)])


class NotificationTests(TestCase):
    def setUp(self):
        cfg = JLCConfig.get()
        cfg.notify_telegram = True
        cfg.notify_email = True
        cfg.notify_email_to = 'team@example.com'
        cfg.save()

    def _send(self, order, old='in_production', new='shipped'):
        from jlcpcb import notifications
        with mock.patch.object(notifications, '_send_telegram') as tg, \
                mock.patch.object(notifications, '_send_html_email') as mail:
            notifications.notify_jlc_status_change(order, old, new, force=True)
        return tg.call_args[0][0], mail.call_args[0]

    def test_telegram_lists_every_line(self):
        text, (subject, html, plain, to) = self._send(_batch())
        self.assertIn('Позицій: <b>5</b> · разом <b>75</b> шт.', text)
        for line in ('🏷 <b>AN220210-01C</b> × 5', '🏷 <b>AN220203-02A</b> × 10 · part 1',
                     '🏷 <b>AN220203-02A</b> × 10 · part 2', '🏷 <b>AN220215-01A</b> × 25 · board 1',
                     '🏷 <b>AN220215-01A</b> × 25 · board 2'):
            self.assertIn(line, text)
        self.assertNotIn('Кількість: <b>5</b>', text)
        # Лист — теж усі позиції
        for name in ('AN220203-02A', 'part 2', 'AN220215-01A', 'board 2'):
            self.assertIn(name, html)
            self.assertIn(name, plain)
        self.assertIn('позицій: 5', html)

    def test_line_with_other_status_is_marked(self):
        order = _batch()
        order.lines.filter(file_name__contains='part_2').update(status_key='in_production')
        text, _ = self._send(order)
        self.assertIn('× 10 · part 2 — 🏭 У виробництві', text)
        self.assertNotIn('× 10 · part 1 —', text)

    def test_single_line_keeps_short_format(self):
        p = Product.objects.create(sku='FT220407', name='Filter')
        order = JLCOrder.objects.create(jlc_order_id='W5', quantity=5, product=p)
        JLCOrderLine.objects.create(order=order, file_name='FT220407_2L_FR4_10x10', quantity=5, product=p)
        text, _ = self._send(order)
        self.assertIn('Кількість: <b>5</b> шт.\n🏷 Товар: <b>FT220407</b>', text)
        self.assertNotIn('Позицій', text)

    def test_names_are_escaped(self):
        order = JLCOrder.objects.create(jlc_order_id='W6')
        JLCOrderLine.objects.create(order=order, file_name='<b>x</b>', quantity=1)
        JLCOrderLine.objects.create(order=order, file_name='a&b', quantity=1)
        text, (_s, html, _p, _t) = self._send(order)
        self.assertIn('&lt;b&gt;x&lt;/b&gt;', text)
        self.assertNotIn('<b>x</b>', text)
        self.assertNotIn('<b>x</b>', html)

    def test_long_order_is_cut_in_telegram(self):
        order = JLCOrder.objects.create(jlc_order_id='W7')
        for i in range(contents.MAX_TG_ITEMS + 3):
            JLCOrderLine.objects.create(order=order, file_name=f'AN{i:06d}-01A_2L', quantity=2)
        text, _ = self._send(order)
        self.assertIn('… ще 3 поз. (6 шт.)', text)

    def test_active_summary_shows_all_products(self):
        _batch()
        from jlcpcb import notifications
        with mock.patch.object(notifications, '_send_telegram') as tg, \
                mock.patch.object(notifications, '_send_html_email') as mail:
            notifications.notify_jlc_active_orders_summary()
        self.assertIn('AN220203-02A ×10+10', tg.call_args[0][0])
        self.assertIn('AN220215-01A ×25+25', mail.call_args[0][1])


class AdminTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        self.client.force_login(User.objects.create_superuser('slava', 's@x.y', 'p'))
        self.order = _batch()

    def test_list_shows_total_and_contents(self):
        r = self.client.get('/admin/jlcpcb/jlcorder/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for row in ('AN220210-01C <b>×5</b>', 'AN220203-02A <b>×10+10</b>', 'AN220215-01A <b>×25+25</b>'):
            self.assertIn(row, html)
        self.assertIn('<b>75</b>', html)
        self.assertIn('5 поз.', html)

    def test_card_chips_show_every_product(self):
        r = self.client.get(f'/admin/jlcpcb/jlcorder/{self.order.pk}/change/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for chip in ('🏷 AN220210-01C ×5', '🏷 AN220203-02A ×10+10', '🏷 AN220215-01A ×25+25',
                     '🔢 75 шт. · позицій: 5'):
            self.assertIn(chip, html)
