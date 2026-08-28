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
      EP_PCB_WIP     POST /overseas/openapi/pcb/wip/get       {"batchNum":"..."}
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
                           order_type: int = None) -> dict:
        """
        Paginated list of PCB batch numbers in a date range.
        date_from / date_to: 'yyyy-MM-dd HH:mm:ss'
        order_type: integer — 0=batch PCB, 1=prototype/sample PCB, 3=Stencil.
            Pass None to omit the filter (fetch all types if API allows).
        Returns the raw data dict with 'list' of {batchNum, orderTypeInfos}.
        """
        body = {
            'pageNum':         page,
            'pageSize':        min(page_size, 50),
            'createTimeStart': date_from,
            'createTimeEnd':   date_to,
        }
        if order_type is not None:
            body['orderType'] = order_type
        return self._request('POST', self.EP_PCB_BATCH_LIST, body=body)

    def _fetch_batch_page(self, date_from: str, date_to: str,
                          page: int, order_type: int = None) -> tuple:
        """Returns (batch_nums_page: list, total: int)."""
        result = self.get_pcb_batch_list(date_from, date_to,
                                          page=page, page_size=50,
                                          order_type=order_type)
        items = result.get('list') or result.get('records') or []
        total = result.get('total') or result.get('totalCount') or 0
        nums  = [item['batchNum'] for item in items if item.get('batchNum')]
        return nums, int(total)

    def get_all_pcb_batch_numbers(self, date_from: str, date_to: str) -> list:
        """
        Fetch all PCB batch numbers in the given date range (auto-paginate).
        Tries each PCB order type (1=prototype, 0=batch, 3=stencil) separately
        because the JLCPCB API filters by orderType (integer, not string).
        Falls back to a no-filter call if typed calls fail.
        """
        seen       = set()
        batch_nums = []

        # JLCPCB order types: 1=prototype/sample, 0=batch PCB, 3=stencil
        for otype in (1, 0, 3):
            page = 1
            while True:
                try:
                    nums, total = self._fetch_batch_page(date_from, date_to,
                                                         page, order_type=otype)
                except JLCAPIError as e:
                    logger.warning('pageBatchInfoByOrderType orderType=%s page=%s: %s', otype, page, e)
                    break
                for n in nums:
                    if n not in seen:
                        seen.add(n)
                        batch_nums.append(n)
                if not nums or len(seen) >= total:
                    break
                page += 1

        # Fallback: try without orderType filter (API may not require it)
        if not batch_nums:
            logger.info('Typed batch-list returned nothing — retrying without orderType filter')
            page = 1
            while True:
                try:
                    nums, total = self._fetch_batch_page(date_from, date_to, page)
                except JLCAPIError as e:
                    logger.warning('pageBatchInfoByOrderType (no filter) page=%s: %s', page, e)
                    break
                for n in nums:
                    if n not in seen:
                        seen.add(n)
                        batch_nums.append(n)
                if not nums or len(seen) >= total:
                    break
                page += 1

        return batch_nums

    def get_pcb_wip(self, order_uuid: str) -> dict:
        """Get PCB production progress (WIP stages) by orderUUID (from order detail)."""
        return self._request('POST', self.EP_PCB_WIP,
                              body={'orderUUID': order_uuid})

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
