"""🔎 Що замовляли (PurchaseOrder.ordered_skus) — артикули з документів закупівлі: назва Gerber-архіву, файли в
ZIP, тема листа, текст рахунку (inventory/services/po_documents.py, PurchaseOrderAdmin).

python manage.py test inventory.tests_po_ordered --settings=tabele.settings_test
"""
import io
import zipfile

from django.urls import reverse

from inventory.models import Product, ProductAlias, PurchaseOrder, PurchaseOrderDocument as D
from inventory.services import po_documents
from inventory.tests_po_documents import PDF, Base


def zip_bytes(*names) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name in names:
            z.writestr(name, 'G04 gerber*')
    return buf.getvalue()


def codes(found):
    return [c for c, _w in found]


class DetectTests(Base):
    def setUp(self):
        super().setUp()
        self.po.supplier_order_no = 'HR241105-01'
        self.po.save()

    def test_archive_name_without_catalog(self):
        """Gerber-архів, названий артикулом, — артикул, навіть якщо в каталозі його ще немає."""
        po_documents.add(self.po, 'Gerber_TR240401-01.zip', zip_bytes('Gerber_TopLayer.GTL'), D.ORDER)
        self.po.refresh_from_db()
        self.assertEqual(self.po.ordered_skus, 'TR240401-01')
        po_documents.add(self.po, 'Gerber-TR240402-02.rar', b'Rar!\x1a\x07\x00 data', D.ORDER)
        self.po.refresh_from_db()
        self.assertEqual(self.po.ordered_skus, 'TR240401-01, TR240402-02')

    def test_names_inside_zip(self):
        data = zip_bytes('TR240403-01/TR240403-01.GTL', 'TR240403-01/drill.DRL', '__MACOSX/TR999999-01.GTL')
        po_documents.add(self.po, 'pcb.zip', data, D.ORDER)
        self.po.refresh_from_db()
        self.assertEqual(self.po.ordered_skus, 'TR240403-01')

    def test_document_numbers_are_not_articles(self):
        for name in ('PI-HR241105-01.pdf', 'INV240401-01.pdf', 'IMG20241105-01.jpg', 'HR241105-01.pdf',
                     'Invoice 2024-11-05.pdf'):
            po_documents.add(self.po, name, PDF + name.encode(), D.INVOICE)
        self.po.refresh_from_db()
        self.assertEqual(self.po.ordered_skus, '')

    def test_catalog_articles_from_invoice_text(self):
        """У тексті рахунку — лише артикули каталогу (і аліаси), а не все, що схоже на артикул."""
        Product.objects.create(sku='AN220213-01C', name='Antenna')
        cable = Product.objects.create(sku='CA-MHF4-MC1.13-075-MHF4', name='Cable')
        ProductAlias.objects.create(alias='3228-CAMHF4075-ND', product=cable)
        text = ('Proforma Invoice HR241105-01\nItem: AN220213-01C  qty 100\nDigiKey 3228-CAMHF4075-ND 50 pcs\n'
                'TR999999-01 sample, total 2024.00 USD').encode()
        po_documents.add(self.po, 'Proforma.txt', text, D.INVOICE)
        self.po.refresh_from_db()
        self.assertEqual(po_documents.ordered_codes(self.po.ordered_skus), ['AN220213-01C', 'CA-MHF4-MC1.13-075-MHF4'])

    def test_catalog_cut_only_before_letters(self):
        Product.objects.create(sku='TR240401-01', name='PCB')
        Product.objects.create(sku='AN220213', name='Antenna base')
        cat = po_documents.Catalog()
        self.assertEqual(cat.match('tr240401-01-gerber.zip'), 'TR240401-01')
        self.assertEqual(cat.match('TR24040101'), 'TR240401-01')
        self.assertIsNone(cat.match('AN220213-01C'))          # інший артикул, а не «AN220213»
        self.assertIsNone(cat.match('PCB'))

    def test_pattern_inside_catalog_article_not_twice(self):
        Product.objects.create(sku='AN150202-01C-175-MHF1', name='Antenna cable')
        found = po_documents.detect(self.po, [D(purchase_order=self.po, title='AN150202-01C-175-MHF1.zip')])
        self.assertEqual(codes(found), ['AN150202-01C-175-MHF1'])

    def test_mail_subject(self):
        msg = self.mail(subject='RE: PCB order TR240405-01 shipped', files=(('Invoice.pdf', PDF),))
        po_documents.add_from_mail(self.po, self.user, {f'{msg.pk}:0': D.INVOICE})
        self.po.refresh_from_db()
        self.assertEqual(self.po.ordered_skus, 'TR240405-01')

    def test_merge_keeps_manual_value(self):
        self.po.ordered_skus = 'tr240401-01, Steckerplatine'
        self.po.save()
        po_documents.add(self.po, 'TR240401-01.zip', zip_bytes('a.GTL'), D.ORDER)
        po_documents.add(self.po, 'TR240406-01.zip', zip_bytes('b.GTL'), D.ORDER)
        self.po.refresh_from_db()
        self.assertEqual(self.po.ordered_skus, 'tr240401-01, Steckerplatine, TR240406-01')
        self.assertLessEqual(len(po_documents.merge('X' * 495, ['TR240407-01'])), po_documents.ORDERED_MAX)

    def test_detect_skips_po_numbers(self):
        self.po.tracking_number = ''
        self.po.save()
        found = po_documents.detect(self.po, [D(purchase_order=self.po, title=f'{self.po.code}.pdf'),
                                              D(purchase_order=self.po, title='Gerber_HR241105-01.zip')])
        self.assertEqual(found, [])


class AdminTests(Base):
    def test_list_column_search_and_action(self):
        pcb = Product.objects.create(sku='TR240401-01', name='PCB')
        po_documents.add(self.po, 'TR240401-01.zip', zip_bytes('a.GTL'), D.ORDER)
        other = PurchaseOrder.objects.create(supplier=self.supplier, status='received')
        D.objects.create(purchase_order=other, category=D.ORDER, title='Gerber_TR240402-01.zip')
        url = reverse('admin:inventory_purchaseorder_changelist')
        page = self.client.get(url)
        self.assertContains(page, 'Що замовляли')
        self.assertContains(page, reverse('admin:inventory_product_change', args=[pcb.pk]))
        self.assertEqual(list(self.client.get(url, {'q': 'TR240401-01'}).context['cl'].result_list), [self.po])
        self.assertEqual(list(self.client.get(url, {'q': 'TR240402'}).context['cl'].result_list), [])
        self.client.post(url, {'action': 'detect_ordered', '_selected_action': [other.pk]})
        other.refresh_from_db()
        self.assertEqual(other.ordered_skus, 'TR240402-01')
        self.assertEqual(list(self.client.get(url, {'q': 'TR240402'}).context['cl'].result_list), [other])

    def test_detect_button_post_only(self):
        D.objects.create(purchase_order=self.po, category=D.ORDER, title='TR240408-01.zip')
        page = self.client.get(reverse('admin:inventory_purchaseorder_change', args=[self.po.pk]))
        detect = reverse('admin:inventory_purchaseorder_ordered_detect', args=[self.po.pk])
        self.assertContains(page, detect)
        self.client.get(detect)
        self.po.refresh_from_db()
        self.assertEqual(self.po.ordered_skus, '')
        self.client.post(detect)
        self.po.refresh_from_db()
        self.assertEqual(self.po.ordered_skus, 'TR240408-01')

    def test_inline_upload_fills_field(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        url = reverse('admin:inventory_purchaseorder_change', args=[self.po.pk])
        page = self.client.get(url)
        data = {}
        for name, field in page.context['adminform'].form.fields.items():
            value = page.context['adminform'].form.initial.get(name, field.initial)
            if value is not None:
                data[name] = value.isoformat() if hasattr(value, 'isoformat') else value
        data['supplier'] = self.supplier.pk
        for inline in page.context['inline_admin_formsets']:
            fs = inline.formset
            for key, value in fs.management_form.initial.items():
                data[f'{fs.prefix}-{key}'] = value
            if fs.model is D:
                docs_prefix = fs.prefix
        data[f'{docs_prefix}-TOTAL_FORMS'] = 1
        data[f'{docs_prefix}-0-category'] = D.ORDER
        data[f'{docs_prefix}-0-file'] = SimpleUploadedFile('Gerber_TR240409-01.zip', zip_bytes('x.GTL'))
        data = {k: v for k, v in data.items() if v != '' or k.endswith('FORMS')}
        resp = self.client.post(url, data)
        self.assertEqual(resp.status_code, 302, getattr(resp, 'context', {}) and resp.context.get('errors'))
        self.po.refresh_from_db()
        self.assertEqual(self.po.ordered_skus, 'TR240409-01')
