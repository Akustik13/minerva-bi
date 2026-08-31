"""
jlcpcb/services/api.py — JLCPCB Open API client

Base URL:  https://open.jlcpcb.com
Auth:      JOP scheme (single Authorization header)
  Authorization: JOP appid="...",accesskey="...",timestamp="...",nonce="...",signature="..."
  string-to-sign = "METHOD\n{uri_with_query}\n{timestamp_seconds}\n{nonce}\n{body}\n"
  signature      = base64(HMAC-SHA256(secret_key, string_to_sign))

PCB endpoints (per official docs):
  POST /overseas/openapi/pcb/order/detail              — {"batchNum":"W202501..."} → order info
  POST /overseas/openapi/pcb/pageBatchInfoByOrderType  — paginated batch list by date range
  POST /overseas/openapi/pcb/wip/get                   — {"orderUUID":"..."} → WIP progress
  POST /overseas/openapi/pcb/audit/get                 — {"key":"..."} → Gerber pre-review
  GET  /overseas/openapi/pcb/getSteelPriceConfig       — health check (GET, no params)

Gerber / ordering workflow (new endpoints):
  POST /overseas/openapi/pcb/uploadGerber   — multipart: upload .zip/.rar → fileKey string
  POST /overseas/openapi/pcb/calculate      — price quotation + gerberTop/gerberBottom image URLs
  POST /overseas/openapi/pcb/create         — place real order (charges JLCPCB account!)
  GET  /overseas/openapi/pcb/getAvailablePlateBrandAndTg  — available plate brands and Tg values
  GET  /overseas/openapi/pcb/getImpedanceTemplateSettingList — impedance templates

Response structure for order/detail:
  data.orderItem[0].pcbItem.orderStatus  ← INTEGER (0-5), not string!
  0=Cancelled, 1=Pending Review, 2=Awaiting Confirmation,
  3=Confirmed, 4=Submitted to factory (In Production), 5=Shipped
"""
import base64
import hashlib
import hmac
import json
import logging
import secrets
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from django.db.models import Q

logger = logging.getLogger(__name__)

# ── Status mapping ─────────────────────────────────────────────────────────────

_STATUS_PRIORITY = {
    'ordered':       0,
    'reviewed':      1,
    'in_production': 2,
    'manufactured':  3,
    'shipped':       4,
    'delivered':     5,
    'cancelled':     99,
}

# JLCPCB API orderStatus is an INTEGER (from official docs):
# 0=Cancelled, 1=Pending Review, 2=Awaiting Confirmation,
# 3=Confirmed, 4=Submitted to factory, 5=Shipped
# Note: "delivered" has no API status — use deliveryTime field or manual update
JLC_INT_STATUS_MAP = {
    0: 'cancelled',
    1: 'ordered',        # Pending Review
    2: 'reviewed',       # Awaiting Confirmation
    3: 'reviewed',       # Confirmed
    4: 'in_production',  # Submitted to factory
    5: 'shipped',
}


class JLCAPIError(Exception):
    pass


def status_can_advance(current: str, new: str) -> bool:
    return _STATUS_PRIORITY.get(new, 0) > _STATUS_PRIORITY.get(current, 0)


def map_jlc_status(status_int) -> str:
    """Map API integer orderStatus to local_status. Accepts int or string int."""
    if status_int is None:
        return 'ordered'
    try:
        return JLC_INT_STATUS_MAP.get(int(status_int), 'ordered')
    except (TypeError, ValueError):
        logger.warning('Unexpected JLCPCB orderStatus value: %r', status_int)
        return 'ordered'


def extract_pcb_item(raw: dict) -> dict:
    """
    Navigate nested order/detail response to find pcbItem.
    orderType: 0=batch PCB, 1=prototype/sample PCB, 3=Stencil
    Returns pcbItem dict or {}.
    """
    items = raw.get('orderItem', [])
    # Prefer prototype (1) then batch (0), skip stencil (3)
    for order_type in (1, 0):
        for item in items:
            if item.get('orderType') == order_type:
                pcb = item.get('pcbItem') or {}
                if pcb:
                    return pcb
    # Final fallback: any pcbItem
    for item in items:
        pcb = item.get('pcbItem')
        if pcb:
            return pcb
    return {}


# ── Gerber image extraction ───────────────────────────────────────────────────

# Known field names that JLCPCB API may use for board preview images in pcbItem
_GERBER_TOP_FIELDS    = ('gerberTop', 'gerberTopUrl', 'topImageUrl', 'topImage', 'pcbTopImageUrl')
_GERBER_BOTTOM_FIELDS = ('gerberBottom', 'gerberBottomUrl', 'bottomImageUrl', 'bottomImage', 'pcbBottomImageUrl')


def _extract_gerber_images(pcb: dict, raw: dict) -> None:
    """
    Try to extract gerber preview image URLs from pcbItem and write into raw.
    Preserves previously-stored Gerber-flow images (won't overwrite if API has none).
    """
    top = next((pcb.get(f) for f in _GERBER_TOP_FIELDS if pcb.get(f)), None)
    bot = next((pcb.get(f) for f in _GERBER_BOTTOM_FIELDS if pcb.get(f)), None)
    # Also check root-level response fields (some endpoints return them at top level)
    if not top:
        top = next((raw.get(f) for f in _GERBER_TOP_FIELDS if raw.get(f)), None)
    if not bot:
        bot = next((raw.get(f) for f in _GERBER_BOTTOM_FIELDS if raw.get(f)), None)
    if top:
        raw['gerber_top'] = top
    if bot:
        raw['gerber_bottom'] = bot


# ── Product matching ──────────────────────────────────────────────────────────

def find_product_for_jlc_name(jlc_name: str):
    """
    Match JLC order description → Product in catalog.

    JLC Gerber name: AN120202-01H_2L_FR4_0.8x160x77.5mm_
    Product SKU:     AN120202-01H
    Strategy: prefix before first '_' = SKU candidate.

    Returns (Product | None, match_type: str)
    """
    from inventory.models import Product
    from jlcpcb.models import JLCProductMapping

    if not jlc_name:
        return None, 'unmatched'

    # 1. Manual mapping table (always wins)
    mapping = JLCProductMapping.objects.filter(
        Q(jlc_reference__iexact=jlc_name) |
        Q(jlc_reference__iexact=jlc_name.split('_')[0])
    ).select_related('product').first()
    if mapping:
        return mapping.product, 'mapping'

    # 2. Prefix before first underscore
    prefix = jlc_name.split('_')[0].strip()
    if prefix:
        product = Product.objects.filter(sku__iexact=prefix).first()
        if product:
            return product, 'auto_prefix'
        product = Product.objects.filter(sku_short__iexact=prefix).first()
        if product:
            return product, 'auto_prefix'

    # 3. Any active product SKU that is a prefix of the JLC name
    jlc_upper = jlc_name.upper()
    for p in Product.objects.filter(is_active=True).only('pk', 'sku'):
        if p.sku and jlc_upper.startswith(p.sku.upper()):
            return p, 'auto_startswith'

    return None, 'unmatched'


# ── Inventory receipt ─────────────────────────────────────────────────────────

def receive_into_inventory(jlc_order, location_code: str = None, performed_by=None):
    """Create InventoryTransaction(Incoming). Idempotent via external_key."""
    from decimal import Decimal
    from inventory.models import InventoryTransaction, Location
    from jlcpcb.models import JLCConfig

    if not jlc_order.product_id:
        logger.warning('JLCOrder %s: no product linked, skip receipt', jlc_order.jlc_order_id)
        return None

    cfg = JLCConfig.get()
    loc_code = location_code or cfg.default_location or 'MAIN'
    location, _ = Location.objects.get_or_create(code=loc_code, defaults={'name': loc_code})

    ext_key = f'jlc-{jlc_order.jlc_order_id}-recv'
    if InventoryTransaction.objects.filter(external_key=ext_key).exists():
        return None

    qty = jlc_order.quantity - float(jlc_order.received_qty)
    if qty <= 0:
        return None

    tx = InventoryTransaction.objects.create(
        tx_type=InventoryTransaction.TxType.INCOMING,
        product=jlc_order.product,
        location=location,
        qty=Decimal(str(qty)),
        ref_doc=f'JLC-{jlc_order.jlc_order_id}',
        external_key=ext_key,
        performed_by=performed_by,
    )
    jlc_order.received_qty = Decimal(str(jlc_order.quantity))
    jlc_order.save(update_fields=['received_qty', 'updated_at'])
    return tx


# ── API Client ────────────────────────────────────────────────────────────────

_NONCE_CHARS = string.ascii_letters + string.digits


class JLCAPIClient:
    """
    JLCPCB Open API client (overseas portal).

    Base URL: https://open.jlcpcb.com
    Auth: JOP scheme — single Authorization header
      Authorization: JOP appid="...",accesskey="...",timestamp="...",nonce="...",signature="..."
      string-to-sign: "METHOD\n{path+query}\n{ts_seconds}\n{nonce}\n{body}\n"
      signature:      base64(HMAC-SHA256(secret_key, string_to_sign))

    PCB order endpoints (POST, JSON body):
      EP_PCB_CONFIG  GET  /overseas/openapi/pcb/getSteelPriceConfig  ← health check
      EP_PCB_DETAIL  POST /overseas/openapi/pcb/order/detail  {"batchNum":"W202501..."}
      EP_PCB_WIP     POST /overseas/openapi/pcb/wip/get       {"orderUUID":"..."}  ← orderId from create response
      EP_PCB_AUDIT   POST /overseas/openapi/pcb/audit/get     {"batchNum":"..."}

    IMPORTANT: There is no PCB order list endpoint.
    Orders are tracked individually by batch number (batchNum), which is the
    order number visible in the JLCPCB order history page (e.g. W2025040800001).
    """

    BASE_URL = 'https://open.jlcpcb.com'

    # PCB endpoints (per official docs)
    EP_PCB_CONFIG     = '/overseas/openapi/pcb/getSteelPriceConfig'          # GET — health check
    EP_PCB_DETAIL     = '/overseas/openapi/pcb/order/detail'                 # POST {"batchNum":"..."}
    EP_PCB_BATCH_LIST = '/overseas/openapi/pcb/pageBatchInfoByOrderType'     # POST — paginated order list
    EP_PCB_WIP        = '/overseas/openapi/pcb/wip/get'                      # POST {"orderUUID":"..."}
    EP_PCB_AUDIT      = '/overseas/openapi/pcb/audit/get'                    # POST {"key":"..."} Gerber review

    # Gerber / ordering endpoints
    EP_PCB_UPLOAD_GERBER = '/overseas/openapi/pcb/uploadGerber'              # POST multipart → fileKey
    EP_PCB_CALCULATE     = '/overseas/openapi/pcb/calculate'                 # POST JSON → price + gerber images
    EP_PCB_CREATE        = '/overseas/openapi/pcb/create'                    # POST JSON → orderId/batchNum
    EP_PCB_PLATE_BRANDS  = '/overseas/openapi/pcb/getAvailablePlateBrandAndTg'        # GET → plate brand list
    EP_PCB_IMPEDANCE     = '/overseas/openapi/pcb/getImpedanceTemplateSettingList'    # GET → impedance templates

    # Always-authorized endpoint (JPay balance) — used to verify credentials
    EP_JPAY_BALANCE = '/overseas/openapi/jpay/customerJpayAccount/getAccountDetail'  # GET

    def __init__(self, app_id: str, access_key: str, secret_key: str):
        self.app_id     = app_id
        self.access_key = access_key
        self.secret_key = secret_key

    # ── Auth ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _nonce() -> str:
        return ''.join(secrets.choice(_NONCE_CHARS) for _ in range(32))

    def _sign(self, method: str, path: str, query: str,
              body_str: str, timestamp: int, nonce: str) -> str:
        canonical = f'{path}?{query}' if query else path
        sts = f'{method.upper()}\n{canonical}\n{timestamp}\n{nonce}\n{body_str}\n'
        digest = hmac.new(
            self.secret_key.encode('utf-8'),
            sts.encode('utf-8'),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode('ascii')

    def _authorization(self, method: str, path: str,
                       query: str = '', body_str: str = '') -> str:
        ts    = int(time.time())
        nonce = self._nonce()
        sig   = self._sign(method, path, query, body_str, ts, nonce)
        return (
            f'JOP appid="{self.app_id}",'
            f'accesskey="{self.access_key}",'
            f'timestamp="{ts}",'
            f'nonce="{nonce}",'
            f'signature="{sig}"'
        )

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _request(self, method: str, path: str,
                 params: Optional[dict] = None,
                 body: Optional[dict] = None,
                 timeout: int = 15) -> dict:
        query    = ''
        url      = self.BASE_URL + path
        if params:
            query = urllib.parse.urlencode(params)
            url   = f'{url}?{query}'

        body_str = json.dumps(body, ensure_ascii=False) if body else ''
        data     = body_str.encode('utf-8') if body_str else None

        headers = {
            'Authorization': self._authorization(method, path, query, body_str),
            'Content-Type':  'application/json',
            'Accept':        'application/json',
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            body_err = e.read().decode('utf-8', errors='replace')
            raise JLCAPIError(f'HTTP {e.code} {e.reason} — {body_err[:400]}') from e
        except urllib.error.URLError as e:
            raise JLCAPIError(f'Connection error: {e.reason}') from e
        except Exception as e:
            raise JLCAPIError(f'Request failed: {e}') from e

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            raise JLCAPIError(f'Invalid JSON response: {raw[:200]}')

        if isinstance(result, dict):
            code = result.get('code', 0)
            if code not in (0, 200, '0', '200'):
                msg = result.get('message') or result.get('msg') or str(result)
                raise JLCAPIError(f'API error {code}: {msg}')
            return result.get('data', result)

        return result

    def _upload_multipart(self, path: str, file_data: bytes,
                          file_name: str, timeout: int = 120) -> dict:
        """
        POST multipart/form-data with a file field.
        Auth body_str = '' (empty) — JLCPCB docs: no canonical body for multipart.
        Returns the parsed response data dict.
        """
        boundary = 'JLCGerber' + secrets.token_hex(16)

        parts = []
        if file_name:
            parts.append(
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="fileName"\r\n'
                f'\r\n'
                f'{file_name}\r\n'
            )
        parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
            f'Content-Type: application/octet-stream\r\n'
            f'\r\n'
        )
        body_bytes = (
            ''.join(parts).encode('utf-8')
            + file_data
            + f'\r\n--{boundary}--\r\n'.encode('utf-8')
        )

        headers = {
            # Auth uses empty body_str for multipart (no canonical JSON body)
            'Authorization': self._authorization('POST', path, '', ''),
            'Content-Type':  f'multipart/form-data; boundary={boundary}',
            'Accept':        'application/json',
        }
        req = urllib.request.Request(
            self.BASE_URL + path, data=body_bytes, headers=headers, method='POST'
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            body_err = e.read().decode('utf-8', errors='replace')
            raise JLCAPIError(f'HTTP {e.code} {e.reason} — {body_err[:400]}') from e
        except urllib.error.URLError as e:
            raise JLCAPIError(f'Connection error: {e.reason}') from e
        except Exception as e:
            raise JLCAPIError(f'Upload failed: {e}') from e

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            raise JLCAPIError(f'Invalid JSON response: {raw[:200]}')

        if isinstance(result, dict):
            code = result.get('code', 0)
            if code not in (0, 200, '0', '200'):
                msg = result.get('message') or result.get('msg') or str(result)
                raise JLCAPIError(f'API error {code}: {msg}')
            return result.get('data', result)

        return result

    # ── Public methods ────────────────────────────────────────────────────────

    def test_connection(self) -> dict:
        """
        Test credentials using two probes:
        1. JPay balance endpoint — always authorized, verifies auth/credentials
        2. PCB config endpoint — verifies PCB API is authorized
        Returns {'ok': bool, 'message': str}
        """
        if not self.app_id:
            return {'ok': False, 'message': 'App ID не заповнено.'}
        if not self.access_key:
            return {'ok': False, 'message': 'Access Key не заповнено.'}
        if not self.secret_key:
            return {
                'ok': False,
                'message': (
                    '⚠️ Secret Key / Tokenization Key не заповнено.\n'
                    'Знайдіть його на Developer Portal → App Setting → Tokenization Key.\n'
                    'Натисни "Generate" якщо він ще не згенерований.'
                ),
            }

        lines = []

        # Probe 1: JPay balance — verifies credentials regardless of PCB auth
        try:
            raw = self._request('GET', self.EP_JPAY_BALANCE, timeout=12)
            jpay_status = raw.get('jpayAccountStatus', -1)
            balance     = raw.get('accountBalance')
            frozen      = raw.get('freezeAmount')
            customer    = raw.get('customerCode', '')
            if jpay_status == 1:
                balance_str = f'{balance:.2f} USD' if balance is not None else '0.00 USD'
                frozen_str  = f'{frozen:.2f}' if frozen else '0.00'
                lines.append(
                    f'✅ Авторизація працює — JPay активовано\n'
                    f'   Клієнт: {customer} | Баланс: {balance_str} | Заморожено: {frozen_str}'
                )
            else:
                lines.append(
                    f'✅ Авторизація працює — JPay не активовано\n'
                    f'   Клієнт: {customer} | Щоб побачити баланс: активуй JPay рахунок на сайті JLCPCB'
                )
            auth_ok = True
        except JLCAPIError as e:
            err = str(e)
            if '401' in err or '403' in err or 'Unauthorized' in err or 'sign' in err.lower():
                lines.append(f'❌ Помилка авторизації: {err[:200]}')
                lines.append('   → Перевір App ID, Access Key та Tokenization Key.')
                return {'ok': False, 'message': '\n'.join(lines)}
            lines.append(f'⚠️ JPay endpoint: {err[:150]}')
            auth_ok = False

        # Probe 2: PCB config — verifies PCB API is authorized
        try:
            self._request('GET', self.EP_PCB_CONFIG, timeout=12)
            lines.append('✅ PCB API авторизовано — синхронізація доступна!')
            return {'ok': True, 'message': '\n'.join(lines)}
        except JLCAPIError as e:
            err = str(e)
            if '403' in err:
                # 403 = credentials OK, but PCB API not yet approved (still Reviewing)
                lines.append('⏳ PCB API ще на розгляді (403 — недостатньо прав).')
                lines.append('   Зайди на Developer Portal → Permission Setting →')
                lines.append('   зачекай поки статус PCB зміниться з "Reviewing" на "Active".')
                lines.append('')
                lines.append('Ключі правильні. Як тільки JLCPCB схвалить — все запрацює.')
                return {'ok': True, 'message': '\n'.join(lines)}
            elif '404' in err:
                lines.append('⚠️ PCB API — endpoint не знайдено (404).')
                lines.append('   Зайди на Developer Portal → Requestable APIs →')
                lines.append('   подай заявки на PCB APIs → зачекай підтвердження.')
                if auth_ok:
                    lines.append('Ключі правильні — щойно PCB API схвалять, все запрацює.')
                return {'ok': False, 'message': '\n'.join(lines)}
            elif '401' in err:
                lines.append(f'❌ Помилка авторизації PCB (401): {err[:150]}')
                lines.append('   Перевір App ID, Access Key та Tokenization Key.')
                return {'ok': False, 'message': '\n'.join(lines)}
            else:
                lines.append(f'⚠️ PCB API: {err[:150]}')
                return {'ok': auth_ok, 'message': '\n'.join(lines)}

    def get_pcb_order(self, batch_number: str) -> dict:
        """
        Get PCB order detail by batch number.
        Batch number format: W2025040800001 (visible in JLCPCB Order History).
        Returns the raw data dict (contains orderItem list with pcbItem nested inside).
        Use extract_pcb_item(raw) to get the PCB-specific fields.
        """
        return self._request('POST', self.EP_PCB_DETAIL,
                              body={'batchNum': batch_number})

    def get_pcb_batch_list(self, date_from: str, date_to: str,
                           page: int = 1, page_size: int = 50,
                           order_type: str = 'order_pcb') -> dict:
        """
        Paginated list of PCB batch numbers in a date range.
        date_from / date_to: 'yyyy-MM-dd HH:mm:ss'
        order_type: 'order_pcb' (PCB orders) or 'order_steel' (stencil orders) — per API docs.
        Returns raw data dict with 'list', 'total', 'pages'.
        """
        return self._request('POST', self.EP_PCB_BATCH_LIST, body={
            'pageNum':         page,
            'pageSize':        min(page_size, 50),
            'orderType':       order_type,
            'createTimeStart': date_from,
            'createTimeEnd':   date_to,
        })

    def get_all_pcb_batch_numbers(self, date_from: str, date_to: str) -> list:
        """
        Fetch all batch numbers (PCB + stencil) in the given date range.
        Returns list of (batchNum, orderId_or_None) tuples so callers can
        capture orderId for WIP queries without a separate API call.
        Paginates using the 'pages' field from the API response.
        """
        seen  = set()
        items_out = []

        for otype in ('order_pcb', 'order_steel'):
            page = 1
            while True:
                try:
                    result = self.get_pcb_batch_list(date_from, date_to,
                                                     page=page, order_type=otype)
                except JLCAPIError as e:
                    logger.warning('pageBatchInfoByOrderType orderType=%s page=%s: %s',
                                   otype, page, e)
                    break
                if not isinstance(result, dict):
                    logger.warning('pageBatchInfoByOrderType unexpected result type=%s',
                                   type(result).__name__)
                    break
                items       = result.get('list') or []
                total_pages = result.get('pages') or 1
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    n = item.get('batchNum')
                    if n and n not in seen:
                        seen.add(n)
                        # orderId (UUID) may be present in list items — capture for WIP
                        oid = item.get('orderId') or item.get('orderUUID') or None
                        items_out.append((n, oid))
                if not items or page >= total_pages:
                    break
                page += 1

        return items_out

    def get_pcb_wip(self, order_uuid: str) -> tuple:
        """
        Get PCB production progress (WIP stages) by orderUUID.
        Returns (stages_list, raw_data) where raw_data is the full API 'data' field.
        stages_list may be empty if API returned null/empty/unknown structure.
        """
        import logging as _log
        _logger = _log.getLogger(__name__)
        raw_data = self._request('POST', self.EP_PCB_WIP,
                                 body={'orderUUID': order_uuid})
        _logger.info('WIP raw_data type=%s value=%s', type(raw_data).__name__,
                     str(raw_data)[:500])

        stages = []
        if isinstance(raw_data, list):
            stages = raw_data
        elif isinstance(raw_data, dict):
            # Try common wrapper field names
            for key in ('processList', 'wipList', 'list', 'stages',
                        'wip', 'data', 'items', 'records'):
                v = raw_data.get(key)
                if isinstance(v, list):
                    stages = v
                    break

        return stages, raw_data

    def get_order_files(self, batch_num: str) -> list:
        """
        Fetch fresh order detail and extract all downloadable file URLs.
        Returns list of dicts: [{name, url, field}] sorted by priority.
        Pre-signed URLs from JLCPCB expire in ~1-4h after the API call.
        """
        # Human-readable labels for known pcbItem / orderItem URL fields
        _LABELS = {
            'orderFileUrl':      'Gerber ZIP',
            'bomFileUrl':        'BOM файл',
            'cplFileUrl':        'CPL / Pick & Place',
            'smtFileUrl':        'SMT файл',
            'sldFileUrl':        'SLD файл',
            'gerberFileUrl':     'Gerber ZIP',
            'pcbFileUrl':        'PCB файл',
            'productionFileUrl': 'Виробничий файл',
            'assemblyFileUrl':   'Складальний файл',
            'configFileUrl':     'Конфігурація',
            'reportFileUrl':     'Звіт',
            'testFileUrl':       'Тест-файл',
        }
        raw = self.get_pcb_order(batch_num)
        seen_urls: set = set()
        files: list = []

        def _add(field: str, url, source_label: str = '') -> None:
            if not isinstance(url, str) or not url.startswith('http'):
                return
            if url in seen_urls:
                return
            seen_urls.add(url)
            label = _LABELS.get(field) or field
            if source_label:
                label = f'{label} ({source_label})'
            files.append({'name': label, 'url': url, 'field': field})

        # Scan orderItem → pcbItem and top-level fields
        for item in (raw.get('orderItem') or []):
            order_type = item.get('orderType', '')
            src = f'тип {order_type}' if order_type != '' else ''
            pcb = item.get('pcbItem') or {}
            # Scan all *Url fields in pcbItem
            for k, v in pcb.items():
                if k.endswith(('Url', 'URL', 'url')) and isinstance(v, str) and v.startswith('http'):
                    _add(k, v, src)
            # Also scan top-level item fields
            for k, v in item.items():
                if k.endswith(('Url', 'URL', 'url')) and isinstance(v, str) and v.startswith('http'):
                    _add(k, v, src)

        # Scan root-level of raw response too
        for k, v in raw.items():
            if k.endswith(('Url', 'URL', 'url')) and isinstance(v, str) and v.startswith('http'):
                _add(k, v)

        return files

    # ── Gerber / ordering workflow ────────────────────────────────────────────

    def upload_gerber(self, file_path: str, file_name: Optional[str] = None) -> str:
        """
        Upload a Gerber .zip or .rar file.
        Returns fileKey string (links upload → quotation → order).

        Error codes:
          2001 — file verification error (invalid Gerber content)
          2002 — file size exceeds limit
        """
        import os
        if not file_name:
            file_name = os.path.basename(file_path)
        with open(file_path, 'rb') as fh:
            file_data = fh.read()
        result = self._upload_multipart(
            self.EP_PCB_UPLOAD_GERBER, file_data, file_name
        )
        if isinstance(result, str):
            return result
        # data may be the fileKey string directly or a dict with a key field
        if isinstance(result, dict):
            key = result.get('fileKey') or result.get('key') or result.get('data')
            if key:
                return key
        raise JLCAPIError(f'uploadGerber: unexpected response shape: {result!r}')

    def get_pcb_audit(self, file_key: str) -> dict:
        """
        Get Gerber pre-review (DRC check) result.
        fileKey is returned by upload_gerber().
        Response contains board dimensions, layer count, DRC pass/fail.
        """
        return self._request('POST', self.EP_PCB_AUDIT, body={'key': file_key})

    def calculate_quote(self, file_key: str, pcb_param: dict,
                        achieve_date: int = 120,
                        country: str = 'DE',
                        post_code: str = '',
                        shipping_method: Optional[str] = None,
                        order_type: int = 1) -> dict:
        """
        Get price quotation for a Gerber upload.

        pcb_param fields (PcbOrderCraftData):
          layer, width, length, qty, thickness,
          pcbColor (0=green,1=red,2=yellow,3=blue,4=white,5=black,6=purple),
          surfaceFinish (0=HASL lead,1=HASL leadfree,2=ENIG),
          copperWeight, goldFinger, panelFlag, flyingProbeTest,
          impedanceFlag, plateType, viaCovering, ...

        achieve_date: build time in hours (e.g. 24, 48, 120)
        country: ISO-2 ship-to country (affects freight options)

        Returns dict with:
          priceWithoutFreight, orderTotalWeight,
          pcbCostInfo: {totalFee, projectFee, spellFee, testsFee, ...},
          shipList:    [{shippingMethod, freightCost, deliveryDays}, ...],
          achieveDateList: [{achieveDate, fee}, ...],
          gerberTop:   image URL (PNG preview — top copper layer),
          gerberBottom: image URL (PNG preview — bottom copper layer)
        """
        body: dict = {
            'orderType':   order_type,
            'fileKey':     file_key,
            'achieveDate': achieve_date,
            'country':     country,
            'pcbParam':    pcb_param,
        }
        if post_code:
            body['postCode'] = post_code
        if shipping_method:
            body['shippingMethod'] = shipping_method
        return self._request('POST', self.EP_PCB_CALCULATE, body=body, timeout=30)

    def create_pcb_order(self, file_key: str, pcb_param: dict,
                         shipping_address: dict, shipping_method: str,
                         achieve_date: Optional[int] = None,
                         order_type: int = 1,
                         tax_vat_number: str = '',
                         batch_num: Optional[str] = None) -> dict:
        """
        Place a PCB order.
        WARNING: This charges the JLCPCB account and creates a real production order.

        shipping_address (OrderAddressData):
          firstName, lastName, companyName, streetAddress, addressLine2,
          city, country, province, postalCode, cellOrMobileNumber

        Returns: {orderId, orderType, orderDate, batchNum}

        Error codes:
          2500 — fileKey not found (re-upload required)
          2501 — no audit result yet (call get_pcb_audit first)
          2000–5006 — various validation / payment errors
        """
        body: dict = {
            'orderType':          order_type,
            'fileKey':            file_key,
            'pcbParam':           pcb_param,
            'shippingAddress':    shipping_address,
            'shippingMethod':     shipping_method,
            'billingAddressFlag': 0,   # use shipping address as billing
            'taxOrVATNumber':     tax_vat_number,
        }
        if achieve_date is not None:
            body['achieveDate'] = achieve_date
        if batch_num is not None:
            body['batchNum'] = batch_num
        return self._request('POST', self.EP_PCB_CREATE, body=body, timeout=30)

    def get_plate_brands(self) -> list:
        """Return available plate brands and Tg values (for plateType + Tg selection)."""
        result = self._request('GET', self.EP_PCB_PLATE_BRANDS)
        if isinstance(result, list):
            return result
        return result.get('list') or []

    def get_impedance_templates(self, layer: int = 2) -> list:
        """Return impedance template settings for a given layer count."""
        result = self._request('GET', self.EP_PCB_IMPEDANCE,
                               params={'layer': layer})
        if isinstance(result, list):
            return result
        return result.get('list') or []

    def refresh_order(self, jlc_order) -> dict:
        """
        Fetch latest data for a JLCOrder. Returns raw data dict.
        Caller should use extract_pcb_item(raw) to get orderStatus (int) etc.
        """
        batch = jlc_order.jlc_order_number or jlc_order.jlc_order_id
        if not batch:
            raise JLCAPIError('Batch number (jlc_order_number) not set on this order.')
        return self.get_pcb_order(batch)

    @classmethod
    def from_config(cls) -> 'JLCAPIClient':
        """Instantiate from saved JLCConfig."""
        from jlcpcb.models import JLCConfig
        cfg = JLCConfig.get()
        return cls(
            app_id=cfg.app_id,
            access_key=cfg.access_key,
            secret_key=cfg.secret_key,
        )


# ── urllib.parse needed for URL quoting ──────────────────────────────────────
import urllib.parse  # noqa: E402 (imported here to keep at top of section)
