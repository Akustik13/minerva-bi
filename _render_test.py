"""Render fixed_Invic (9).docx with sample context and report result."""
import sys, os, io
sys.path.insert(0, r'C:\tabele_mvp')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tabele.settings')

from docxtpl import DocxTemplate
import jinja2

path = r'C:\tabele_mvp\data\fixed_Invic (9).docx'

ctx = {
    'order_number': 'TEST-001', 'order_date': '21.05.2026',
    'order_status': 'received', 'invoice_number': 'INV-TEST-001',
    'invoice_date': '21.05.2026', 'due_date': '20.06.2026',
    'customer_name': 'Test GmbH', 'customer_address': 'Teststrasse 1',
    'customer_city': 'Berlin, 10115', 'customer_country': 'Deutschland',
    'customer_email': 'test@example.com', 'customer_phone': '+49 30 1234567',
    'customer_vat': 'DE123456789',
    'shipper_name': 'Our Co. GmbH', 'shipper_address': 'Main St. 5',
    'shipper_city': '20095 Hamburg', 'shipper_country': 'Deutschland',
    'shipper_email': 'info@co.de', 'shipper_phone': '+49 40 9999999',
    'vat_number': 'DE987654321', 'eori_number': 'DE1234567890123',
    'bank_name': 'Deutsche Bank', 'bank_iban': 'DE89370400440532013000',
    'bank_swift': 'DEUTDEDB',
    'tracking_number': '1Z999AA1', 'carrier_name': 'DHL',
    'shipping_date': '21.05.2026', 'currency': 'EUR',
    'subtotal': '200.00', 'vat_rate': '0',
    'vat_amount': '0.00', 'total_amount': '200.00',
    'payment_terms': 'Prepaid',
    'total_weight': '1.200', 'total_items': '3', 'items_count': '2',
    'customs_type': 'SALE', 'customs_reason': 'Commercial goods',
    'country_of_origin': 'DE', 'declared_value': '200.00', 'gross_weight': '1.200',
    'items': [
        {'sku': 'SKU-001', 'name': 'Resistor 10K', 'quantity': 10,
         'unit_price': '5.00', 'total_price': '50.00',
         'weight': '0.010', 'hs_code': '8533.10', 'country': 'DE'},
        {'sku': 'SKU-002', 'name': 'Capacitor 100uF', 'quantity': 5,
         'unit_price': '30.00', 'total_price': '150.00',
         'weight': '0.050', 'hs_code': '8532.22', 'country': 'DE'},
    ],
    'generated_date': '21.05.2026 10:00', 'generated_by': 'Minerva BI',
    'notes': 'Test note', 'proforma_notes': '',
}

print('Rendering:', path)
print()

try:
    tpl = DocxTemplate(path)
    tpl.render(ctx)
    buf = io.BytesIO()
    tpl.save(buf)
    size = buf.tell()
    print('OK: rendered successfully, output size =', size, 'bytes')

    out_path = r'C:\tabele_mvp\data\_rendered_test.docx'
    with open(out_path, 'wb') as f:
        buf.seek(0)
        f.write(buf.read())
    print('Saved to:', out_path)

    import zipfile, re
    buf.seek(0)
    with zipfile.ZipFile(buf) as z:
        xml = z.read('word/document.xml').decode('utf-8')
    all_text = ' '.join(re.findall(r'<w:t[^>]*>([^<]+)</w:t>', xml))

    print()
    print('=== Content check ===')
    checks = [
        ('TEST-001',       'order_number'),
        ('Resistor 10K',   'item.name row 1'),
        ('Capacitor 100uF','item.name row 2'),
        ('8533.10',        'item.hs_code row 1'),
        ('8532.22',        'item.hs_code row 2'),
        ('5.00',           'item.unit_price row 1'),
        ('30.00',          'item.unit_price row 2'),
        ('200.00',         'total_amount'),
        ('DHL',            'carrier_name'),
    ]
    for val, desc in checks:
        print('  ' + ('OK' if val in all_text else 'MISSING') + ': ' + desc + ' = ' + repr(val))

    leftover = re.findall(r'\{[{%][^}%]+[}%]\}', all_text)
    print()
    if leftover:
        print('WARNING - leftover template tags:', leftover[:5])
    else:
        print('OK: no leftover template syntax in output')

except jinja2.exceptions.TemplateSyntaxError as e:
    print('FAIL TemplateSyntaxError:', e)
except Exception as e:
    import traceback
    traceback.print_exc()
    print('FAIL', type(e).__name__, ':', e)
