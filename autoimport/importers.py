"""
Auto-import logic for 4 import types:
  - import_sales()    → SalesOrder + SalesOrderLine
  - import_products() → Product catalog
  - import_receipt()  → InventoryTransaction (Incoming)
  - import_adjust()   → InventoryTransaction (Adjustment)

All functions accept a pandas DataFrame and return:
  {'created': int, 'updated': int, 'skipped': int, 'errors': [str]}

column_map dict: {internal_field_name: actual_column_in_df}
  e.g. {'order_number': 'Bestellnummer', 'sku_raw': 'Artikelnummer', 'qty': 'Menge'}
  Fields NOT in column_map fall back to fuzzy auto-detection.
"""
import re
from decimal import Decimal, InvalidOperation

import pandas as pd
from django.db import transaction
from django.utils import timezone


# ─── Shared helpers ───────────────────────────────────────────────────────────

def norm_col(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())


def find_col(cols, candidates):
    cols_norm = {norm_col(c): c for c in cols}
    for cand in candidates:
        cn = norm_col(cand)
        if cn in cols_norm:
            return cols_norm[cn]
    for cand in candidates:
        cn = norm_col(cand)
        for k, orig in cols_norm.items():
            if cn in k:
                return orig
    return None


def clean_sku(x):
    if x is None:
        return ""
    try:
        if pd.isna(x):
            return ""
    except Exception:
        pass
    s = str(x).strip()
    if s.lower() in ("nan", "none", "null"):
        return ""
    return s.replace("\u200b", "")


def make_key(*parts):
    import hashlib
    raw = "|".join([str(p) for p in parts])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def _to_decimal(val, default=None):
    if val is None:
        return default
    try:
        if pd.isna(val):
            return default
    except Exception:
        pass
    s = str(val).replace(",", ".").strip()
    if s.lower() in ("", "nan", "none", "-", "—"):
        return default
    try:
        return Decimal(s)
    except InvalidOperation:
        return default


def _to_date(val):
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except Exception:
        pass
    s = str(val).strip()
    if s.lower() in ("", "nan", "none"):
        return None
    try:
        return pd.to_datetime(val, errors="coerce").date()
    except Exception:
        return None


def _inherit_str(val, last):
    if val is None:
        return last
    s = str(val).strip()
    if s == "" or s.lower() in ("nan", "none", "-", "—"):
        return last
    return s


def _inherit_dt(val, last):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return last
    s = str(val).strip()
    if s.lower() in ("", "nan", "none", "-", "—"):
        return last
    try:
        d = pd.to_datetime(val, errors="coerce")
        if pd.isna(d):
            return last
        return timezone.make_aware(d.to_pydatetime(), timezone.get_current_timezone())
    except Exception:
        return last


def _inherit_date(val, last):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return last
    s = str(val).strip()
    if s.lower() in ("", "nan", "none", "-", "—"):
        return last
    try:
        d = pd.to_datetime(val, errors="coerce")
        if pd.isna(d):
            return last
        return d.date()
    except Exception:
        return last


def _resolve_product(sku_raw: str):
    from inventory.models import Product, ProductAlias
    sku_raw = clean_sku(sku_raw)
    if not sku_raw:
        return None, sku_raw
    p = Product.objects.filter(sku=sku_raw).first()
    if p:
        return p, sku_raw
    alias = ProductAlias.objects.filter(alias=sku_raw).select_related("product").first()
    if alias:
        return alias.product, sku_raw
    p, _ = Product.objects.get_or_create(sku=sku_raw, defaults={"category": "other"})
    return p, sku_raw


def _rc(df_cols, field_name: str, column_map: dict, candidates: list):
    """
    Resolve column: check explicit column_map first, then fall back to fuzzy matching.
    column_map values of '' or None mean "use auto-detect".
    """
    mapped = column_map.get(field_name, '')
    if mapped and mapped in df_cols:
        return mapped
    return find_col(df_cols, candidates)


# ─── Field definitions per import type (used by UI + importers) ──────────────

IMPORT_FIELDS = {
    'sales': [
        ('order_number',      'Номер замовлення',       True),
        ('sku_raw',           'SKU товару',              True),
        ('qty',               'Кількість',               True),
        ('source',            'Джерело замовлення',      False),
        ('order_date',        'Дата замовлення',         False),
        ('status',            'Статус',                  False),
        ('client',            'Клієнт (білінг)',          False),
        ('contact_name',      'Контактна особа (білінг)',False),
        ('email',             'Email (білінг)',           False),
        ('phone',             'Телефон (білінг)',         False),
        ('ship_name',         "Ім'я отримувача",         False),
        ('ship_company',      'Компанія отримувача',     False),
        ('ship_phone',        'Телефон доставки',        False),
        ('ship_email',        'Email доставки',          False),
        ('unit_price',        'Ціна за одиницю',         False),
        ('currency',          'Валюта',                  False),
        ('shipping_courier',  'Перевізник',              False),
        ('tracking_number',   'Трекінг номер',           False),
        ('shipping_deadline', 'Дедлайн відправки',       False),
        ('shipped_at',        'Дата відправки',          False),
        ('addr_street',       'Вулиця',                  False),
        ('addr_city',         'Місто',                   False),
        ('addr_zip',          'Поштовий індекс',         False),
        ('addr_country',      'Країна (ISO2)',            False),
        ('affects_stock',     'Впливає на склад',        False),
    ],
    'products': [
        ('sku',               'SKU',                     True),
        ('name',              'Назва товару',             False),
        ('category',          'Категорія',               False),
        ('manufacturer',      'Виробник',                False),
        ('purchase_price',    'Ціна закупки',            False),
        ('sale_price',        'Ціна продажу',            False),
        ('reorder_point',     'Мінімальний залишок',     False),
        ('lead_time_days',    'Термін поставки (дні)',   False),
        ('initial_stock',     'Початковий залишок',      False),
        ('unit_type',         'Одиниця виміру',          False),
        ('hs_code',           'HS Code',                 False),
        ('country_of_origin', 'Країна походження',      False),
        ('net_weight_g',      'Вага (г)',                False),
        ('is_active',         'Активний',                False),
    ],
    'receipt': [
        ('sku',  'SKU',                   True),
        ('qty',  'Кількість',             True),
        ('date', 'Дата',                  False),
        ('ref',  'Документ / референс',  False),
    ],
    'adjust': [
        ('sku',       'SKU',                              True),
        ('new_qty',   'Новий залишок (абсолютне значення)', False),
        ('delta_qty', 'Зміна залишку (±)',                False),
        ('date',      'Дата',                             False),
        ('ref',       'Документ / референс',             False),
    ],
}


# ─── 1. Sales import ─────────────────────────────────────────────────────────

def import_sales(df: pd.DataFrame, source: str = 'auto', conflict_strategy: str = 'skip',
                 dry_run: bool = False, column_map: dict = None) -> dict:
    from sales.models import SalesOrder, SalesOrderLine

    column_map = column_map or {}
    result = {'created': 0, 'updated': 0, 'skipped': 0, 'errors': []}

    cols = list(df.columns)
    rc = lambda f, c: _rc(cols, f, column_map, c)

    sku_col      = rc('sku_raw',           ['sku_raw', 'sku', 'product number', 'product namber', 'part number', 'pn'])
    qty_col      = rc('qty',               ['qty', 'quantity', 'кількість'])
    order_col    = rc('order_number',      ['order_number', 'sales order', 'order number', 'order', 'замовлення'])
    date_col     = rc('order_date',        ['order_date', 'order date', 'date', 'дата'])
    status_col   = rc('status',            ['status', 'статус'])
    client_col      = rc('client',       ['client', 'customer', 'клієнт'])
    contact_col     = rc('contact_name', ['contact_name', 'contact', 'контактна особа', 'контакт'])
    email_col       = rc('email',        ['email'])
    phone_col       = rc('phone',        ['phone', 'телефон'])
    ship_name_col   = rc('ship_name',    ['ship_name', 'recipient', 'recipient name', 'отримувач'])
    ship_company_col= rc('ship_company', ['ship_company', 'recipient company', 'компанія отримувача'])
    ship_phone_col  = rc('ship_phone',   ['ship_phone', 'recipient phone', 'телефон доставки'])
    ship_email_col  = rc('ship_email',   ['ship_email', 'recipient email', 'email доставки'])
    source_col   = rc('source',            ['source', 'джерело'])
    ship_col     = rc('shipped_at',        ['shipped_at', 'shipping', 'відправлено'])
    courier_col  = rc('shipping_courier',  ['shipping_courier', 'courier', 'перевізник'])
    tracking_col = rc('tracking_number',   ['tracking_number', 'tracking', 'трекінг'])
    deadline_col = rc('shipping_deadline', ['shipping_deadline', 'deadline', 'дедлайн'])
    addr_col     = rc('shipping_address',  ['shipping_address', 'address', 'адреса'])
    street_col   = rc('addr_street',       ['addr_street', 'street', 'вулиця'])
    city_col     = rc('addr_city',         ['addr_city', 'city', 'місто'])
    zip_col      = rc('addr_zip',          ['addr_zip', 'zip', 'postal', 'індекс'])
    country_col  = rc('addr_country',      ['addr_country', 'country', 'країна'])
    currency_col = rc('currency',          ['currency', 'валюта'])
    price_col    = rc('unit_price',        ['unit_price', 'price', 'ціна'])
    affects_col  = rc('affects_stock',     ['affects_stock', 'affects stock'])

    if not order_col:
        result['errors'].append('Не знайдено колонку з номером замовлення (order_number)')
        return result
    if not sku_col or not qty_col:
        result['errors'].append('Не знайдено колонки SKU та/або кількості')
        return result

    last_order_number = None
    last_order_date   = None
    last_ship_date    = None
    last_courier      = ''
    last_tracking     = ''
    last_client       = ''
    last_contact      = ''
    last_email        = ''
    last_phone        = ''
    last_ship_name    = ''
    last_ship_company = ''
    last_ship_phone   = ''
    last_ship_email   = ''
    last_deadline     = None
    last_addr         = ''
    last_street       = ''
    last_city         = ''
    last_zip          = ''
    last_country      = ''
    last_currency     = 'USD'
    last_source       = source
    last_status       = 'received'
    last_affects      = True

    with transaction.atomic():
        sp = transaction.savepoint()
        for _, row in df.iterrows():
            raw_qty = row.get(qty_col) if qty_col else None
            if raw_qty is None or str(raw_qty).strip().lower() in ('', 'nan', 'none', '-', '—'):
                continue
            qty = _to_decimal(raw_qty)
            if qty is None or not qty.is_finite() or qty <= 0:
                continue

            sku_raw = clean_sku(row.get(sku_col)) if sku_col else ''
            if not sku_raw:
                continue

            order_number = clean_sku(row.get(order_col)) if order_col else ''
            if order_number:
                last_order_number = order_number
            else:
                order_number = last_order_number
            if not order_number:
                continue

            raw_date = row.get(date_col) if date_col else None
            if raw_date is not None:
                d = _to_date(raw_date)
                if d:
                    last_order_date = d

            if source_col:
                v = clean_sku(row.get(source_col))
                if v:
                    last_source = v
            if status_col:
                v = clean_sku(row.get(status_col))
                if v:
                    last_status = v
            if affects_col:
                v = row.get(affects_col)
                if v is not None and str(v).strip().lower() not in ('nan', 'none', ''):
                    last_affects = str(v).strip().lower() in ('1', 'true', 'yes', 'так', '+')

            last_ship_date = _inherit_dt(row.get(ship_col) if ship_col else None, last_ship_date)
            last_courier   = _inherit_str(row.get(courier_col) if courier_col else None, last_courier)
            last_tracking  = _inherit_str(row.get(tracking_col) if tracking_col else None, last_tracking)
            last_client       = _inherit_str(row.get(client_col)       if client_col       else None, last_client)
            last_contact      = _inherit_str(row.get(contact_col)     if contact_col      else None, last_contact)
            last_email        = _inherit_str(row.get(email_col)       if email_col        else None, last_email)
            last_phone        = _inherit_str(row.get(phone_col)       if phone_col        else None, last_phone)
            last_ship_name    = _inherit_str(row.get(ship_name_col)   if ship_name_col    else None, last_ship_name)
            last_ship_company = _inherit_str(row.get(ship_company_col)if ship_company_col else None, last_ship_company)
            last_ship_phone   = _inherit_str(row.get(ship_phone_col)  if ship_phone_col   else None, last_ship_phone)
            last_ship_email   = _inherit_str(row.get(ship_email_col)  if ship_email_col   else None, last_ship_email)
            last_deadline  = _inherit_date(row.get(deadline_col) if deadline_col else None, last_deadline)
            last_addr      = _inherit_str(row.get(addr_col) if addr_col else None, last_addr)
            last_street    = _inherit_str(row.get(street_col) if street_col else None, last_street)
            last_city      = _inherit_str(row.get(city_col) if city_col else None, last_city)
            last_zip       = _inherit_str(row.get(zip_col) if zip_col else None, last_zip)
            last_country   = _inherit_str(row.get(country_col) if country_col else None, last_country)
            last_currency  = _inherit_str(row.get(currency_col) if currency_col else None, last_currency) or 'USD'

            try:
                product, _ = _resolve_product(sku_raw)
            except Exception as e:
                result['errors'].append(f'SKU {sku_raw}: {e}')
                continue

            if not dry_run:
                so, so_created = SalesOrder.objects.get_or_create(
                    source=last_source,
                    order_number=order_number,
                    defaults={
                        'order_date':       last_order_date,
                        'status':           last_status,
                        'affects_stock':    last_affects,
                        'client':           last_client,
                        'contact_name':     last_contact,
                        'email':            last_email,
                        'phone':            last_phone,
                        'ship_name':        last_ship_name,
                        'ship_company':     last_ship_company,
                        'ship_phone':       last_ship_phone,
                        'ship_email':       last_ship_email,
                        'shipping_deadline':last_deadline,
                        'shipping_address': last_addr,
                        'addr_street':      last_street,
                        'addr_city':        last_city,
                        'addr_zip':         last_zip,
                        'addr_country':     last_country,
                        'currency':         last_currency,
                    },
                )
                if so_created:
                    result['created'] += 1
                elif conflict_strategy == 'update':
                    changed = False
                    if last_ship_date and so.shipped_at is None:
                        so.shipped_at = last_ship_date; changed = True
                    if last_courier and not so.shipping_courier:
                        so.shipping_courier = last_courier; changed = True
                    if last_tracking and not so.tracking_number:
                        so.tracking_number = last_tracking; changed = True
                    if last_client and not so.client:
                        so.client = last_client; changed = True
                    if last_email and not so.email:
                        so.email = last_email; changed = True
                    if last_deadline and so.shipping_deadline is None:
                        so.shipping_deadline = last_deadline; changed = True
                    if changed:
                        so.save()
                        result['updated'] += 1
                    else:
                        result['skipped'] += 1
                else:
                    result['skipped'] += 1
                    continue

                if not SalesOrderLine.objects.filter(order=so, product=product, qty=qty).exists():
                    unit_price = _to_decimal(row.get(price_col)) if price_col else None
                    SalesOrderLine.objects.create(
                        order=so, product=product, sku_raw=sku_raw, qty=qty,
                        unit_price=unit_price, currency=last_currency,
                    )
            else:
                result['created'] += 1

        if dry_run:
            transaction.savepoint_rollback(sp)
        else:
            transaction.savepoint_commit(sp)

    return result


# ─── 2. Products import ──────────────────────────────────────────────────────

def import_products(df: pd.DataFrame, dry_run: bool = False, column_map: dict = None) -> dict:
    from inventory.models import Product, InventoryTransaction, Location

    column_map = column_map or {}
    result = {'created': 0, 'updated': 0, 'skipped': 0, 'errors': []}

    cols = list(df.columns)
    rc = lambda f, c: _rc(cols, f, column_map, c)

    sku_col      = rc('sku',               ['sku', 'id full', 'id_full', 'product number', 'part number'])
    name_col     = rc('name',              ['name', 'назва', 'description'])
    cat_col      = rc('category',          ['category', 'категорія'])
    manuf_col    = rc('manufacturer',      ['manufacturer', 'виробник'])
    pprice_col   = rc('purchase_price',    ['purchase_price', 'purchase price', 'ціна закупки'])
    sprice_col   = rc('sale_price',        ['sale_price', 'sale price', 'ціна продажу'])
    reorder_col  = rc('reorder_point',     ['reorder_point', 'reorder point', 'мінімальний залишок'])
    lead_col     = rc('lead_time_days',    ['lead_time_days', 'lead time', 'lead time days'])
    stock_col    = rc('initial_stock',     ['initial_stock', 'initial stock', 'stock', 'залишок'])
    unit_col     = rc('unit_type',         ['unit_type', 'unit type', 'одиниця'])
    hs_col       = rc('hs_code',           ['hs_code', 'hs code', 'hscode'])
    origin_col   = rc('country_of_origin', ['country_of_origin', 'country of origin', 'країна походження'])
    weight_col   = rc('net_weight_g',      ['net_weight_g', 'weight', 'вага (г)'])
    active_col   = rc('is_active',         ['is_active', 'active', 'активний'])

    if not sku_col:
        result['errors'].append('Не знайдено колонку SKU (sku / ID FULL / product number)')
        return result

    with transaction.atomic():
        sp = transaction.savepoint()
        for _, row in df.iterrows():
            sku = clean_sku(row.get(sku_col))
            if not sku:
                continue

            defaults = {}
            if name_col:
                v = clean_sku(row.get(name_col))
                if v:
                    defaults['name'] = v
            if cat_col:
                v = clean_sku(row.get(cat_col))
                if v:
                    defaults['category'] = v
            if manuf_col:
                v = clean_sku(row.get(manuf_col))
                if v:
                    defaults['manufacturer'] = v
            if pprice_col:
                v = _to_decimal(row.get(pprice_col))
                if v is not None:
                    defaults['purchase_price'] = v
            if sprice_col:
                v = _to_decimal(row.get(sprice_col))
                if v is not None:
                    defaults['sale_price'] = v
            if reorder_col:
                v = _to_decimal(row.get(reorder_col))
                if v is not None:
                    defaults['reorder_point'] = v
            if lead_col:
                v = _to_decimal(row.get(lead_col))
                if v is not None:
                    defaults['lead_time_days'] = int(v)
            if unit_col:
                v = clean_sku(row.get(unit_col))
                if v:
                    defaults['unit_type'] = v
            if hs_col:
                v = clean_sku(row.get(hs_col))
                if v:
                    defaults['hs_code'] = v
            if origin_col:
                v = clean_sku(row.get(origin_col))
                if v:
                    defaults['country_of_origin'] = v[:2].upper()
            if weight_col:
                v = _to_decimal(row.get(weight_col))
                if v is not None:
                    defaults['net_weight_g'] = v
            if active_col:
                v = row.get(active_col)
                if v is not None and str(v).strip().lower() not in ('nan', 'none', ''):
                    defaults['is_active'] = str(v).strip().lower() not in ('0', 'false', 'no', 'ні', '-')

            if not dry_run:
                product, created = Product.objects.get_or_create(sku=sku, defaults=defaults)
                if created:
                    result['created'] += 1
                else:
                    changed = False
                    for field, value in defaults.items():
                        if getattr(product, field) != value:
                            setattr(product, field, value)
                            changed = True
                    if changed:
                        product.save()
                        result['updated'] += 1
                    else:
                        result['skipped'] += 1

                if stock_col:
                    qty = _to_decimal(row.get(stock_col))
                    if qty is not None and qty != 0:
                        location, _ = Location.objects.get_or_create(code='MAIN', defaults={'name': 'Main'})
                        ext_key = make_key('AUTOIMPORT_STOCK', sku)
                        if not InventoryTransaction.objects.filter(external_key=ext_key).exists():
                            InventoryTransaction.objects.create(
                                external_key=ext_key, product=product, location=location,
                                tx_type='Adjustment', qty=qty,
                                ref_doc='auto-import:initial', tx_date=timezone.now(),
                            )
            else:
                result['created'] += 1

        if dry_run:
            transaction.savepoint_rollback(sp)
        else:
            transaction.savepoint_commit(sp)

    return result


# ─── 3. Stock receipt import ─────────────────────────────────────────────────

def import_receipt(df: pd.DataFrame, dry_run: bool = False, column_map: dict = None) -> dict:
    from inventory.models import InventoryTransaction, Location

    column_map = column_map or {}
    result = {'created': 0, 'updated': 0, 'skipped': 0, 'errors': []}

    cols = list(df.columns)
    rc = lambda f, c: _rc(cols, f, column_map, c)

    sku_col  = rc('sku',  ['sku', 'product', 'id full', 'артикул'])
    qty_col  = rc('qty',  ['qty', 'quantity', 'кількість'])
    date_col = rc('date', ['date', 'дата'])
    ref_col  = rc('ref',  ['ref', 'reference', 'ref_doc', 'документ'])

    if not sku_col or not qty_col:
        result['errors'].append('Не знайдено колонки SKU та/або кількості')
        return result

    location, _ = Location.objects.get_or_create(code='MAIN', defaults={'name': 'Main'})

    with transaction.atomic():
        sp = transaction.savepoint()
        for _, row in df.iterrows():
            sku = clean_sku(row.get(sku_col))
            if not sku:
                continue
            qty = _to_decimal(row.get(qty_col))
            if qty is None or qty <= 0:
                continue

            date_val = _to_date(row.get(date_col)) if date_col else None
            ref_val  = clean_sku(row.get(ref_col)) if ref_col else ''
            ref_val  = ref_val or 'auto-import'

            try:
                product, _ = _resolve_product(sku)
            except Exception as e:
                result['errors'].append(f'SKU {sku}: {e}')
                continue

            ext_key = make_key('RECEIPT', sku, str(date_val), ref_val, str(qty))
            if InventoryTransaction.objects.filter(external_key=ext_key).exists():
                result['skipped'] += 1
                continue

            if not dry_run:
                InventoryTransaction.objects.create(
                    external_key=ext_key, product=product, location=location,
                    tx_type='Incoming', qty=qty, ref_doc=ref_val,
                    tx_date=timezone.make_aware(
                        pd.Timestamp(date_val).to_pydatetime(),
                        timezone.get_current_timezone()
                    ) if date_val else timezone.now(),
                )
            result['created'] += 1

        if dry_run:
            transaction.savepoint_rollback(sp)
        else:
            transaction.savepoint_commit(sp)

    return result


# ─── 4. Stock adjustment import ──────────────────────────────────────────────

def import_adjust(df: pd.DataFrame, dry_run: bool = False, column_map: dict = None) -> dict:
    from inventory.models import InventoryTransaction, Location
    from django.db.models import Sum

    column_map = column_map or {}
    result = {'created': 0, 'updated': 0, 'skipped': 0, 'errors': []}

    cols = list(df.columns)
    rc = lambda f, c: _rc(cols, f, column_map, c)

    sku_col     = rc('sku',       ['sku', 'product', 'id full', 'артикул'])
    new_qty_col = rc('new_qty',   ['new_qty', 'new qty', 'новий залишок', 'target'])
    delta_col   = rc('delta_qty', ['delta_qty', 'delta', 'delta qty', 'зміна'])
    date_col    = rc('date',      ['date', 'дата'])
    ref_col     = rc('ref',       ['ref', 'reference', 'ref_doc', 'документ'])

    if not sku_col:
        result['errors'].append('Не знайдено колонку SKU')
        return result
    if not new_qty_col and not delta_col:
        result['errors'].append('Не знайдено колонку new_qty (новий залишок) або delta_qty (зміна ±)')
        return result

    location, _ = Location.objects.get_or_create(code='MAIN', defaults={'name': 'Main'})

    with transaction.atomic():
        sp = transaction.savepoint()
        for _, row in df.iterrows():
            sku = clean_sku(row.get(sku_col))
            if not sku:
                continue

            date_val = _to_date(row.get(date_col)) if date_col else None
            ref_val  = clean_sku(row.get(ref_col)) if ref_col else 'auto-import'
            ref_val  = ref_val or 'auto-import'

            try:
                product, _ = _resolve_product(sku)
            except Exception as e:
                result['errors'].append(f'SKU {sku}: {e}')
                continue

            if new_qty_col:
                new_qty = _to_decimal(row.get(new_qty_col))
                if new_qty is None:
                    continue
                current = InventoryTransaction.objects.filter(
                    product=product, location=location
                ).aggregate(total=Sum('qty'))['total'] or Decimal('0')
                delta = new_qty - current
                if delta == 0:
                    result['skipped'] += 1
                    continue
            else:
                delta = _to_decimal(row.get(delta_col))
                if delta is None or delta == 0:
                    continue

            ext_key = make_key('ADJUST', sku, str(date_val), ref_val, str(delta))
            if InventoryTransaction.objects.filter(external_key=ext_key).exists():
                result['skipped'] += 1
                continue

            if not dry_run:
                InventoryTransaction.objects.create(
                    external_key=ext_key, product=product, location=location,
                    tx_type='Adjustment', qty=delta, ref_doc=ref_val,
                    tx_date=timezone.make_aware(
                        pd.Timestamp(date_val).to_pydatetime(),
                        timezone.get_current_timezone()
                    ) if date_val else timezone.now(),
                )
            result['created'] += 1

        if dry_run:
            transaction.savepoint_rollback(sp)
        else:
            transaction.savepoint_commit(sp)

    return result
