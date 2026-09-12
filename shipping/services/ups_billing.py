"""
shipping/services/ups_billing.py — UPS Billing Centre integration.

Два режими:
  1. API  — UPSClient.get_billing_invoices() (потрібна підписка Invoice Management)
  2. CSV  — парсинг експорту з billing.ups.com (завжди доступний)

Обидва режими записують результат як accounting.Expense.
"""
import csv
import io
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

logger = logging.getLogger('shipping.ups_billing')

# ── CSV-колонки (UPS використовує різні назви залежно від регіону/звіту) ──────
_COL_INVOICE_NO   = ('invoice number', 'invoice no', 'invoice #', 'invoice_number')
_COL_INVOICE_DATE = ('invoice date', 'invoice_date', 'date')
_COL_TRACKING     = ('lead tracking number', 'tracking number', 'shipment tracking number',
                      'tracking #', 'lead_tracking_number', 'tracking_number')
_COL_SERVICE      = ('service', 'service description', 'service type', 'service_description')
_COL_AMOUNT       = ('net charge', 'total charges', 'charge amount', 'billed amount',
                      'net amount', 'net_charge', 'total_charges')
_COL_CURRENCY     = ('currency', 'currency code', 'currency_code')


def _find_col(headers: list, aliases: tuple):
    """Знаходить першу колонку з заголовку по списку аліасів (case-insensitive)."""
    h_lower = {h.strip().lower(): h for h in headers}
    for alias in aliases:
        if alias in h_lower:
            return h_lower[alias]
    return None


# ── CSV-парсер ────────────────────────────────────────────────────────────────

def parse_ups_csv(file_obj) -> list:
    """
    Парсить CSV-експорт з billing.ups.com.

    Повертає список dict:
      invoice_number, invoice_date (date), tracking_number, service,
      amount (Decimal), currency (str)
    """
    raw = file_obj.read()
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8-sig', errors='replace')

    records = []
    reader  = csv.reader(io.StringIO(raw))
    headers = None
    c_inv = c_date = c_track = c_svc = c_amt = c_cur = None

    for row in reader:
        if not any(cell.strip() for cell in row):
            continue

        # Шукаємо рядок-заголовок
        if headers is None:
            headers = [c.strip() for c in row]
            c_inv   = _find_col(headers, _COL_INVOICE_NO)
            c_date  = _find_col(headers, _COL_INVOICE_DATE)
            c_track = _find_col(headers, _COL_TRACKING)
            c_svc   = _find_col(headers, _COL_SERVICE)
            c_amt   = _find_col(headers, _COL_AMOUNT)
            c_cur   = _find_col(headers, _COL_CURRENCY)
            # Якщо жодної знайомої колонки — це не заголовок
            if not c_inv and not c_date and not c_amt:
                headers = None
            continue

        def _get(col):
            if col is None:
                return ''
            idx = headers.index(col)
            return row[idx].strip() if idx < len(row) else ''

        invoice_no   = _get(c_inv)
        invoice_date = _get(c_date)
        tracking     = _get(c_track)
        service      = _get(c_svc)
        amount_raw   = _get(c_amt).replace(',', '').replace(' ', '').replace('\xa0', '')
        currency     = _get(c_cur) or 'EUR'

        if not invoice_no and not amount_raw:
            continue

        # Парсинг дати
        parsed_date = None
        for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%d.%m.%Y', '%Y%m%d', '%d/%m/%Y'):
            try:
                parsed_date = datetime.strptime(invoice_date, fmt).date()
                break
            except (ValueError, TypeError):
                pass
        parsed_date = parsed_date or date.today()

        # Парсинг суми
        try:
            amount = Decimal(amount_raw)
        except InvalidOperation:
            continue
        if amount <= 0:
            continue

        records.append({
            'invoice_number':  invoice_no,
            'invoice_date':    parsed_date,
            'tracking_number': tracking,
            'service':         service,
            'amount':          amount,
            'currency':        currency.upper()[:3],
        })

    return records


# ── API-режим ─────────────────────────────────────────────────────────────────

def fetch_from_api(carrier, start_date: str, end_date: str) -> list:
    """
    Отримує рахунки через UPS Invoice Management API.
    Повертає список dict (той самий формат що і parse_ups_csv).
    """
    from shipping.ups_client import UPSClient
    client = UPSClient(carrier=carrier)
    data   = client.get_billing_invoices(start_date, end_date)

    records = []
    for inv in data.get('invoices', []):
        inv_no       = inv.get('invoiceNumber', '')
        inv_date_raw = inv.get('invoiceDate', '')
        currency     = inv.get('currencyCode', 'EUR')

        parsed_date = None
        for fmt in ('%Y%m%d', '%Y-%m-%d'):
            try:
                parsed_date = datetime.strptime(inv_date_raw, fmt).date()
                break
            except (ValueError, TypeError):
                pass
        parsed_date = parsed_date or date.today()

        shipments = inv.get('shipments') or []
        if not shipments:
            amount = inv.get('invoiceAmount') or inv.get('netCharge') or 0
            try:
                amount = Decimal(str(amount))
            except InvalidOperation:
                continue
            if amount > 0:
                records.append({
                    'invoice_number':  inv_no,
                    'invoice_date':    parsed_date,
                    'tracking_number': '',
                    'service':         '',
                    'amount':          amount,
                    'currency':        currency,
                })
        else:
            for shp in shipments:
                tracking = shp.get('trackingNumber', '')
                service  = shp.get('serviceDescription', '') or shp.get('service', '')
                amount   = shp.get('netCharge') or shp.get('totalCharge') or 0
                try:
                    amount = Decimal(str(amount))
                except InvalidOperation:
                    continue
                if amount > 0:
                    records.append({
                        'invoice_number':  inv_no,
                        'invoice_date':    parsed_date,
                        'tracking_number': tracking,
                        'service':         service,
                        'amount':          amount,
                        'currency':        currency,
                    })

    return records


# ── Import до accounting.Expense ──────────────────────────────────────────────

def _get_ups_expense_category():
    from accounting.models import ExpenseCategory
    parent, _ = ExpenseCategory.objects.get_or_create(name='Доставка')
    cat, _    = ExpenseCategory.objects.get_or_create(
        name='UPS', defaults={'parent': parent}
    )
    if not cat.parent_id:
        cat.parent = parent
        cat.save(update_fields=['parent'])
    return cat


def import_billing_records(records: list, dry_run: bool = False) -> dict:
    """
    Записує розпарсені рядки як accounting.Expense.
    Пропускає дублікати (однакова дата + опис + сума).

    Повертає {'created': int, 'skipped': int, 'errors': list, 'preview': list}
    """
    from accounting.models import Expense

    cat     = _get_ups_expense_category()
    created = skipped = 0
    errors  = []
    preview = []

    for rec in records:
        inv_no   = rec.get('invoice_number') or '—'
        tracking = rec.get('tracking_number') or ''
        service  = rec.get('service') or ''

        desc = f"UPS Invoice #{inv_no}"
        if tracking:
            desc += f" | {tracking}"
        if service:
            desc += f" | {service}"

        is_dup = Expense.objects.filter(
            date=rec['invoice_date'],
            description=desc,
            amount=rec['amount'],
        ).exists()

        preview.append({
            'invoice_number':  inv_no,
            'invoice_date':    rec['invoice_date'].strftime('%d.%m.%Y'),
            'tracking_number': tracking,
            'service':         service,
            'amount':          str(rec['amount']),
            'currency':        rec['currency'],
            'duplicate':       is_dup,
        })

        if is_dup:
            skipped += 1
            continue

        if not dry_run:
            try:
                Expense.objects.create(
                    date=rec['invoice_date'],
                    amount=rec['amount'],
                    currency=rec['currency'],
                    category=cat,
                    description=desc,
                    is_vat_deductible=False,
                )
                created += 1
            except Exception as e:
                errors.append(str(e))
                logger.error('ups_billing import error: %s', e)
        else:
            created += 1

    return {
        'created': created,
        'skipped': skipped,
        'errors':  errors,
        'preview': preview,
    }
