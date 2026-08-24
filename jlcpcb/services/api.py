"""
jlcpcb/services/api.py — JLCPCB API client (stub)

JLCPCB API requires developer portal approval:
  https://jlcpcb.com/api/

Once credentials are obtained, implement real HTTP calls below.
Current version: manual order entry + status updates via admin.
"""
import logging
from django.db.models import Q

logger = logging.getLogger(__name__)

_STATUS_PRIORITY = {
    'ordered':       0,
    'reviewed':      1,
    'in_production': 2,
    'manufactured':  3,
    'shipped':       4,
    'delivered':     5,
    'cancelled':     99,
}

JLC_STATUS_MAP = {
    # Map raw JLCPCB API statuses → local_status choices (extend as API is discovered)
    'Placed':         'ordered',
    'Reviewing':      'reviewed',
    'Confirmed':      'reviewed',
    'Manufacturing':  'in_production',
    'Produced':       'manufactured',
    'Shipped':        'shipped',
    'Delivered':      'delivered',
    'Cancelled':      'cancelled',
    'Refunded':       'cancelled',
}


class JLCAPIError(Exception):
    pass


def status_can_advance(current: str, new: str) -> bool:
    return _STATUS_PRIORITY.get(new, 0) > _STATUS_PRIORITY.get(current, 0)


def map_jlc_status(jlc_raw_status: str) -> str:
    """Convert raw JLCPCB API status string to local_status choice."""
    return JLC_STATUS_MAP.get(jlc_raw_status, 'ordered')


# ── Product matching ──────────────────────────────────────────────────────────

def find_product_for_jlc_name(jlc_name: str):
    """
    Try to match a JLC order description to a Product in the catalog.

    JLC Gerber names typically: AN120202-01H_2L_FR4_0.8x160x77.5mm_
    Product SKU on inventory:   AN120202-01H
    Strategy: prefix before first '_' = SKU candidate.

    Returns: (Product | None, match_type: str)
    match_type: 'mapping' | 'auto_prefix' | 'auto_startswith' | 'unmatched'
    """
    from inventory.models import Product
    from jlcpcb.models import JLCProductMapping

    if not jlc_name:
        return None, 'unmatched'

    # 1. Manual mapping table wins over everything
    mapping = JLCProductMapping.objects.filter(
        Q(jlc_reference__iexact=jlc_name) |
        Q(jlc_reference__iexact=jlc_name.split('_')[0])
    ).select_related('product').first()
    if mapping:
        return mapping.product, 'mapping'

    # 2. Prefix before first underscore → exact SKU match
    prefix = jlc_name.split('_')[0].strip()
    if prefix:
        product = Product.objects.filter(sku__iexact=prefix).first()
        if product:
            return product, 'auto_prefix'
        product = Product.objects.filter(sku_short__iexact=prefix).first()
        if product:
            return product, 'auto_prefix'

    # 3. Any active product whose SKU is a prefix of the full JLC name
    jlc_upper = jlc_name.upper()
    for p in Product.objects.filter(is_active=True).only('pk', 'sku'):
        if p.sku and jlc_upper.startswith(p.sku.upper()):
            return p, 'auto_startswith'

    return None, 'unmatched'


# ── Inventory receipt ─────────────────────────────────────────────────────────

def receive_into_inventory(jlc_order, location_code: str = None, performed_by=None):
    """
    Create InventoryTransaction(Incoming) for a delivered JLC order.
    Returns the created transaction or None if product not linked.
    """
    from inventory.models import InventoryTransaction, Location
    from jlcpcb.models import JLCConfig

    if not jlc_order.product_id:
        logger.warning('JLCOrder %s: cannot receive — no product linked', jlc_order.jlc_order_id)
        return None

    cfg = JLCConfig.get()
    loc_code = location_code or cfg.default_location or 'MAIN'
    location, _ = Location.objects.get_or_create(code=loc_code, defaults={'name': loc_code})

    ext_key = f'jlc-{jlc_order.jlc_order_id}-recv'
    if InventoryTransaction.objects.filter(external_key=ext_key).exists():
        logger.info('JLCOrder %s: receipt already exists, skipping', jlc_order.jlc_order_id)
        return None

    qty = jlc_order.quantity - float(jlc_order.received_qty)
    if qty <= 0:
        return None

    from decimal import Decimal
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
    logger.info('JLCOrder %s: received %s pcs to %s', jlc_order.jlc_order_id, qty, loc_code)
    return tx


# ── JLCPCB API stub (implement after obtaining API access) ────────────────────

class JLCAPIClient:
    """
    HTTP client for JLCPCB Orders API.
    Currently a stub — fill in once developer portal access is granted.
    """

    BASE_URL_PROD    = 'https://jlcpcb.com/api'
    BASE_URL_SANDBOX = 'https://jlcpcb.com/api/sandbox'

    def __init__(self, api_key: str, api_secret: str = '', use_sandbox: bool = True):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.base_url   = self.BASE_URL_SANDBOX if use_sandbox else self.BASE_URL_PROD

    def get_orders(self, page: int = 1, page_size: int = 50) -> list:
        """Fetch paginated list of orders from JLCPCB API."""
        raise JLCAPIError(
            'JLCPCB API access not yet configured. '
            'Apply at https://jlcpcb.com/api and fill in credentials in JLCConfig.'
        )

    def get_order(self, order_id: str) -> dict:
        """Fetch single order details."""
        raise JLCAPIError('JLCPCB API access not yet configured.')

    def get_tracking(self, order_id: str) -> dict:
        """Fetch shipping/tracking info for an order."""
        raise JLCAPIError('JLCPCB API access not yet configured.')
