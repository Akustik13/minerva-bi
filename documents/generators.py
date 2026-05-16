"""
documents/generators.py
Збирає дані з Minerva для заповнення шаблонів.
Використовує реальні назви полів моделей Minerva.
"""
from decimal import Decimal
from django.utils import timezone
import logging

logger = logging.getLogger('documents')


def get_order_context(order_pk: int) -> dict:
    """Повний контекст для документів на основі SalesOrder."""
    from sales.models import SalesOrder

    # Системні налаштування
    try:
        from config.models import SystemSettings, DocumentSettings
        sys_s = SystemSettings.get()
        doc_s = DocumentSettings.get()
    except Exception:
        sys_s = None
        doc_s = None

    # Дані нашої компанії — з AccountingSettings
    try:
        from accounting.models import CompanySettings
        cs = CompanySettings.get()
    except Exception:
        cs = None

    order = (SalesOrder.objects
             .prefetch_related('lines__product')
             .get(pk=order_pk))

    now = timezone.now()

    # ── Клієнт / одержувач ──────────────────────────────────────────────────
    # SalesOrder зберігає адресу безпосередньо, без FK на Customer
    customer_name    = order.ship_name or order.client or ''
    customer_address = order.addr_street or ''
    city_parts = [p for p in [order.addr_city, order.addr_zip] if p]
    customer_city    = ', '.join(city_parts) if city_parts else ''
    customer_country = order.addr_country or ''
    customer_email   = order.ship_email or order.email or ''
    customer_phone   = order.ship_phone or order.phone or ''

    # VAT — шукаємо в Customer по customer_key якщо є
    customer_vat = ''
    if order.customer_key:
        try:
            from crm.models import Customer
            cust = Customer.objects.filter(external_key=order.customer_key).first()
            if cust:
                customer_name    = customer_name or cust.company or cust.name or ''
                customer_email   = customer_email or cust.email or ''
                customer_phone   = customer_phone or cust.phone or ''
                customer_country = customer_country or cust.country or ''
        except Exception:
            pass

    # ── Відправник — CompanySettings ────────────────────────────────────────
    if cs:
        shipper_name    = cs.name or ''
        shipper_address = cs.addr_street or ''
        city_parts_s    = [p for p in [cs.addr_zip, cs.addr_city] if p]
        shipper_city    = ' '.join(city_parts_s)
        shipper_country = cs.addr_country or 'DE'
        shipper_email   = cs.email or ''
        shipper_phone   = cs.phone or ''
        vat_number      = cs.vat_id or ''
        bank_name       = cs.bank_name or ''
        bank_iban       = cs.iban or ''
        bank_swift      = cs.swift or ''
    else:
        shipper_name = shipper_address = shipper_city = ''
        shipper_country = 'DE'
        shipper_email = shipper_phone = vat_number = ''
        bank_name = bank_iban = bank_swift = ''

    eori_number = getattr(sys_s, 'eori_number', '') or '' if sys_s else ''

    # ── Дати замовлення ──────────────────────────────────────────────────────
    order_number = order.order_number or str(order.pk)
    order_date_raw = order.order_date
    if hasattr(order_date_raw, 'strftime'):
        order_date = order_date_raw.strftime('%d.%m.%Y')
    else:
        order_date = str(order_date_raw) if order_date_raw else now.strftime('%d.%m.%Y')

    # ── Рядки замовлення ─────────────────────────────────────────────────────
    items        = []
    subtotal     = Decimal('0')
    total_weight = Decimal('0')
    total_qty    = 0

    for line in order.lines.select_related('product').all():
        product    = line.product
        qty        = int(line.qty or 0)
        unit_price = Decimal(str(line.unit_price or 0))
        total_line = Decimal(str(line.total_price or unit_price * qty))

        weight = Decimal('0')
        if product:
            w = getattr(product, 'net_weight_g', None)
            if w:
                weight = Decimal(str(w)) * qty / 1000  # г → кг

        subtotal     += total_line
        total_weight += weight
        total_qty    += qty

        sku  = line.sku_raw or (product.sku if product else '') or ''
        name = str(product) if product else (line.sku_raw or '')
        hs   = (getattr(product, 'hs_code', '') or '') if product else ''
        coo  = (getattr(product, 'country_of_origin', '') or '') if product else ''

        items.append({
            'sku':         sku,
            'name':        name[:80],
            'quantity':    qty,
            'unit_price':  f'{unit_price:.2f}',
            'total_price': f'{total_line:.2f}',
            'weight':      f'{weight:.3f}' if weight else '',
            'hs_code':     hs,
            'country':     coo or shipper_country,
        })

    # ── Валюта і ПДВ ─────────────────────────────────────────────────────────
    currency = order.currency or (sys_s.default_currency if sys_s else 'EUR') or 'EUR'
    vat_rate   = Decimal(str(
        getattr(doc_s,   'default_vat_rate', 0) or
        getattr(sys_s,   'default_vat_rate', 0) or 0
    ))
    vat_amount = (subtotal * vat_rate / 100).quantize(Decimal('0.01'))
    total      = subtotal + vat_amount

    # ── Доставка ─────────────────────────────────────────────────────────────
    tracking_number = order.tracking_number or ''
    carrier_name    = order.shipping_courier or ''
    shipping_date   = ''
    try:
        from shipping.models import Shipment
        shipment = (Shipment.objects
                    .filter(order=order)
                    .order_by('-created_at')
                    .first())
        if shipment:
            tracking_number = tracking_number or shipment.tracking_number or ''
            if shipment.carrier:
                carrier_name = str(shipment.carrier)
            if shipment.created_at:
                shipping_date = shipment.created_at.strftime('%d.%m.%Y')
    except Exception:
        pass

    invoice_number = f'INV-{order_number}'
    payment_terms  = (getattr(doc_s, 'proforma_payment_terms', '') or '') if doc_s else ''
    payment_terms  = payment_terms or 'Payment within 30 days'
    notes          = (getattr(doc_s, 'packing_list_footer_note', '') or '') if doc_s else ''
    proforma_notes = (getattr(doc_s, 'proforma_notes', '') or '') if doc_s else ''
    customs_type   = (getattr(doc_s, 'customs_default_type', 'SALE') or 'SALE') if doc_s else 'SALE'
    customs_reason = (getattr(doc_s, 'customs_reason', 'Commercial goods') or 'Commercial goods') if doc_s else 'Commercial goods'

    return {
        # Замовлення
        'order_number':     order_number,
        'order_date':       order_date,
        'order_status':     order.status or '',
        'invoice_number':   invoice_number,
        'invoice_date':     now.strftime('%d.%m.%Y'),
        'due_date':         (now + timezone.timedelta(days=30)).strftime('%d.%m.%Y'),
        # Клієнт
        'customer_name':    customer_name,
        'customer_address': customer_address,
        'customer_city':    customer_city,
        'customer_country': customer_country,
        'customer_email':   customer_email,
        'customer_phone':   customer_phone,
        'customer_vat':     customer_vat,
        # Відправник
        'shipper_name':     shipper_name,
        'shipper_address':  shipper_address,
        'shipper_city':     shipper_city,
        'shipper_country':  shipper_country,
        'shipper_email':    shipper_email,
        'shipper_phone':    shipper_phone,
        'eori_number':      eori_number,
        'vat_number':       vat_number,
        'bank_name':        bank_name,
        'bank_iban':        bank_iban,
        'bank_swift':       bank_swift,
        # Доставка
        'tracking_number':  tracking_number,
        'carrier_name':     carrier_name,
        'shipping_date':    shipping_date,
        # Фінанси
        'currency':         currency,
        'subtotal':         f'{subtotal:.2f}',
        'vat_rate':         f'{vat_rate:.0f}',
        'vat_amount':       f'{vat_amount:.2f}',
        'total_amount':     f'{total:.2f}',
        'payment_terms':    payment_terms,
        # Параметри
        'total_weight':     f'{total_weight:.3f}',
        'total_items':      str(total_qty),
        'items_count':      str(len(items)),
        # Митна
        'customs_type':     customs_type,
        'customs_reason':   customs_reason,
        'country_of_origin': shipper_country,
        'declared_value':   f'{subtotal:.2f}',
        'gross_weight':     f'{total_weight:.3f}',
        # Товари
        'items':            items,
        # Мета
        'generated_date':   now.strftime('%d.%m.%Y %H:%M'),
        'generated_by':     'Minerva BI',
        'notes':            notes,
        'proforma_notes':   proforma_notes,
    }
