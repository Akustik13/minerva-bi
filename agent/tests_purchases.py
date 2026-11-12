"""🤖 Агент і закупівлі (agent/purchases.py): посилка від постачальника в листі → закупівля з позиціями й вхідна
посилка «📥 До нас» на сторінці «Відправлення»; застосовує людина (✓). Ще — переслана частина листа й вкладення,
якого ще немає на диску (з поштового сервера).

python manage.py test agent.tests_purchases --settings=tabele.settings_test
"""
import json
import os
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone

from agent import decide, mcp, tools
from agent.models import AgentProposal
from agent.tests_tasks import Base, pdf_bytes
from inventory.models import IncomingShipment, Product, PurchaseOrder, Supplier
from shipping.models import Carrier, Shipment

TN = '1Z2R5D130407163813'


class PurchaseTests(Base):
    def setUp(self):
        super().setUp()
        self.ups = Carrier.objects.create(name='UPS', carrier_type='ups', is_active=True, sender_company='Sevskiy GmbH',
                                          sender_city='München', sender_country='DE')
        self.cab = Product.objects.create(sku='CA-MHF4-MC1.13-075-MHF4', name='Cable 75 mm')
        self.cab2 = Product.objects.create(sku='CA-MHF4-MC1.13-100-MHF4', name='Cable 100 mm')

    def ctx(self):
        return tools.Context(self.task('Занеси закупівлю з листа й трек-номер.', products=[]))

    def call(self, ctx, tool, ok=True, **args):
        out, err = tools.run(ctx, tool, args)
        self.assertEqual(err, not ok, out)
        return out

    def propose(self, ctx, **extra):
        args = {'supplier': {'name': 'Raynool', 'email': 'oscar@raynool.com', 'country': 'CN'},
                'supplier_order_no': 'INV-1750', 'currency': 'USD', 'tracking_number': TN, 'carrier': 'UPS',
                'lines': [{'sku': 'CA-MHF4-MC1.13-075-MHF4', 'qty': 300, 'unit_price': 0.85},
                          {'sku': 'ca-mhf4-mc1.13-100-mhf4', 'qty': 400}],
                'evidence': 'UPS Tracking-No.: 1Z2R5D130407163813', 'source': 'WG: RFQ … 04.11.2026'}
        args.update(extra)
        return self.call(ctx, 'propose_purchase_order', **args)

    def test_tools_offered_only_with_right(self):
        names = {t['name'] for t in tools.definitions(self.task('x', products=[]))}
        self.assertTrue({'find_purchase_orders', 'propose_purchase_order', 'propose_incoming_parcel'} <= names)
        self.assertIn('propose_purchase_order', mcp.names(self.user))
        clerk = User.objects.create_user('clerk', 'c@x.y', 'p', is_staff=True)
        task = self.task('x', products=[])
        task.owner = clerk
        self.assertNotIn('find_purchase_orders', {t['name'] for t in tools.definitions(task)})

    def test_propose_and_apply_purchase_with_parcel(self):
        ctx = self.ctx()
        self.propose(ctx)
        p = ctx.task.proposals.get()
        self.assertEqual(p.kind, AgentProposal.PURCHASE)
        self.assertEqual(p.data['carrier_name'], 'UPS')
        self.assertEqual([l['sku'] for l in p.data['lines']], ['CA-MHF4-MC1.13-075-MHF4', 'CA-MHF4-MC1.13-100-MHF4'])
        what, _old, new = decide.describe(p)
        self.assertEqual(what, '🛒 Закупівля')
        self.assertIn('Raynool: 2 поз., 700 шт', new)
        self.assertEqual(PurchaseOrder.objects.count(), 0)              # до ✓ нічого не створено
        decide.apply_selected(ctx.task, [p.pk], self.user)
        p.refresh_from_db()
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        po = PurchaseOrder.objects.get()
        self.assertEqual((po.supplier.name, po.status, po.supplier_order_no, po.currency, po.tracking_number),
                         ('Raynool', 'ordered', 'INV-1750', 'USD', TN))
        self.assertEqual(sorted((l.product.sku, int(l.qty_ordered)) for l in po.lines.all()),
                         [('CA-MHF4-MC1.13-075-MHF4', 300), ('CA-MHF4-MC1.13-100-MHF4', 400)])
        s = Shipment.objects.get()
        self.assertEqual((s.direction, s.purchase_order_id, s.tracking_number, s.carrier_id, s.status),
                         (Shipment.IN, po.pk, TN, self.ups.pk, 'in_transit'))
        self.assertEqual(s.sender_company, 'Raynool')
        self.assertEqual(Supplier.objects.get().email, 'oscar@raynool.com')
        self.assertTrue(p.result_url.endswith(f'/purchaseorder/{po.pk}/change/'))

    def test_no_duplicates(self):
        ctx = self.ctx()
        self.propose(ctx)
        decide.apply_selected(ctx.task, list(ctx.task.proposals.values_list('pk', flat=True)), self.user)
        out = self.propose(self.ctx(), ok=False)
        self.assertIn('уже є', out)
        out = self.propose(self.ctx(), ok=False, tracking_number='')    # той самий номер підтвердження
        self.assertIn('PO-', out)
        found = json.loads(self.call(self.ctx(), 'find_purchase_orders', query=TN))
        self.assertEqual(found['purchase_orders'][0]['supplier'], 'Raynool')
        self.assertEqual(found['parcel_with_this_tracking']['direction'], '📥 До нас')
        old = PurchaseOrder.objects.create(supplier=Supplier.objects.get(), status='ordered')
        IncomingShipment.objects.create(purchase_order=old, tracking_number='JD0001234567')
        out = self.call(self.ctx(), 'propose_incoming_parcel', ok=False, purchase_order=old.code,
                        tracking_number='JD0001234567', evidence='x', source='y')
        self.assertIn('Трекінгу приходу', out)

    def test_find_by_ordered_skus(self):
        """«Що замовляли» (з назви Gerber-архіву) — агент знаходить закупівлю за цим артикулом."""
        po = PurchaseOrder.objects.create(supplier=Supplier.objects.create(name='HongRong'), status='received',
                                          ordered_skus='TR240401-01, TR240402-01')
        found = json.loads(self.call(self.ctx(), 'find_purchase_orders', query='TR240402-01'))
        self.assertEqual([r['code'] for r in found['purchase_orders']], [po.code])
        self.assertEqual(found['purchase_orders'][0]['ordered'], 'TR240401-01, TR240402-01')

    def test_unknown_sku_needs_new_product(self):
        out = self.propose(self.ctx(), ok=False, lines=[{'sku': 'CA-NOPE-1', 'qty': 5}], tracking_number='')
        self.assertIn('propose_product', out)
        ctx = self.ctx()
        self.call(ctx, 'propose_product', sku='CA-NOPE-1', name='New cable', evidence='x', source='y')
        self.propose(ctx, lines=[{'sku': 'CA-NOPE-1', 'qty': 5}], tracking_number='', supplier_order_no='')
        decide.apply_selected(ctx.task, list(ctx.task.proposals.values_list('pk', flat=True)), self.user)
        line = PurchaseOrder.objects.get().lines.get()
        self.assertEqual(line.product.sku, 'CA-NOPE-1')                  # новий товар застосовано першим

    def test_parcel_for_existing_order(self):
        po = PurchaseOrder.objects.create(supplier=Supplier.objects.create(name='Raynool'), status='ordered')
        ctx = self.ctx()
        self.call(ctx, 'propose_incoming_parcel', purchase_order=po.code, tracking_number=TN, evidence='UPS', source='л')
        p = ctx.task.proposals.get()
        self.assertEqual(decide.describe(p)[0], '📥 Посилка до нас')
        decide.apply_selected(ctx.task, [p.pk], self.user)
        s = Shipment.objects.get()
        self.assertEqual((s.direction, s.purchase_order_id), (Shipment.IN, po.pk))
        po.refresh_from_db()
        self.assertEqual(po.tracking_number, TN)

    def test_rights_on_apply(self):
        ctx = self.ctx()
        self.propose(ctx)
        clerk = User.objects.create_user('clerk', 'c@x.y', 'p', is_staff=True)
        out = decide.apply_selected(ctx.task, list(ctx.task.proposals.values_list('pk', flat=True)), clerk)
        self.assertIn('немає права створювати закупівлі', out['failed'][0])
        self.assertEqual(PurchaseOrder.objects.count(), 0)


class MailReadingTests(Base):
    def setUp(self):
        super().setUp()
        from email_assistant.models import EmailAccount, EmailMessage
        self.account = EmailAccount.objects.create(user=self.user, email_address='slava@sevskiy.de')
        body = ('UPS Tracking-No.: 1Z2R5D130407163813\n\nMit freundlichen Grüßen\nDr. Sergey Sevskiy\n\n'
                'Von: Oscar Yang <oscar@raynool.com>\nGesendet: Dienstag, 3. November 2026 03:53\n'
                'An: Sergey <sergey@sevskiy.de>\nBetreff: RFQ for MHF1/MHF4 cable assemblies\n\n'
                'Dear Sergey,\nthe goods were shipped today, invoice attached.\nBest regards\nOscar')
        self.msg = EmailMessage.objects.create(
            account=self.account, folder='inbox', imap_folder_name='INBOX', imap_uid=7,
            subject='WG: RFQ for MHF1/MHF4 cable assemblies', from_email='sergey@sevskiy.de',
            from_name='Dr. Sergey Sevskiy', to_emails=['slava@sevskiy.de'], body_text=body, sent_at=timezone.now(),
            attachments=[{'name': 'Invoice.pdf', 'size': 408000, 'content_type': 'application/pdf'}])
        self.ctx = tools.Context(self.task('Занеси закупівлю', products=[], message=self.msg))

    def test_forwarded_part_is_read(self):
        out, err = tools.run(self.ctx, 'read_mail', {})
        self.assertFalse(err, out)
        self.assertIn('Переслано — оригінал від', out)
        self.assertIn('oscar@raynool.com', out)

    def test_attachment_fetched_from_mail_server(self):
        rel = os.path.join('email_attachments', str(self.account.pk), '7_Invoice.pdf')

        def fetch(account, msg, index):
            os.makedirs(os.path.join(self.media, os.path.dirname(rel)), exist_ok=True)
            with open(os.path.join(self.media, rel), 'wb') as fh:
                fh.write(pdf_bytes('Commercial Invoice 1750', 'CA-MHF4-MC1.13-075-MHF4  300 pcs  USD 0.85'))
            atts = list(msg.attachments)
            atts[index] = {**atts[index], 'file_path': rel}
            msg.attachments = atts
            msg.save(update_fields=['attachments'])
            return b'%PDF'

        with mock.patch('email_assistant.attachments.attachment_bytes', side_effect=fetch) as got:
            out, err = tools.run(self.ctx, 'read_attachment', {'index': 0})
        self.assertFalse(err, out)
        got.assert_called_once()
        self.assertIn('Commercial Invoice 1750', out)
        with mock.patch('email_assistant.attachments.attachment_bytes', side_effect=OSError('no imap')):
            out, err = tools.run(self.ctx, 'read_attachment', {'index': 5})
        self.assertTrue(err)
        self.assertIn('немає вкладення', out)
