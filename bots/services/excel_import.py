"""
bots/services/excel_import.py — Parse DigiKey Excel tables.

Supported formats (auto-detected from row-2 headers):
  • FILTERS — Real_quantities_and_price_formation_FILTERS.xlsx
  • CABLES  — Real_quantities_and_price_formation_CABLES.xlsx

Row layout:
  Row 1 — metadata / notes (skipped)
  Row 2 — column headers  (parsed here)
  Row 3+ — product data rows

Column naming convention: "Name (DK_CODE)" e.g. "Frequency (139)"
The numeric code in parentheses is the DigiKey additionalField code.
"""
import re
from typing import Optional


# ── Column-name → DK attribute code ─────────────────────────────────────────

def _extract_dk_code(header: str) -> Optional[str]:
    """'Frequency (139)' → '139';  'Style (91) (Unit)' → '91';  'Name' → None."""
    m = re.search(r'\((\d+)\)', str(header or ''))
    return m.group(1) if m else None


# ── fa_* field ↔ DK numeric code ────────────────────────────────────────────

FILTER_CODE_TO_FA = {
    '139': 'fa_frequency',
    '398': 'fa_bandwidth',
    '21':  'fa_filter_type',
    '428': 'fa_ripple',
    '327': 'fa_insertion_loss',
    '69':  'fa_mounting_type',
    '16':  'fa_package_case',
    '46':  'fa_size_dimension',
    '966': 'fa_height_max',
}

# Column names that map to special listing/offer fields (case-insensitive strip)
_SPECIAL = {
    'id full':                  'sku',
    'photo link':               'image_url',
    'datasheet link':           'datasheet_url',
    'preis':                    'price',
    'description':              'description',
    'break quantity 1 (moq)':   'moq',
    'break quantity 1':         'moq',
    'break price 1':            'price_break1',
    'leadtime to ship':         'lead_time',
    'available stock quantity': 'dk_quantity',
}


# ── Per-row data class (plain dict) ─────────────────────────────────────────
# Row dict keys:
#   sku          str
#   image_url    str
#   datasheet_url str
#   description  str
#   price        float | None  — price for 1 pc (Break Price 1 or Preis)
#   moq          int | None    — minimum order qty
#   lead_time    int | None    — days
#   dk_quantity  int | None    — DK stock qty
#   attrs        dict          — {dk_code_str: value_str}  numeric codes only
#   fa_fields    dict          — {fa_field_name: value_str} subset of attrs for filter
#   format       'filter'|'cable'|'generic'


def _safe_float(val) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _safe_int(val) -> Optional[int]:
    f = _safe_float(val)
    return int(f) if f is not None else None


def _clean_str(val) -> str:
    if val is None:
        return ''
    s = str(val).strip()
    return '' if s in ('-', 'None', 'nan') else s


# ── Main parser ──────────────────────────────────────────────────────────────

def parse_dk_excel(file_or_path) -> dict:
    """Parse a DigiKey Excel table.

    Args:
        file_or_path: opened file-like object OR path string.

    Returns:
        {
            'format': 'filter' | 'cable' | 'generic',
            'rows': [ row_dict, ... ],   # see keys above
            'headers': {col_idx: header_str},
            'error': str | None,
        }
    """
    try:
        import openpyxl
    except ImportError:
        return {'format': 'unknown', 'rows': [], 'headers': {}, 'error': 'openpyxl not installed'}

    try:
        wb = openpyxl.load_workbook(file_or_path, data_only=True, read_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
    except Exception as exc:
        return {'format': 'unknown', 'rows': [], 'headers': {}, 'error': str(exc)}

    # Row 1 — skip (metadata / notes)
    next(rows_iter, None)

    # Row 2 — headers
    header_row = next(rows_iter, None)
    if not header_row:
        return {'format': 'unknown', 'rows': [], 'headers': {}, 'error': 'Empty file'}

    # Build column map: col_idx → {'type': 'special'|'attr'|'skip', ...}
    col_map = {}
    raw_headers = {}
    has_filter_attrs = False
    has_cable_attrs  = False

    for idx, cell in enumerate(header_row):
        raw = _clean_str(cell)
        if not raw:
            continue
        raw_headers[idx] = raw
        raw_lower = raw.lower()

        # Col 0 is always SKU
        if idx == 0:
            col_map[idx] = {'type': 'special', 'key': 'sku'}
            continue

        # Check special-column names
        special = _SPECIAL.get(raw_lower)
        if special:
            col_map[idx] = {'type': 'special', 'key': special}
            continue

        # Check for numeric DK code in header
        code = _extract_dk_code(raw)
        if code:
            col_map[idx] = {'type': 'attr', 'code': code}
            if code in FILTER_CODE_TO_FA:
                has_filter_attrs = True
            else:
                has_cable_attrs = True

    if has_filter_attrs:
        fmt = 'filter'
    elif has_cable_attrs:
        fmt = 'cable'
    else:
        fmt = 'generic'

    # Row 3+ — data
    result_rows = []
    for row in rows_iter:
        if not row:
            continue
        # Skip rows where first 5 cells are all empty
        if all(_clean_str(row[i]) == '' for i in range(min(5, len(row)))):
            continue

        sku           = ''
        image_url     = ''
        datasheet_url = ''
        description   = ''
        price         = None
        moq           = None
        lead_time     = None
        dk_quantity   = None
        attrs         = {}
        fa_fields     = {}

        for col_idx, col_info in col_map.items():
            if col_idx >= len(row):
                continue
            raw_val = row[col_idx]
            if raw_val is None:
                continue
            val_str = _clean_str(raw_val)
            if not val_str:
                continue

            ctype = col_info['type']
            if ctype == 'special':
                key = col_info['key']
                if key == 'sku':
                    sku = val_str
                elif key == 'image_url':
                    image_url = val_str
                elif key == 'datasheet_url':
                    datasheet_url = val_str
                elif key == 'description':
                    description = val_str
                elif key == 'price':
                    price = _safe_float(raw_val)
                elif key == 'moq':
                    moq = _safe_int(raw_val)
                elif key == 'lead_time':
                    lead_time = _safe_int(raw_val)
                elif key == 'dk_quantity':
                    dk_quantity = _safe_int(raw_val)

            elif ctype == 'attr':
                code = col_info['code']
                attrs[code] = val_str
                fa_field = FILTER_CODE_TO_FA.get(code)
                if fa_field:
                    fa_fields[fa_field] = val_str

        if not sku:
            continue

        result_rows.append({
            'sku':           sku,
            'image_url':     image_url,
            'datasheet_url': datasheet_url,
            'description':   description,
            'price':         price,
            'moq':           moq,
            'lead_time':     lead_time,
            'dk_quantity':   dk_quantity,
            'attrs':         attrs,
            'fa_fields':     fa_fields,
            'format':        fmt,
        })

    return {
        'format':  fmt,
        'rows':    result_rows,
        'headers': raw_headers,
        'error':   None,
    }


def apply_row_to_listing(row: dict, listing) -> list:
    """Apply a parsed Excel row to a DigiKeyListing instance (in memory, no save).

    Returns list of field names that were changed.
    """
    changed = []

    def _set(field, value, max_len=None):
        if value in (None, ''):
            return
        if max_len and isinstance(value, str):
            value = value[:max_len]
        if getattr(listing, field, None) != value:
            setattr(listing, field, value)
            changed.append(field)

    # Core listing fields
    _set('dk_image_url',     row.get('image_url', ''))
    _set('dk_datasheet_url', row.get('datasheet_url', ''))
    _set('dk_description',   row.get('description', ''), max_len=2048)

    lead_time = row.get('lead_time')
    if lead_time is not None:
        _set('dk_lead_time_days', lead_time)

    moq = row.get('moq')
    if moq is not None:
        _set('dk_min_order_qty', max(1, moq))

    dk_qty = row.get('dk_quantity')
    if dk_qty is not None:
        _set('dk_quantity_override', dk_qty)

    # Price tier
    price = row.get('price')
    moq_for_price = max(1, moq or 1)
    if price is not None:
        existing = listing.dk_prices or []
        new_tier = {'qty': moq_for_price, 'price': round(price, 4)}
        # Replace tier with same qty or add
        tiers = [t for t in existing if int(t.get('qty', 0)) != moq_for_price]
        tiers.insert(0, new_tier)
        tiers.sort(key=lambda t: int(t.get('qty', 0)))
        if tiers != existing:
            listing.dk_prices = tiers
            changed.append('dk_prices')

    # fa_* fields (filter-specific)
    for field, value in row.get('fa_fields', {}).items():
        _set(field, value, max_len=200)

    # Merge attrs into dk_attributes
    new_attrs = dict(listing.dk_attributes or {})
    for code, value in row.get('attrs', {}).items():
        new_attrs[code] = value
    if new_attrs != (listing.dk_attributes or {}):
        listing.dk_attributes = new_attrs
        changed.append('dk_attributes')

    return changed
