"""
python manage.py create_sample_templates [--force]

Creates 3 sample Word templates using paragraph-level {% for %} loops
(compatible with all docxtpl versions, no {%tr %} table-row tags).
"""
from django.core.management.base import BaseCommand
from io import BytesIO


class Command(BaseCommand):
    help = 'Create 3 sample Word document templates'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true',
                            help='Delete existing samples and recreate')

    def handle(self, *args, **options):
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from django.core.files.base import ContentFile
        from documents.models import DocumentTemplate

        A = WD_ALIGN_PARAGRAPH
        force = options['force']

        def save(doc, name, doc_type):
            if DocumentTemplate.objects.filter(name=name).exists():
                if force:
                    DocumentTemplate.objects.filter(name=name).delete()
                    self.stdout.write(f'  Deleted old: {name}')
                else:
                    self.stdout.write(f'  Skip (exists): {name}')
                    return
            buf = BytesIO()
            doc.save(buf)
            buf.seek(0)
            tpl = DocumentTemplate(
                name=name, doc_type=doc_type,
                module='sales', language='en',
                is_active=True, is_default=True)
            tpl.template_file.save(
                f'{doc_type}_sample_en.docx',
                ContentFile(buf.getvalue()), save=True)
            self.stdout.write(f'  OK: {name}')

        def add_kv(doc, label, var):
            p = doc.add_paragraph()
            p.add_run(f'{label} ').bold = True
            p.add_run(var)

        # ── Packing List ──────────────────────────────────────────────────────
        doc = Document()
        h = doc.add_heading('PACKING LIST', 0)
        h.alignment = A.CENTER
        doc.add_paragraph('')

        add_kv(doc, 'Order No:', '{{order_number}}')
        add_kv(doc, 'Date:', '{{order_date}}')
        add_kv(doc, 'Tracking:', '{{tracking_number}}')
        add_kv(doc, 'Carrier:', '{{carrier_name}}')

        doc.add_paragraph('')
        tbl = doc.add_table(rows=1, cols=2)
        tbl.style = 'Table Grid'
        tbl.rows[0].cells[0].text = (
            'FROM:\n{{shipper_name}}\n{{shipper_address}}\n'
            '{{shipper_city}}, {{shipper_country}}\n{{shipper_email}}')
        tbl.rows[0].cells[1].text = (
            'TO:\n{{customer_name}}\n{{customer_address}}\n'
            '{{customer_city}}, {{customer_country}}\n{{customer_email}}')

        doc.add_paragraph('')
        doc.add_heading('Items', level=2)

        # Header row
        tbl2 = doc.add_table(rows=1, cols=5)
        tbl2.style = 'Table Grid'
        for i, h in enumerate(['SKU', 'Description', 'Qty', 'Unit Price', 'Total']):
            c = tbl2.rows[0].cells[i]
            c.text = h
            c.paragraphs[0].runs[0].bold = True

        # Items via paragraph loop (below the header table)
        doc.add_paragraph(
            '{% for item in items %}'
            '{{item.sku}}  |  {{item.name}}  |  {{item.quantity}}'
            '  |  {{item.unit_price}} {{currency}}'
            '  |  {{item.total_price}} {{currency}}'
            '{% endfor %}'
        )

        doc.add_paragraph('')
        add_kv(doc, 'Total items:', '{{total_items}} pcs')
        add_kv(doc, 'Total weight:', '{{total_weight}} kg')
        add_kv(doc, 'Total amount:', '{{total_amount}} {{currency}}')
        doc.add_paragraph('')
        doc.add_paragraph('{{notes}}')
        doc.add_paragraph('Generated: {{generated_date}}')
        save(doc, 'Packing List EN', 'packing_list')

        # ── Proforma Invoice ──────────────────────────────────────────────────
        doc = Document()
        h = doc.add_heading('PROFORMA INVOICE', 0)
        h.alignment = A.CENTER
        doc.add_paragraph('')

        add_kv(doc, 'Invoice No:', '{{invoice_number}}')
        add_kv(doc, 'Date:', '{{invoice_date}}')
        add_kv(doc, 'Due Date:', '{{due_date}}')

        doc.add_paragraph('')
        tbl = doc.add_table(rows=1, cols=2)
        tbl.style = 'Table Grid'
        tbl.rows[0].cells[0].text = (
            'SELLER:\n{{shipper_name}}\n{{shipper_address}}\n'
            'VAT: {{vat_number}}\nEORI: {{eori_number}}\n'
            'IBAN: {{bank_iban}}\nSWIFT: {{bank_swift}}')
        tbl.rows[0].cells[1].text = (
            'BUYER:\n{{customer_name}}\n{{customer_address}}\n'
            '{{customer_city}}, {{customer_country}}\n'
            'Email: {{customer_email}}\nVAT: {{customer_vat}}')

        doc.add_paragraph('')
        doc.add_heading('Items', level=2)

        tbl2 = doc.add_table(rows=1, cols=4)
        tbl2.style = 'Table Grid'
        for i, h in enumerate(['SKU', 'Description', 'Qty', 'Amount']):
            c = tbl2.rows[0].cells[i]
            c.text = h
            c.paragraphs[0].runs[0].bold = True

        doc.add_paragraph(
            '{% for item in items %}'
            '{{item.sku}}  |  {{item.name}}  |  {{item.quantity}}'
            '  |  {{item.total_price}} {{currency}}'
            '{% endfor %}'
        )

        doc.add_paragraph('')
        tbl3 = doc.add_table(rows=3, cols=2)
        tbl3.style = 'Table Grid'
        for i, (label, val) in enumerate([
            ('Subtotal:', '{{subtotal}} {{currency}}'),
            ('VAT ({{vat_rate}}%):', '{{vat_amount}} {{currency}}'),
            ('TOTAL:', '{{total_amount}} {{currency}}'),
        ]):
            tbl3.rows[i].cells[0].text = label
            tbl3.rows[i].cells[0].paragraphs[0].runs[0].bold = True
            tbl3.rows[i].cells[1].text = val

        doc.add_paragraph('')
        add_kv(doc, 'Payment Terms:', '{{payment_terms}}')
        doc.add_paragraph('{{proforma_notes}}')
        doc.add_paragraph('Generated: {{generated_date}}')
        save(doc, 'Proforma Invoice EN', 'proforma')

        # ── CN23 ──────────────────────────────────────────────────────────────
        doc = Document()
        h = doc.add_heading('CN23 CUSTOMS DECLARATION', 0)
        h.alignment = A.CENTER
        doc.add_paragraph('(For commercial goods sent by post)')
        doc.add_paragraph('')

        tbl = doc.add_table(rows=1, cols=2)
        tbl.style = 'Table Grid'
        tbl.rows[0].cells[0].text = (
            'SENDER:\n{{shipper_name}}\n{{shipper_address}}\n'
            '{{shipper_city}}, {{shipper_country}}\n{{shipper_email}}')
        tbl.rows[0].cells[1].text = (
            'ADDRESSEE:\n{{customer_name}}\n{{customer_address}}\n'
            '{{customer_city}}, {{customer_country}}\n{{customer_email}}')

        doc.add_paragraph('')
        add_kv(doc, 'Type of contents:', '{{customs_type}} - {{customs_reason}}')
        add_kv(doc, 'Country of origin:', '{{country_of_origin}}')

        doc.add_paragraph('')
        doc.add_heading('Contents', level=2)

        tbl2 = doc.add_table(rows=1, cols=4)
        tbl2.style = 'Table Grid'
        for i, h in enumerate(['HS Code', 'Description', 'Qty', 'Value']):
            c = tbl2.rows[0].cells[i]
            c.text = h
            c.paragraphs[0].runs[0].bold = True

        doc.add_paragraph(
            '{% for item in items %}'
            '{{item.hs_code}}  |  {{item.name}}  |  {{item.quantity}}'
            '  |  {{item.total_price}} {{currency}}  |  {{item.weight}} kg'
            '{% endfor %}'
        )

        doc.add_paragraph('')
        tbl3 = doc.add_table(rows=2, cols=2)
        tbl3.style = 'Table Grid'
        tbl3.rows[0].cells[0].text = 'Total declared value:'
        tbl3.rows[0].cells[0].paragraphs[0].runs[0].bold = True
        tbl3.rows[0].cells[1].text = '{{declared_value}} {{currency}}'
        tbl3.rows[1].cells[0].text = 'Gross weight:'
        tbl3.rows[1].cells[0].paragraphs[0].runs[0].bold = True
        tbl3.rows[1].cells[1].text = '{{gross_weight}} kg'

        doc.add_paragraph('')
        doc.add_paragraph(
            'I certify that the particulars given in this declaration '
            'are correct and that this item does not contain any dangerous '
            'article or articles prohibited by legislation or by postal or '
            'customs regulations.')
        doc.add_paragraph('')
        add_kv(doc, 'Signature:', '_________________________')
        add_kv(doc, 'Date:', '{{invoice_date}}')
        doc.add_paragraph('Generated by Minerva BI: {{generated_date}}')
        save(doc, 'CN23 Customs Declaration EN', 'cn23')

        self.stdout.write(self.style.SUCCESS('Done. Templates ready!'))
        self.stdout.write('  See: /admin/documents/documenttemplate/')
