"""📎 Документи закупівлі за категоріями (inventory/services/po_documents.py, PurchaseOrderAdmin, агент).

python manage.py test inventory.tests_po_documents --settings=tabele.settings_test
"""
import os
import shutil
import tempfile

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from inventory.models import Product, PurchaseOrder, PurchaseOrderDocument as D, Supplier
from inventory.services import po_documents

PDF = b'%PDF-1.4 invoice 1750201'


class Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        over = override_settings(MEDIA_ROOT=self.media)
        over.enable()
        self.addCleanup(over.disable)
        self.user = User.objects.create_superuser('slava', 's@x.y', 'p')
        self.client.force_login(self.user)
        self.supplier = Supplier.objects.create(name='Raynool Technology Co.,Ltd', email='oscar@raynool.com')
        self.po = PurchaseOrder.objects.create(supplier=self.supplier, status='ordered', tracking_number='1Z2R5D130407163813')
        from email_assistant.models import EmailAccount
        self.account = EmailAccount.objects.create(user=self.user, email_address='slava@sevskiy.de')
        self.uid = 0

    def mail(self, frm='oscar@raynool.com', subject='Order have been sent', files=(('Invoice.pdf', PDF),),
             account=None, body=''):
        from email_assistant.models import EmailMessage
        account = account or self.account
        self.uid += 1
        atts = []
        for i, (name, data) in enumerate(files):
            rel = f'email_attachments/{account.pk}/{self.uid}/{i}_{name}'
            path = os.path.join(self.media, *rel.split('/'))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as fh:
                fh.write(data)
            atts.append({'name': name, 'file_path': rel, 'size': len(data), 'content_type': 'application/pdf'})
        return EmailMessage.objects.create(account=account, folder='inbox', imap_folder_name='INBOX', imap_uid=self.uid,
                                           subject=subject, from_email=frm, body_text=body, attachments=atts,
                                           to_emails=['slava@sevskiy.de'], sent_at=timezone.now())


class CategoryAndStorageTests(Base):
    def test_guess_category(self):
        for name, cat in (('Invoice.pdf', D.INVOICE), ('Proforma Invoice 12.pdf', D.INVOICE),
                          ('Zahlungsbestätigung.pdf', D.PAYMENT), ('Payment receipt.pdf', D.PAYMENT),
                          ('Lieferschein 4711.pdf', D.SHIPPING), ('Packing_List.xlsx', D.SHIPPING),
                          ('Order Confirmation AB-123.pdf', D.CONFIRMATION), ('Quotation_1750.pdf', D.QUOTE),
                          ('Gutschrift 7.pdf', D.CLAIM), ('ABD 25DE.pdf', D.CUSTOMS), ('CoC MHF4.pdf', D.QUALITY),
                          ('Purchase Order PO-12.pdf', D.ORDER), ('scan001.pdf', D.OTHER)):
            self.assertEqual(po_documents.guess_category(name), cat, name)
        self.assertEqual(po_documents.guess_category('drawing.pdf', 'WG: RFQ for MHF1 cable assemblies'), D.RFQ)

    def test_add_stores_by_category_without_duplicates(self):
        doc, created = po_documents.add(self.po, 'Invoice.pdf', PDF)
        self.assertTrue(created)
        self.assertEqual((doc.category, doc.title, doc.source), (D.INVOICE, 'Invoice.pdf', D.UPLOAD))
        self.assertTrue(doc.file.name.startswith(f'purchase_orders/{self.po.pk}/invoice/'))
        same, created = po_documents.add(self.po, 'Rechnung-copy.pdf', PDF)       # той самий вміст
        self.assertEqual((same, created), (doc, False))
        with self.assertRaises(ValueError):
            po_documents.add(self.po, 'evil.exe', b'MZ')
        path = doc.file.path
        doc.delete()
        self.assertFalse(os.path.exists(path))                                    # файл прибрано разом із записом

    def test_summary_unpaid_until_payment(self):
        po_documents.add(self.po, 'Invoice.pdf', PDF)
        self.assertTrue(po_documents.summary(self.po)['unpaid'])
        po_documents.add(self.po, 'Zahlung.pdf', b'%PDF paid')
        s = po_documents.summary(self.po)
        self.assertFalse(s['unpaid'])
        self.assertEqual([g[0] for g in s['groups']], [D.INVOICE, D.PAYMENT])

    def test_media_needs_login_and_inventory(self):
        doc, _ = po_documents.add(self.po, 'Invoice.pdf', PDF)
        self.assertEqual(self.client.get(doc.file.url).status_code, 200)
        self.client.logout()
        self.assertEqual(self.client.get(doc.file.url).status_code, 302)


class AdminTests(Base):
    def test_change_page_block_and_list(self):
        url = reverse('admin:inventory_purchaseorder_change', args=[self.po.pk])
        page = self.client.get(url)
        self.assertContains(page, 'Документів ще немає')
        self.assertContains(page, reverse('admin:inventory_purchaseorder_mail_docs', args=[self.po.pk]))
        self.assertContains(page, 'enctype="multipart/form-data"')
        doc, _ = po_documents.add(self.po, 'Invoice.pdf', PDF)
        page = self.client.get(url)
        self.assertContains(page, doc.file.url)
        self.assertContains(page, 'Рахунок є — документа про оплату ще немає')
        other = PurchaseOrder.objects.create(supplier=self.supplier, status='ordered')
        changelist = reverse('admin:inventory_purchaseorder_changelist')
        unpaid = self.client.get(changelist, {'docs': 'unpaid'}).context['cl'].result_list
        self.assertEqual(list(unpaid), [self.po])
        none = self.client.get(changelist, {'docs': 'none'}).context['cl'].result_list
        self.assertEqual(list(none), [other])
        self.assertContains(self.client.get(changelist), '🧾')

    def test_inline_form_checks_type(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from inventory.admin import PurchaseOrderDocumentForm
        bad = PurchaseOrderDocumentForm(data={'category': D.INVOICE}, files={
            'file': SimpleUploadedFile('virus.exe', b'MZ')})
        self.assertIn('file', bad.errors)
        good = PurchaseOrderDocumentForm(data={'category': D.INVOICE}, files={
            'file': SimpleUploadedFile('Invoice.pdf', PDF)})
        self.assertTrue(good.is_valid(), good.errors)

    def test_mail_picker_lists_supplier_mail_and_adds(self):
        msg = self.mail(files=(('Invoice.pdf', PDF), ('logo.png', b'\x89PNG small')))
        fwd = self.mail(frm='sergey@sevskiy.de', subject='WG: RFQ', body='Von: oscar@raynool.com',
                        files=(('Lieferschein.pdf', b'%PDF ls'),))
        self.mail(frm='anna@kunde.de', subject='Angebot', files=(('Angebot.pdf', b'%PDF a'),))
        bob = User.objects.create_user('bob', 'b@x.y', 'p', is_staff=True)
        from email_assistant.models import EmailAccount
        foreign = self.mail(account=EmailAccount.objects.create(user=bob, email_address='bob@sevskiy.de'))
        url = reverse('admin:inventory_purchaseorder_mail_docs', args=[self.po.pk])
        rows = self.client.get(url).context['rows']
        self.assertEqual({r['msg'].pk for r in rows}, {msg.pk, fwd.pk})              # чужа скринька й Angebot — ні
        first = next(r for r in rows if r['msg'].pk == msg.pk)
        self.assertEqual([(a['name'], a['category']) for a in first['atts']], [('Invoice.pdf', D.INVOICE)])  # без лого
        r = self.client.post(url, {'pick': [f'{msg.pk}:0', f'{fwd.pk}:0', f'{foreign.pk}:0'],
                                   f'cat_{msg.pk}_0': D.INVOICE, f'cat_{fwd.pk}_0': D.SHIPPING})
        self.assertRedirects(r, reverse('admin:inventory_purchaseorder_change', args=[self.po.pk]),
                             fetch_redirect_response=False)
        docs = {d.title: d for d in self.po.documents.all()}
        self.assertEqual(set(docs), {'Invoice.pdf', 'Lieferschein.pdf'})
        self.assertEqual((docs['Invoice.pdf'].source, docs['Invoice.pdf'].email_message, docs['Invoice.pdf'].source_ref),
                         (D.MAIL, msg, f'msg:{msg.pk}:0'))
        self.assertEqual(docs['Lieferschein.pdf'].category, D.SHIPPING)
        self.client.post(url, {'pick': [f'{msg.pk}:0']})                             # удруге — не дублює
        self.assertEqual(self.po.documents.count(), 2)
        again = self.client.get(url).context['rows']
        self.assertTrue(next(r for r in again if r['msg'].pk == msg.pk)['atts'][0]['added'])


class AgentTests(Base):
    def test_agent_purchase_brings_mail_documents(self):
        from agent import purchases
        from agent.models import AgentProposal, AgentTask
        Product.objects.create(sku='CA-MHF4-MC1.13-075-MHF4', name='Cable')
        msg = self.mail(subject='WG: RFQ for MHF1', files=(('Invoice.pdf', PDF),))
        task = AgentTask.objects.create(owner=self.user, title='внеси заказ', instruction='внеси заказ', message=msg)
        p = AgentProposal.objects.create(task=task, kind=AgentProposal.PURCHASE, field='Raynool', new_value='x',
                                         evidence='x', data={'supplier': {'name': 'Raynool Technology Co.,Ltd',
                                                                          'email': 'oscar@raynool.com'},
                                                             'supplier_order_no': 'INV-9', 'lines': [
                                                                 {'sku': 'CA-MHF4-MC1.13-075-MHF4', 'qty': '300'}]})
        self.assertEqual(purchases.apply_purchase(p, self.user), '')
        po = PurchaseOrder.objects.get(supplier_order_no='INV-9')
        doc = po.documents.get()
        self.assertEqual((doc.category, doc.source, doc.email_message), (D.INVOICE, D.AGENT, msg))
