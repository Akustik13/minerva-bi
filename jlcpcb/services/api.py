"""
jlcpcb/services/api.py — JLCPCB API client

Auth: HMAC-SHA256 signature
  Timestamp (ms) + AccessKey → HMAC-SHA256(SecretKey, AccessKey + Timestamp)
  Headers: X-Access-Key, X-Timestamp, X-Signature, Content-Type: application/json

Base URL (production): https://jlcpcb.com/api/v1
"""
import hashlib
import hmac
import json
import logging
import time
import urllib.error
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

# Raw JLCPCB API statuses → local_status
# Extend when you see new statuses in raw_data
JLC_STATUS_MAP = {
    'Placed':              'ordered',
    'Reviewing':           'reviewed',
    'Confirmed':           'reviewed',
    'Quotation Confirmed': 'reviewed',
    'In Production':       'in_production',
    'Manufacturing':       'in_production',
    'SMT':                 'in_production',
    'Produced':            'manufactured',
    'Quality Check':       'manufactured',
    'Shipped':             'shipped',
    'Partially Shipped':   'shipped',
    'Delivered':           'delivered',
    'Cancelled':           'cancelled',
    'Refunded':            'cancelled',
}


class JLCAPIError(Exception):
    pass


def status_can_advance(current: str, new: str) -> bool:
    return _STATUS_PRIORITY.get(new, 0) > _STATUS_PRIORITY.get(current, 0)


def map_jlc_status(jlc_raw: str) -> str:
    """Best-effort mapping; unknown statuses stay 'ordered'."""
    if not jlc_raw:
        return 'ordered'
    if jlc_raw in JLC_STATUS_MAP:
        return JLC_STATUS_MAP[jlc_raw]
    # case-insensitive fallback
    for k, v in JLC_STATUS_MAP.items():
        if k.lower() == jlc_raw.lower():
            return v
    logger.warning('Unknown JLCPCB status: %r — treating as ordered', jlc_raw)
    return 'ordered'


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

class JLCAPIClient:
    """
    JLCPCB Orders API client.

    Authentication: HMAC-SHA256
      timestamp_ms = str(int(time.time() * 1000))
      signature    = HMAC-SHA256(secret_key, access_key + timestamp_ms)
      Headers:
        X-Access-Key: <access_key>
        X-Timestamp:  <timestamp_ms>
        X-Signature:  <signature>
        Content-Type: application/json
    """

    BASE_URL_PROD    = 'https://jlcpcb.com/api/v1'
    BASE_URL_SANDBOX = 'https://jlcpcb.com/api/v1/sandbox'

    # Endpoint constants
    EP_ORDERS     = '/order/list'
    EP_ORDER      = '/order/detail'
    EP_TRACKING   = '/order/tracking'
    EP_PING       = '/ping'

    def __init__(self, access_key: str, secret_key: str, use_sandbox: bool = False):
        self.access_key = access_key
        self.secret_key = secret_key
        self.base_url   = self.BASE_URL_SANDBOX if use_sandbox else self.BASE_URL_PROD

    # ── Auth ─────────────────────────────────────────────────────────────────

    def _make_signature(self, timestamp_ms: str) -> str:
        msg = (self.access_key + timestamp_ms).encode('utf-8')
        key = self.secret_key.encode('utf-8')
        return hmac.new(key, msg, hashlib.sha256).hexdigest()

    def _headers(self) -> dict:
        ts = str(int(time.time() * 1000))
        return {
            'X-Access-Key': self.access_key,
            'X-Timestamp':  ts,
            'X-Signature':  self._make_signature(ts),
            'Content-Type': 'application/json',
            'Accept':       'application/json',
        }

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _request(self, method: str, path: str,
                 params: Optional[dict] = None,
                 body: Optional[dict] = None,
                 timeout: int = 15) -> dict:
        url = self.base_url + path
        if params:
            qs = '&'.join(f'{k}={urllib.parse.quote(str(v))}' for k, v in params.items())
            url = f'{url}?{qs}'

        data = json.dumps(body).encode() if body else None
        req  = urllib.request.Request(url, data=data, headers=self._headers(), method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            body_err = e.read().decode('utf-8', errors='replace')
            raise JLCAPIError(
                f'HTTP {e.code} {e.reason} — {body_err[:300]}'
            ) from e
        except urllib.error.URLError as e:
            raise JLCAPIError(f'Connection error: {e.reason}') from e
        except Exception as e:
            raise JLCAPIError(f'Request failed: {e}') from e

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            raise JLCAPIError(f'Invalid JSON response: {raw[:200]}')

        # JLCPCB API wraps data in {"code": 0, "data": {...}, "message": ""}
        if isinstance(result, dict):
            code = result.get('code', result.get('status', 0))
            if code not in (0, 200, '0', '200', 'success', None):
                msg = result.get('message') or result.get('msg') or str(result)
                raise JLCAPIError(f'API error {code}: {msg}')
            return result.get('data', result)

        return result

    # ── Public methods ────────────────────────────────────────────────────────

    def test_connection(self) -> dict:
        """
        Test credentials. Tries /ping, falls back to listing 1 order.
        Returns {'ok': True/False, 'message': str, 'raw': dict}
        """
        if not self.access_key or not self.secret_key:
            return {'ok': False, 'message': 'Access Key або Secret Key не заповнено.', 'raw': {}}

        # Try /ping first (lightweight)
        try:
            raw = self._request('GET', self.EP_PING, timeout=10)
            return {'ok': True, 'message': f'✅ З\'єднання успішне. Відповідь: {str(raw)[:120]}', 'raw': raw}
        except JLCAPIError as e:
            ping_err = str(e)
            logger.info('JLCPCB /ping failed (%s), trying /order/list', ping_err)

        # Fallback: list 1 order
        try:
            raw = self._request('GET', self.EP_ORDERS, params={'page': 1, 'pageSize': 1}, timeout=10)
            return {'ok': True, 'message': f'✅ З\'єднання успішне (через /order/list). Замовлень отримано.', 'raw': raw}
        except JLCAPIError as e:
            return {'ok': False, 'message': f'❌ Помилка: {e}', 'raw': {}}

    def get_orders(self, page: int = 1, page_size: int = 50) -> dict:
        """
        Fetch paginated order list.
        Returns raw API response dict (contains list + pagination).
        Expected structure: {'list': [...], 'total': N, 'page': 1, 'pageSize': 50}
        """
        return self._request('GET', self.EP_ORDERS, params={
            'page':     page,
            'pageSize': page_size,
        })

    def get_all_orders(self, max_pages: int = 20) -> list:
        """Fetch all orders across pages."""
        all_orders = []
        for p in range(1, max_pages + 1):
            result = self.get_orders(page=p, page_size=50)
            items = result if isinstance(result, list) else (
                result.get('list') or result.get('orders') or result.get('data') or []
            )
            if not items:
                break
            all_orders.extend(items)
            total   = result.get('total', 0) if isinstance(result, dict) else 0
            fetched = len(all_orders)
            if total and fetched >= total:
                break
        return all_orders

    def get_order(self, order_id: str) -> dict:
        """Fetch single order detail."""
        return self._request('GET', self.EP_ORDER, params={'orderId': order_id})

    def get_tracking(self, order_id: str) -> dict:
        """Fetch tracking info for an order."""
        return self._request('GET', self.EP_TRACKING, params={'orderId': order_id})

    @classmethod
    def from_config(cls) -> 'JLCAPIClient':
        """Instantiate from saved JLCConfig."""
        from jlcpcb.models import JLCConfig
        cfg = JLCConfig.get()
        return cls(
            access_key=cfg.access_key,
            secret_key=cfg.secret_key,
            use_sandbox=cfg.use_sandbox,
        )


# ── urllib.parse needed for URL quoting ──────────────────────────────────────
import urllib.parse  # noqa: E402 (imported here to keep at top of section)
