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

    product = {
        "partNumber":       listing.get_supplier_sku(),
        "categoryId":       listing.dk_category_id,
        "description":      listing.dk_description,
        "manufacturer":     listing.dk_manufacturer or "",
        "imageUrl":         listing.dk_image_url or "",
        "additionalFields": additional_fields,
    }
    if config.marketplace_supplier_id:
        product["supplierId"] = config.marketplace_supplier_id
    if listing.dk_product_id:
        product["existingProductId"] = listing.dk_product_id

    payload = {"product": product}

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

def publish_listing(listing) -> None:
    """Full publish: stage product → create/update offer → save IDs & status."""
    from django.utils import timezone
    from bots.models import DigiKeyConfig

    config = DigiKeyConfig.get()
    if not config.marketplace_supplier_id:
        raise DKMarketplaceError(
            "Marketplace Supplier UUID не вказано в DigiKey → Конфігурація"
        )
    if not listing.dk_category_id:
        raise DKMarketplaceError("DK Category ID не заповнено")
    if not listing.dk_title:
        raise DKMarketplaceError("Назва (DK Title) не заповнена")
    if not listing.dk_description:
        raise DKMarketplaceError("Опис (DK Description) не заповнений")

    try:
        product_id = upsert_product(config, listing)
        listing.dk_product_id = product_id

        if listing.dk_offer_id:
            update_offer(config, listing)
        else:
            offer_id = create_offer(config, listing)
            listing.dk_offer_id = offer_id

        listing.sync_status    = DigiKeyListing.SYNC_PUBLISHED
        listing.last_synced_at = timezone.now()
        listing.last_error     = ''
        listing.save(update_fields=[
            'dk_product_id', 'dk_offer_id',
            'sync_status', 'last_synced_at', 'last_error',
        ])
    except Exception as exc:
        listing.sync_status = DigiKeyListing.SYNC_ERROR
        listing.last_error  = str(exc)
        listing.save(update_fields=['sync_status', 'last_error'])
        raise


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
    """Decode marketplace JWT token to extract supplier UUID from claims.
    Returns dict with all token claims for inspection."""
    import base64
    import json as _json

    token = get_marketplace_token(config)

    # JWT = header.payload.signature — decode middle part (base64url, no verify)
    try:
        parts = token.split('.')
        if len(parts) < 2:
            return {'raw_token_preview': token[:40] + '...', 'error': 'Not a JWT'}
        padding = parts[1] + '=' * (4 - len(parts[1]) % 4)
        claims = _json.loads(base64.urlsafe_b64decode(padding).decode('utf-8', errors='replace'))
        return claims
    except Exception as e:
        return {'error': str(e), 'raw_token_preview': token[:60] + '...'}


# Import model reference after definition to avoid circular import
from bots.models import DigiKeyListing  # noqa: E402
