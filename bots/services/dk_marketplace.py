"""
bots/services/dk_marketplace.py — DigiKey Marketplace Products & Offers API

Products API:  POST /Sales/Marketplace2/Products/v1/products/stage/upsert
Offers API:    POST /Sales/Marketplace2/Offers/v1/offers
               PUT  /Sales/Marketplace2/Offers/v1/offers/{offerId}

Auth: OAuth 2.0 3-legged (marketplace_access_token у DigiKeyConfig).
      Refresh: POST /v1/oauth2/token  grant_type=refresh_token
"""
import logging
from datetime import timedelta

logger = logging.getLogger(__name__)

_PROD_BASE    = "https://api.digikey.com"
_SANDBOX_BASE = "https://sandbox-api.digikey.com"

_PRODUCTS_BASE = "/Sales/Marketplace2/Products/v1"
_OFFERS_BASE   = "/Sales/Marketplace2/Offers/v1"
_TOKEN_PATH    = "/v1/oauth2/token"


class DKMarketplaceError(Exception):
    pass


# ── Token ────────────────────────────────────────────────────────────────────

def _base_url(config) -> str:
    return _SANDBOX_BASE if config.use_sandbox else _PROD_BASE


def _refresh_marketplace_token(config) -> str:
    """Refresh 3-legged OAuth token using stored refresh_token."""
    import requests as req
    from django.utils import timezone
    from bots.models import DigiKeyConfig

    resp = req.post(
        f"{_base_url(config)}{_TOKEN_PATH}",
        data={
            "client_id":     config.client_id,
            "client_secret": config.client_secret,
            "grant_type":    "refresh_token",
            "refresh_token": config.marketplace_refresh_token,
        },
        timeout=15,
    )
    try:
        resp.raise_for_status()
    except req.HTTPError as e:
        body = {}
        try: body = e.response.json()
        except Exception: pass
        raise DKMarketplaceError(
            f"Token refresh error {e.response.status_code}: {body.get('error_description') or body}"
        ) from e

    data = resp.json()
    token      = data["access_token"]
    refresh    = data.get("refresh_token", config.marketplace_refresh_token)
    expires_in = int(data.get("expires_in", 3600))

    DigiKeyConfig.objects.filter(pk=1).update(
        marketplace_access_token=token,
        marketplace_refresh_token=refresh,
        marketplace_token_expires_at=timezone.now() + timedelta(seconds=expires_in - 60),
    )
    config.marketplace_access_token      = token
    config.marketplace_refresh_token     = refresh
    config.marketplace_token_expires_at  = timezone.now() + timedelta(seconds=expires_in - 60)
    return token


def get_marketplace_token(config) -> str:
    """Return valid marketplace access token (refresh if needed)."""
    from django.utils import timezone

    if (config.marketplace_access_token
            and config.marketplace_token_expires_at
            and config.marketplace_token_expires_at > timezone.now() + timedelta(seconds=60)):
        return config.marketplace_access_token

    if config.marketplace_refresh_token:
        return _refresh_marketplace_token(config)

    raise DKMarketplaceError(
        "Marketplace OAuth токен відсутній. "
        "Виконайте OAuth авторизацію в налаштуваннях DigiKey → Marketplace OAuth."
    )


def _headers(config, token: str) -> dict:
    return {
        "Authorization":       f"Bearer {token}",
        "X-DIGIKEY-Client-Id": config.client_id,
        "Content-Type":        "application/json",
        "Accept":              "application/json",
    }


# ── Products API ─────────────────────────────────────────────────────────────

def upsert_product(config, listing) -> str:
    """Stage or update a product. Returns DK Product UUID (_id)."""
    import requests as req

    token = get_marketplace_token(config)
    url   = f"{_base_url(config)}{_PRODUCTS_BASE}/products/stage/upsert"

    # Build additionalFields based on category
    if listing.category_type == 'filter':
        additional_fields = listing.get_filter_attributes_api()
    else:
        additional_fields = []

    if not config.marketplace_supplier_id:
        raise DKMarketplaceError(
            "Marketplace Vendor UUID не вказано. "
            "Натисни 🪪 Отримати Supplier UUID щоб знайти його автоматично, "
            "або зайди supplier.digikey.com → Account → Company Profile."
        )

    payload = {
        "supplierId":       config.marketplace_supplier_id,
        "partNumber":       listing.get_supplier_sku(),
        "categoryId":       listing.dk_category_id,
        "description":      listing.dk_description,
        "manufacturer":     listing.dk_manufacturer or "",
        "imageUrl":         listing.dk_image_url or "",
        "additionalFields": additional_fields,
    }
    if listing.dk_product_id:
        payload["existingProductId"] = listing.dk_product_id

    logger.info("DK upsert_product SKU=%s payload=%s", listing.get_supplier_sku(), payload)
    resp = req.post(url, json=payload, headers=_headers(config, token), timeout=30)
    try:
        resp.raise_for_status()
    except req.HTTPError as e:
        body = {}
        try: body = e.response.json()
        except Exception: pass
        raise DKMarketplaceError(
            f"upsert_product {e.response.status_code}: {body}"
        ) from e

    data = resp.json()
    product_id = data.get("_id") or data.get("id") or ""
    logger.info("DK upsert_product OK product_id=%s", product_id)
    return product_id


# ── Offers API ───────────────────────────────────────────────────────────────

def _offer_payload(listing) -> dict:
    stock  = listing.get_stock_qty()
    prices = listing.get_prices_api()
    if not prices:
        raise DKMarketplaceError("Цінові тири не заповнені (dk_prices порожній)")
    return {
        "supplierSku":       listing.get_supplier_sku(),
        "title":             listing.dk_title.strip(),
        "description":       listing.dk_description[:500],
        "isActive":          listing.dk_is_active,
        "isAvailable":       True,
        "prices":            prices,
        "quantityAvailable": stock,
        "minOrderQuantity":  max(1, listing.dk_min_order_qty),
        "leadTimeToShip":    listing.dk_lead_time_days,
        "minQuantityAlert":  listing.dk_qty_alert,
    }


def create_offer(config, listing) -> str:
    """POST /offers — create offer. Returns DK Offer UUID."""
    import requests as req

    token = get_marketplace_token(config)
    url   = f"{_base_url(config)}{_OFFERS_BASE}/offers"

    payload = _offer_payload(listing)
    payload["supplierId"] = config.marketplace_supplier_id
    payload["productId"]  = listing.dk_product_id

    logger.info("DK create_offer SKU=%s", listing.get_supplier_sku())
    resp = req.post(url, json=payload, headers=_headers(config, token), timeout=30)
    try:
        resp.raise_for_status()
    except req.HTTPError as e:
        body = {}
        try: body = e.response.json()
        except Exception: pass
        raise DKMarketplaceError(
            f"create_offer {e.response.status_code}: {body}"
        ) from e

    data     = resp.json()
    offer_id = data.get("id") or ""
    logger.info("DK create_offer OK offer_id=%s", offer_id)
    return offer_id


def update_offer(config, listing) -> None:
    """PUT /offers/{offerId} — update existing offer (incl. qty refresh)."""
    import requests as req

    if not listing.dk_offer_id:
        raise DKMarketplaceError("dk_offer_id порожній — неможливо оновити пропозицію")

    token = get_marketplace_token(config)
    url   = f"{_base_url(config)}{_OFFERS_BASE}/offers/{listing.dk_offer_id}"

    logger.info("DK update_offer offer_id=%s", listing.dk_offer_id)
    resp = req.put(url, json=_offer_payload(listing), headers=_headers(config, token), timeout=30)
    try:
        resp.raise_for_status()
    except req.HTTPError as e:
        body = {}
        try: body = e.response.json()
        except Exception: pass
        raise DKMarketplaceError(
            f"update_offer {e.response.status_code}: {body}"
        ) from e
    logger.info("DK update_offer OK")


# ── High-level actions ────────────────────────────────────────────────────────

_SYNC_STAGED = 'staged'


def publish_listing(listing) -> str:
    """Stage product in DigiKey PIM. Returns 'staged' or 'published'.

    DigiKey workflow:
      1. upsert_product → product enters PIM review queue (staged)
      2. After DigiKey approves the product, call create_offer_for_listing()
    """
    from django.utils import timezone
    from bots.models import DigiKeyConfig

    config = DigiKeyConfig.get()
    if not listing.dk_category_id:
        raise DKMarketplaceError("DK Category ID не заповнено")
    if not listing.dk_title:
        raise DKMarketplaceError("Назва (DK Title) не заповнена")
    if not listing.dk_description:
        raise DKMarketplaceError("Опис (DK Description) не заповнений")

    try:
        product_id = upsert_product(config, listing)
        listing.dk_product_id = product_id

        # Try offer immediately — works if product already approved
        if listing.dk_offer_id:
            try:
                update_offer(config, listing)
                status = DigiKeyListing.SYNC_PUBLISHED
            except DKMarketplaceError:
                status = _SYNC_STAGED
        else:
            try:
                offer_id = create_offer(config, listing)
                listing.dk_offer_id = offer_id
                status = DigiKeyListing.SYNC_PUBLISHED
            except DKMarketplaceError:
                # Product staged but not yet approved — offer will be created later
                status = _SYNC_STAGED

        listing.sync_status    = status
        listing.last_synced_at = timezone.now()
        listing.last_error     = '' if status == DigiKeyListing.SYNC_PUBLISHED else (
            'Продукт в черзі на перевірку DigiKey. '
            'Після затвердження натисніть 📦 Створити Offer.'
        )
        listing.save(update_fields=[
            'dk_product_id', 'dk_offer_id',
            'sync_status', 'last_synced_at', 'last_error',
        ])
        return status
    except Exception as exc:
        listing.sync_status = DigiKeyListing.SYNC_ERROR
        listing.last_error  = str(exc)
        listing.save(update_fields=['sync_status', 'last_error'])
        raise


def create_offer_for_listing(listing) -> None:
    """Create or update offer for an already-staged/approved product."""
    from django.utils import timezone
    from bots.models import DigiKeyConfig

    config = DigiKeyConfig.get()
    if not listing.dk_product_id:
        raise DKMarketplaceError("Product ID відсутній. Спочатку опублікуй продукт.")

    if listing.dk_offer_id:
        update_offer(config, listing)
    else:
        offer_id = create_offer(config, listing)
        listing.dk_offer_id = offer_id

    listing.sync_status    = DigiKeyListing.SYNC_PUBLISHED
    listing.last_synced_at = timezone.now()
    listing.last_error     = ''
    listing.save(update_fields=['dk_offer_id', 'sync_status', 'last_synced_at', 'last_error'])


def sync_quantity(listing) -> None:
    """Update only quantityAvailable (and price) on existing offer."""
    from django.utils import timezone
    from bots.models import DigiKeyConfig

    config = DigiKeyConfig.get()
    update_offer(config, listing)
    listing.last_synced_at = timezone.now()
    listing.last_error     = ''
    listing.save(update_fields=['last_synced_at', 'last_error'])


def fetch_supplier_uuid(config) -> dict:
    """Find supplier UUID via GET /offers (supplierId in each offer belongs to the token owner).
    Falls back to GET /products if no offers found.
    Returns dict {uuid: name}."""
    import requests as req

    token  = get_marketplace_token(config)
    uuids  = {}

    # ── Try GET /offers first (supplierId is always the authenticated supplier) ──
    resp = req.get(
        f"{_base_url(config)}{_OFFERS_BASE}/offers",
        params={'Max': 5},
        headers=_headers(config, token),
        timeout=15,
    )
    if resp.status_code == 200:
        for offer in resp.json().get('offers', []):
            uid  = offer.get('supplierId', '')
            name = offer.get('supplierName', '')
            if uid:
                uuids[uid] = name

    if uuids:
        return uuids

    # ── Fallback: GET /products (no filter = all products visible to token) ────
    resp2 = req.get(
        f"{_base_url(config)}{_PRODUCTS_BASE}/products",
        params={'Max': 10},
        headers=_headers(config, token),
        timeout=15,
    )
    if resp2.status_code == 200:
        for p in resp2.json().get('products', []):
            for sup in p.get('authorizedSuppliersList', []):
                uid  = sup.get('id', '')
                name = sup.get('name', '')
                if uid:
                    uuids[uid] = name

    if uuids:
        return uuids

    raise DKMarketplaceError(
        "Не знайдено жодного offer/product для поточного Marketplace токена. "
        "UUID знаходиться в supplier.digikey.com → Account → Company Information "
        "(поле Supplier GUID / API Supplier ID)."
    )


def fetch_offers(config, max_count: int = 500) -> list:
    """GET /offers — paginated, returns list of Offer dicts for authenticated supplier."""
    import requests as req

    token = get_marketplace_token(config)
    url   = f"{_base_url(config)}{_OFFERS_BASE}/offers"
    all_offers: list = []
    offset = 0

    while True:
        resp = req.get(url, params={'Max': 50, 'Offset': offset},
                       headers=_headers(config, token), timeout=15)
        try:
            resp.raise_for_status()
        except req.HTTPError as e:
            body = {}
            try: body = e.response.json()
            except Exception: pass
            raise DKMarketplaceError(f"fetch_offers {e.response.status_code}: {body}") from e

        data   = resp.json()
        offers = data.get('offers', [])
        all_offers.extend(offers)

        if len(offers) < 50 or len(all_offers) >= max_count:
            break
        offset += 50

    logger.info("DK fetch_offers total=%d", len(all_offers))
    return all_offers


def import_offers_from_dk() -> dict:
    """Pull all offers from DigiKey and sync to local DigiKeyListing records.

    Matching logic (by priority):
      1. listing.dk_supplier_sku == offer.supplierSku
      2. listing.product.sku     == offer.supplierSku

    Updates per matched listing:
      dk_offer_id, dk_product_id, sync_status=published, last_synced_at, last_error
      dk_prices  — overwritten from DigiKey if offer has prices
      dk_title   — filled from DigiKey only when blank in Minerva

    Returns dict: {updated: N, not_found: [sku, ...]}
    """
    import json
    from django.utils import timezone
    from bots.models import DigiKeyConfig, DigiKeyListing

    config = DigiKeyConfig.get()
    offers = fetch_offers(config)

    updated: int    = 0
    not_found: list = []

    for offer in offers:
        offer_id   = offer.get('id', '')
        product_id = offer.get('productId', '')
        sku        = offer.get('supplierSku', '')
        if not sku:
            continue

        listing = None
        # 1. match by dk_supplier_sku
        try:
            listing = DigiKeyListing.objects.select_related('product').get(dk_supplier_sku=sku)
        except DigiKeyListing.DoesNotExist:
            pass

        # 2. match by product.sku
        if listing is None:
            try:
                listing = DigiKeyListing.objects.select_related('product').get(product__sku=sku)
            except DigiKeyListing.DoesNotExist:
                not_found.append(sku)
                continue

        listing.dk_offer_id    = offer_id
        listing.dk_product_id  = product_id
        listing.sync_status    = DigiKeyListing.SYNC_PUBLISHED
        listing.last_synced_at = timezone.now()
        listing.last_error     = ''

        update_flds = ['dk_offer_id', 'dk_product_id', 'sync_status',
                       'last_synced_at', 'last_error']

        # Pull title only if blank
        if offer.get('title') and not listing.dk_title:
            listing.dk_title = offer['title'][:200]
            update_flds.append('dk_title')

        # Pull prices from DigiKey (overwrite)
        raw_prices = offer.get('prices', [])
        if raw_prices:
            listing.dk_prices = json.dumps([
                {'qty': p['quantityBreak'], 'price': float(p['price'])}
                for p in raw_prices
            ])
            update_flds.append('dk_prices')

        listing.save(update_fields=update_flds)
        updated += 1

    logger.info("DK import_offers updated=%d not_found=%d", updated, len(not_found))
    return {'updated': updated, 'not_found': not_found}


def create_listings_from_offers() -> dict:
    """Create DigiKeyListing records for DigiKey offers that have no listing in Minerva.

    For each unmatched offer:
      - Looks up Product by sku == offer.supplierSku
      - If found and no listing exists: creates DigiKeyListing with offer data pre-filled
      - sync_status = published (offer already live on DigiKey)

    Returns: {created: N, already_exists: M, no_product: [sku, ...]}
    """
    import json
    from django.db import models as _m
    from django.utils import timezone
    from bots.models import DigiKeyConfig, DigiKeyListing

    try:
        from inventory.models import Product
    except ImportError:
        raise DKMarketplaceError("inventory.Product модель не знайдена")

    config = DigiKeyConfig.get()
    offers = fetch_offers(config)

    created: int       = 0
    already_exists_cnt = 0
    no_product: list   = []

    for offer in offers:
        sku = offer.get('supplierSku', '')
        if not sku:
            continue

        # Skip if a listing already covers this offer (by dk_supplier_sku or product.sku)
        if DigiKeyListing.objects.filter(
            _m.Q(dk_supplier_sku=sku) | _m.Q(product__sku=sku)
        ).exists():
            already_exists_cnt += 1
            continue

        # Find product in inventory
        try:
            product = Product.objects.get(sku=sku)
        except Product.DoesNotExist:
            no_product.append(sku)
            continue
        except Product.MultipleObjectsReturned:
            no_product.append(f"{sku} (дублі SKU)")
            continue

        raw_prices = offer.get('prices', [])
        prices_json = json.dumps([
            {'qty': p['quantityBreak'], 'price': float(p['price'])}
            for p in raw_prices
        ]) if raw_prices else '[]'

        DigiKeyListing.objects.create(
            product          = product,
            dk_supplier_sku  = sku,
            dk_offer_id      = offer.get('id', ''),
            dk_product_id    = offer.get('productId', ''),
            dk_title         = (offer.get('title') or '')[:200],
            dk_description   = (offer.get('description') or '')[:500],
            dk_is_active     = bool(offer.get('isActive', True)),
            dk_min_order_qty = max(1, int(offer.get('minOrderQuantity') or 1)),
            dk_lead_time_days= int(offer.get('leadTimeToShip') or 0),
            dk_qty_alert     = int(offer.get('minQuantityAlert') or 0),
            dk_prices        = prices_json,
            sync_status      = DigiKeyListing.SYNC_PUBLISHED,
            last_synced_at   = timezone.now(),
        )
        created += 1

    logger.info("DK create_listings created=%d already=%d no_product=%d",
                created, already_exists_cnt, len(no_product))
    return {'created': created, 'already_exists': already_exists_cnt, 'no_product': no_product}


def fetch_custom_fields(config) -> list:
    """GET /custom/fields?Owner=Product — returns list of custom field defs with codes."""
    import requests as req
    token = get_marketplace_token(config)
    url   = f"{_base_url(config)}/Sales/Marketplace2/Custom/v1/custom/fields"
    all_fields = []
    offset = 0
    while True:
        resp = req.get(url, params={'Owner': 'Product', 'Max': 50, 'Offset': offset},
                       headers=_headers(config, token), timeout=15)
        try:
            resp.raise_for_status()
        except req.HTTPError as e:
            body = {}
            try: body = e.response.json()
            except Exception: pass
            raise DKMarketplaceError(f"fetch_custom_fields {e.response.status_code}: {body}") from e
        data = resp.json()
        fields = data.get('customFields', [])
        all_fields.extend(fields)
        if len(fields) < 50:
            break
        offset += 50
    return all_fields


# Import model reference after definition to avoid circular import
from bots.models import DigiKeyListing  # noqa: E402
