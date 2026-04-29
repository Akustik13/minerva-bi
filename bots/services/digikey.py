"""
bots/services/digikey.py — DigiKey Marketplace Orders API клієнт

OAuth2 2-legged (Client Credentials):
  POST https://api.digikey.com/v1/oauth2/token

Orders API:
  GET  https://api.digikey.com/orderstatus/v4/orders
  GET  https://api.digikey.com/orderstatus/v4/orders/{salesOrderId}

Документація: https://developer.digikey.com/products/order-status
"""
import logging
from datetime import datetime, timedelta, date

from django.utils import timezone

logger = logging.getLogger(__name__)

# ── Endpoints ─────────────────────────────────────────────────────────────────

_PROD_BASE     = "https://api.digikey.com"
_SANDBOX_BASE  = "https://sandbox-api.digikey.com"

TOKEN_PATH           = "/v1/oauth2/token"
AUTHORIZE_PATH       = "/v1/oauth2/authorize"
ORDERS_PATH          = "/orderstatus/v4/orders"
MARKETPLACE_PATH     = "/Sales/Marketplace2/Orders/v1/orders"
PO_SEARCH_PATH       = "/OrderManagement/v1/SalesOrders/Search/PoNumber"
KEYWORD_SEARCH_PATH  = "/products/v4/search/keyword"

# ── Status priority (higher = more advanced; never downgrade) ─────────────────

_STATUS_PRIORITY = {
    "received":   0,
    "processing": 1,
    "shipped":    2,
    "delivered":  3,
    "cancelled":  99,
}


def _status_can_advance(current: str, new: str) -> bool:
    """Return True only if new status is more advanced than current."""
    return _STATUS_PRIORITY.get(new, 0) > _STATUS_PRIORITY.get(current, 0)


# ── DigiKey order status → Minerva status ────────────────────────────────────

DIGIKEY_STATUS_MAP = {
    # DigiKey SalesOrderStatus strings (not fully documented; extend as discovered)
    "Open":                    "received",
    "Unconfirmed":             "received",
    "NeedShippingInformation": "received",
    "Need Shipping Information": "received",
    "Processing":              "processing",
    "InProduction":            "processing",
    "Backordered":             "processing",
    "Shipped":                 "shipped",
    "PartialShipped":          "shipped",
    "Delivered":               "delivered",
    "Cancelled":               "cancelled",
    "Closed":                  "delivered",
}


class DigiKeyAPIError(Exception):
    pass


# ── Token management ─────────────────────────────────────────────────────────

def _base_url(config) -> str:
    return _SANDBOX_BASE if config.use_sandbox else _PROD_BASE


def get_token(config) -> str:
    """OAuth2 Client Credentials — повертає access_token.
    Кешує результат у DigiKeyConfig (update via QuerySet для уникнення race-condition)."""
    import requests as req
    from bots.models import DigiKeyConfig

    # Check cache (with 60s buffer before expiry)
    if (
        config.access_token
        and config.token_expires_at
        and config.token_expires_at > timezone.now() + timedelta(seconds=60)
    ):
        return config.access_token

    from tabele.api_logger import logged_request
    resp = logged_request('digikey', 'get_token', 'POST',
                          f"{_base_url(config)}{TOKEN_PATH}", req.post,
                          data={
                              "client_id":     config.client_id,
                              "client_secret": config.client_secret,
                              "grant_type":    "client_credentials",
                          },
                          timeout=15)
    try:
        resp.raise_for_status()
    except req.HTTPError as e:
        body = {}
        try:
            body = e.response.json()
        except Exception:
            pass
        raise DigiKeyAPIError(
            f"Token error {e.response.status_code}: {body.get('error_description') or body}"
        ) from e

    data = resp.json()
    token      = data["access_token"]
    expires_in = int(data.get("expires_in", 600))  # DigiKey: 600s = 10 min

    DigiKeyConfig.objects.filter(pk=1).update(
        access_token=token,
        token_expires_at=timezone.now() + timedelta(seconds=expires_in - 30),
    )
    config.access_token     = token
    config.token_expires_at = timezone.now() + timedelta(seconds=expires_in - 30)
    return token


def _headers(config, token: str) -> dict:
    return {
        "Authorization":             f"Bearer {token}",
        "X-DIGIKEY-Client-Id":       config.client_id,
        "X-DIGIKEY-Locale-Language": config.locale_language,
        "X-DIGIKEY-Locale-Currency": config.locale_currency,
        "X-DIGIKEY-Locale-Site":     config.locale_site,
        "X-DIGIKEY-Account-Id":      config.account_id,
        "Content-Type":              "application/json",
    }


# ── API calls ────────────────────────────────────────────────────────────────

def search_orders(config, start_date=None, end_date=None, page=1, page_size=25) -> dict:
    """GET /orderstatus/v4/orders — повертає список замовлень."""
    import requests as req

    token = get_token(config)
    params = {
        "Shared":     False,
        "PageNumber": page,
        "PageSize":   min(page_size, 25),  # API max = 25
    }
    if start_date:
        params["StartDate"] = (
            start_date.strftime("%Y-%m-%d")
            if hasattr(start_date, "strftime")
            else str(start_date)
        )
    if end_date:
        params["EndDate"] = (
            end_date.strftime("%Y-%m-%d")
            if hasattr(end_date, "strftime")
            else str(end_date)
        )

    from tabele.api_logger import logged_request
    resp = logged_request('digikey', 'search_orders', 'GET',
                          f"{_base_url(config)}{ORDERS_PATH}", req.get,
                          headers=_headers(config, token), params=params, timeout=30)
    try:
        resp.raise_for_status()
    except req.HTTPError as e:
        body = {}
        try:
            body = e.response.json()
        except Exception:
            pass
        raise DigiKeyAPIError(
            f"Orders API {e.response.status_code}: {body.get('message') or body}"
        ) from e

    return resp.json()


def get_order(config, sales_order_id: str) -> dict:
    """GET /orderstatus/v4/orders/{salesOrderId} — деталі одного замовлення."""
    import requests as req

    token = get_token(config)
    from tabele.api_logger import logged_request
    resp = logged_request('digikey', 'get_order', 'GET',
                          f"{_base_url(config)}{ORDERS_PATH}/{sales_order_id}", req.get,
                          headers=_headers(config, token), timeout=15)
    resp.raise_for_status()
    return resp.json()


# ── Date helpers ─────────────────────────────────────────────────────────────

def _parse_date(s) -> date | None:
    if not s:
        return None
    try:
        clean = str(s).replace("Z", "+00:00").replace(" ", "T")
        if "T" in clean:
            return datetime.fromisoformat(clean).date()
        return datetime.strptime(clean[:10], "%Y-%m-%d").date()
    except Exception:
        return None


# ── Main sync function ───────────────────────────────────────────────────────

def sync_orders(config) -> dict:
    """
    Polls DigiKey Orders API і синхронізує замовлення в Minerva.

    Стратегія:
    - Нові замовлення (get_or_create) → створюємо SalesOrder + SalesOrderLine
    - Існуючі → оновлюємо статус якщо він змінився
    - Продукти зіставляються за Product.sku == ManufacturerProductNumber або DigiKeyProductNumber
    - Якщо продукт не знайдено → рядок пропускається, SKU логується в unmatched_skus

    Повертає dict зі статистикою.
    """
    from sales.models import SalesOrder, SalesOrderLine
    from inventory.models import Product
    from bots.models import DigiKeyConfig

    stats = {
        "created":        0,
        "updated":        0,
        "skipped":        0,
        "lines_created":  0,
        "lines_skipped":  0,
        "unmatched_skus": [],
        "errors":         [],
    }

    # Date range: last sync - 1 day ... today (overlap prevents gaps)
    if config.last_synced_at:
        start = config.last_synced_at.date() - timedelta(days=1)
    else:
        start = date.today() - timedelta(days=30)
    end = date.today()

    page = 1
    total_fetched = 0

    while True:
        try:
            data = search_orders(config, start_date=start, end_date=end,
                                 page=page, page_size=25)
        except DigiKeyAPIError as e:
            stats["errors"].append(f"Page {page}: {e}")
            break

        orders = data.get("Orders") or []
        if not orders:
            break

        for order in orders:
            order_number = str(order.get("OrderNumber", ""))
            order_currency = order.get("Currency", "USD")
            order_date_str  = order.get("DateEntered", "")

            for so in order.get("SalesOrders") or []:
                so_id = str(so.get("SalesOrderId", ""))
                if not so_id:
                    continue
                total_fetched += 1

                try:
                    _process_sales_order(
                        so, so_id, order_number, order_currency,
                        order_date_str, stats,
                    )
                except Exception as e:
                    logger.exception("DigiKey sync error for SalesOrderId=%s", so_id)
                    stats["errors"].append(f"SalesOrderId {so_id}: {e}")

        total_orders = data.get("TotalOrders") or 0
        if page * 25 >= total_orders:
            break
        page += 1

    # Update last_synced_at
    DigiKeyConfig.objects.filter(pk=1).update(last_synced_at=timezone.now())
    config.last_synced_at = timezone.now()

    logger.info(
        "DigiKey sync done: created=%d updated=%d skipped=%d "
        "lines_created=%d lines_skipped=%d errors=%d",
        stats["created"], stats["updated"], stats["skipped"],
        stats["lines_created"], stats["lines_skipped"], len(stats["errors"]),
    )
    return stats


def _process_sales_order(so: dict, so_id: str, order_number: str,
                          order_currency: str, order_date_str: str,
                          stats: dict):
    """Обробляє один DigiKey SalesOrder — get_or_create + рядки."""
    from sales.models import SalesOrder, SalesOrderLine
    from inventory.models import Product

    contact    = so.get("Contact") or {}
    addr       = so.get("ShippingAddress") or {}
    status_obj = so.get("Status") or {}

    contact_name   = f"{contact.get('FirstName', '')} {contact.get('LastName', '')}".strip()
    dk_status      = (status_obj.get("SalesOrderStatus")
                      or status_obj.get("ShortDescription", ""))
    minerva_status = DIGIKEY_STATUS_MAP.get(dk_status, "received")

    order_date = _parse_date(so.get("DateEntered") or order_date_str)

    so_currency = so.get("Currency") or order_currency

    # ── Shipping recipient (may differ from billing contact) ──────────────────
    addr_first   = addr.get("FirstName", "")
    addr_last    = addr.get("LastName", "")
    ship_name_str = f"{addr_first} {addr_last}".strip()  # actual recipient
    ship_company_str = addr.get("CompanyName", "")
    # client (legacy display): shipping company or recipient name or billing contact
    client_str = ship_company_str or ship_name_str or contact_name

    defaults = {
        "document_type":  "SALE",
        "affects_stock":  True,
        "order_date":     order_date,
        "status":         minerva_status,
        "contact_name":   contact_name,
        "email":          contact.get("Email", ""),
        "client":         client_str,
        "total_price":    so.get("TotalPrice"),
        "currency":       so_currency,
        # Shipping recipient
        "ship_name":      ship_name_str,
        "ship_company":   ship_company_str,
        "ship_phone":     addr.get("PhoneNumber", ""),
        "ship_email":     addr.get("Email", "") or "",
        "addr_city":      addr.get("City", ""),
        "addr_state":     addr.get("State", ""),
        "addr_zip":       addr.get("ZipCode", ""),
        "addr_country":   _normalize_country(addr.get("IsoCode") or ""),
        # Store main OrderNumber for reference
        "lieferschein_nr": order_number,
    }

    sale, created = SalesOrder.objects.get_or_create(
        source="digikey",
        order_number=so_id,
        defaults=defaults,
    )

    if created:
        stats["created"] += 1
        _create_lines(sale, so, so_currency, stats)
    else:
        # Only advance status (never downgrade: DigiKey may lag behind our shipment)
        if _status_can_advance(sale.status, minerva_status):
            sale.status = minerva_status
            sale.save(update_fields=["status"])
            stats["updated"] += 1
        else:
            stats["skipped"] += 1


def _create_lines(sale, so: dict, currency: str, stats: dict):
    """Створює SalesOrderLine для нового замовлення."""
    from sales.models import SalesOrderLine
    from inventory.models import Product

    for li in so.get("LineItems") or []:
        dk_pn  = (li.get("DigiKeyProductNumber") or "").strip()
        mfr_pn = (li.get("ManufacturerProductNumber") or "").strip()

        # Match product: try ManufacturerProductNumber first (closer to internal SKU)
        product = None
        for sku in filter(None, [mfr_pn, dk_pn]):
            product = Product.objects.filter(sku__iexact=sku).first()
            if product:
                break

        if not product:
            stats["lines_skipped"] += 1
            unmatched = f"{dk_pn} / {mfr_pn}".strip(" /")
            if unmatched and unmatched not in stats["unmatched_skus"]:
                stats["unmatched_skus"].append(unmatched)
            logger.warning(
                "DigiKey sync: no Product found for DK=%r MFR=%r (order %s)",
                dk_pn, mfr_pn, sale.order_number,
            )
            continue

        qty = li.get("QuantityOrdered") or 1
        SalesOrderLine.objects.create(
            order=sale,
            product=product,
            sku_raw=dk_pn or mfr_pn,
            qty=qty,
            unit_price=li.get("UnitPrice"),
            total_price=li.get("TotalPrice"),
            currency=currency,
        )
        stats["lines_created"] += 1


# ── Product Information V4 ───────────────────────────────────────────────────

def search_products(config, keywords: str, limit: int = 50, offset: int = 0) -> dict:
    """POST /products/v4/search/keyword — пошук компонентів за ключовими словами."""
    import requests as req

    token = get_token(config)
    from tabele.api_logger import logged_request
    resp = logged_request('digikey', 'search_products', 'POST',
                          f"{_base_url(config)}{KEYWORD_SEARCH_PATH}", req.post,
                          headers=_headers(config, token),
                          json={"Keywords": keywords, "RecordsPerPage": min(limit, 50), "Offset": offset},
                          timeout=20)
    try:
        resp.raise_for_status()
    except req.HTTPError as e:
        body = {}
        try:
            body = e.response.json()
        except Exception:
            pass
        raise DigiKeyAPIError(
            f"Product Search {e.response.status_code}: {body.get('message') or body}"
        ) from e

    return resp.json()


# ── OrderManagement API ──────────────────────────────────────────────────────

def get_orders_by_po_number(config, po_number: str, limit: int = 100) -> dict:
    """GET /OrderManagement/v1/SalesOrders/Search/PoNumber/{poNumber}
    Пошук замовлень DigiKey за PO-номером покупця."""
    import requests as req

    token = get_token(config)
    url = f"{_base_url(config)}{PO_SEARCH_PATH}/{po_number}"
    from tabele.api_logger import logged_request
    resp = logged_request('digikey', 'get_orders_by_po', 'GET', url, req.get,
                          headers=_headers(config, token),
                          params={"ordersLimit": min(limit, 1000)}, timeout=15)
    try:
        resp.raise_for_status()
    except req.HTTPError as e:
        body = {}
        try:
            body = e.response.json()
        except Exception:
            pass
        raise DigiKeyAPIError(
            f"PO Search {e.response.status_code}: {body.get('message') or body}"
        ) from e

    return resp.json()


# ── Marketplace 3-legged OAuth ───────────────────────────────────────────────

def build_authorize_url(config, redirect_uri: str) -> str:
    """Повертає URL для авторизації користувача (3-legged OAuth)."""
    from urllib.parse import urlencode
    base = _PROD_BASE  # авторизація завжди через production URL
    params = urlencode({
        "response_type": "code",
        "client_id":     config.client_id,
        "redirect_uri":  redirect_uri,
        "scope":         "openid",
    })
    return f"{base}{AUTHORIZE_PATH}?{params}"


def exchange_code_for_tokens(config, code: str, redirect_uri: str) -> dict:
    """Обмінює authorization code на access_token + refresh_token."""
    import requests as req
    base = _SANDBOX_BASE if config.use_sandbox else _PROD_BASE
    resp = req.post(
        f"{base}{TOKEN_PATH}",
        data={
            "client_id":     config.client_id,
            "client_secret": config.client_secret,
            "code":          code,
            "grant_type":    "authorization_code",
            "redirect_uri":  redirect_uri,
        },
        timeout=15,
    )
    try:
        resp.raise_for_status()
    except req.HTTPError as e:
        body = {}
        try:
            body = e.response.json()
        except Exception:
            pass
        raise DigiKeyAPIError(
            f"OAuth exchange error {e.response.status_code}: {body.get('error_description') or body}"
        ) from e
    return resp.json()


def refresh_marketplace_token(config) -> str:
    """Оновлює marketplace access_token через refresh_token. Повертає новий access_token."""
    import requests as req
    from bots.models import DigiKeyConfig

    if not config.marketplace_refresh_token:
        raise DigiKeyAPIError("Marketplace не авторизовано. Натисніть 'Авторизувати Marketplace'.")

    base = _SANDBOX_BASE if config.use_sandbox else _PROD_BASE
    resp = req.post(
        f"{base}{TOKEN_PATH}",
        data={
            "client_id":     config.client_id,
            "client_secret": config.client_secret,
            "refresh_token": config.marketplace_refresh_token,
            "grant_type":    "refresh_token",
        },
        timeout=15,
    )
    try:
        resp.raise_for_status()
    except req.HTTPError as e:
        body = {}
        try:
            body = e.response.json()
        except Exception:
            pass
        raise DigiKeyAPIError(
            f"Token refresh error {e.response.status_code}: {body.get('error_description') or body}"
        ) from e

    data       = resp.json()
    token      = data["access_token"]
    expires_in = int(data.get("expires_in", 600))
    new_refresh = data.get("refresh_token", config.marketplace_refresh_token)

    DigiKeyConfig.objects.filter(pk=1).update(
        marketplace_access_token=token,
        marketplace_refresh_token=new_refresh,
        marketplace_token_expires_at=timezone.now() + timedelta(seconds=expires_in - 30),
    )
    config.marketplace_access_token  = token
    config.marketplace_refresh_token = new_refresh
    config.marketplace_token_expires_at = timezone.now() + timedelta(seconds=expires_in - 30)
    return token


def get_marketplace_token(config) -> str:
    """Повертає актуальний marketplace access_token (з кешу або оновлює через refresh)."""
    if (
        config.marketplace_access_token
        and config.marketplace_token_expires_at
        and config.marketplace_token_expires_at > timezone.now() + timedelta(seconds=60)
    ):
        return config.marketplace_access_token
    return refresh_marketplace_token(config)


def get_marketplace_orders(config, offset: int = 0, max_results: int = 20,
                           created_from: str = None, created_to: str = None,
                           order_state: str = None) -> dict:
    """GET /Sales/Marketplace2/Orders/v1/orders — вхідні замовлення маркетплейсу."""
    import requests as req

    token = get_marketplace_token(config)
    params = {"Offset": offset, "Max": min(max_results, 100)}
    if created_from:
        params["CreatedFrom"] = created_from
    if created_to:
        params["CreatedTo"] = created_to
    if order_state:
        params["OrderState"] = order_state

    from tabele.api_logger import logged_request
    resp = logged_request('digikey', 'get_marketplace_orders', 'GET',
                          f"{_base_url(config)}{MARKETPLACE_PATH}", req.get,
                          headers=_headers(config, token), params=params, timeout=30)
    try:
        resp.raise_for_status()
    except req.HTTPError as e:
        body = {}
        try:
            body = e.response.json()
        except Exception:
            pass
        raise DigiKeyAPIError(
            f"Marketplace Orders {e.response.status_code}: {body.get('detail') or body}"
        ) from e
    return resp.json()


# ── Marketplace status → Minerva status ──────────────────────────────────────

MARKETPLACE_STATUS_MAP = {
    "New":                "received",
    "PendingAcceptance":  "received",
    "AwaitingAcceptance": "received",
    "Accepted":           "processing",
    "ShippingInProgress": "processing",
    "Shipped":            "shipped",
    "Received":           "delivered",   # покупець отримав
    "Delivered":          "delivered",
    "Completed":          "delivered",
    "Cancelled":          "cancelled",
    "Rejected":           "cancelled",
}

# ISO 3166-1 alpha-3 → alpha-2 (найпоширеніші в e-commerce)
_ISO3_TO_ISO2 = {
    "AUT": "AT", "BEL": "BE", "BGR": "BG", "CHE": "CH", "CYP": "CY",
    "CZE": "CZ", "DEU": "DE", "DNK": "DK", "ESP": "ES", "EST": "EE",
    "FIN": "FI", "FRA": "FR", "GBR": "GB", "GRC": "GR", "HRV": "HR",
    "HUN": "HU", "IRL": "IE", "ISL": "IS", "ITA": "IT", "LIE": "LI",
    "LTU": "LT", "LUX": "LU", "LVA": "LV", "MLT": "MT", "NLD": "NL",
    "NOR": "NO", "POL": "PL", "PRT": "PT", "ROU": "RO", "SVK": "SK",
    "SVN": "SI", "SWE": "SE", "TUR": "TR", "UKR": "UA", "USA": "US",
    "CAN": "CA", "AUS": "AU", "JPN": "JP", "CHN": "CN", "KOR": "KR",
    "SGP": "SG", "HKG": "HK", "TWN": "TW", "IND": "IN", "BRA": "BR",
    "MEX": "MX", "ARG": "AR", "ZAF": "ZA", "NZL": "NZ",
}


def _normalize_country(code: str) -> str:
    """Конвертує ISO 3166-1 alpha-3 в alpha-2. Повертає alpha-2 як є."""
    if not code:
        return ""
    code = code.strip().upper()
    if len(code) == 3:
        return _ISO3_TO_ISO2.get(code, code[:2])
    return code[:2]


def _get_additional_field(fields: list, code: str, default=None):
    """Витягує значення з additionalFields по code."""
    for f in fields or []:
        if f.get("code") == code:
            return f.get("value", default)
    return default


def sync_marketplace_orders(config) -> dict:
    """
    Синхронізує вхідні замовлення DigiKey Marketplace → Minerva SalesOrder.

    Структура Marketplace замовлення:
      order.businessId       → order_number
      order.orderState       → status
      order.customer         → контакт покупця
      order.orderDetails[]   → рядки (supplierSku, qty, price)
      order.additionalFields → currency, PO номер тощо
    """
    from sales.models import SalesOrder, SalesOrderLine
    from inventory.models import Product
    from bots.models import DigiKeyConfig

    stats = {
        "created":        0,
        "updated":        0,
        "skipped":        0,
        "lines_created":  0,
        "lines_skipped":  0,
        "unmatched_skus": [],
        "errors":         [],
        "changes":        [],
    }

    # Date range: last sync - 7 days (broad overlap to avoid missing orders) … today
    if config.last_synced_at:
        from_dt = (config.last_synced_at - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        from_dt = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")

    offset = 0
    batch  = 50

    while True:
        try:
            data = get_marketplace_orders(
                config,
                offset=offset,
                max_results=batch,
                created_from=from_dt,
            )
        except DigiKeyAPIError as e:
            stats["errors"].append(f"offset={offset}: {e}")
            break

        orders = data.get("orders") or []
        if not orders:
            break

        for order in orders:
            try:
                _process_marketplace_order(order, stats, config)
            except Exception as e:
                logger.exception("Marketplace sync error order=%s", order.get("businessId"))
                stats["errors"].append(f"order {order.get('businessId')}: {e}")

        total = data.get("totalCount") or 0
        offset += batch
        if offset >= total:
            break

    # Оновлюємо last_synced_at тільки якщо не було критичних помилок
    if not stats["errors"] or stats["created"] > 0 or stats["updated"] > 0:
        DigiKeyConfig.objects.filter(pk=1).update(last_synced_at=timezone.now())
        config.last_synced_at = timezone.now()

    logger.info(
        "Marketplace sync done: created=%d updated=%d skipped=%d errors=%d",
        stats["created"], stats["updated"], stats["skipped"], len(stats["errors"]),
    )
    return stats


def _process_marketplace_order(order: dict, stats: dict, config=None):
    """Обробляє одне Marketplace замовлення — get_or_create + рядки."""
    from sales.models import SalesOrder, SalesOrderLine
    from inventory.models import Product

    order_number = str(order.get("businessId", order.get("id", "")))
    if not order_number:
        stats["skipped"] += 1
        return

    customer  = order.get("customer") or {}
    addr      = customer.get("shippingAddress") or customer.get("billingAddress") or {}
    add_fields = order.get("additionalFields") or []

    dk_state       = order.get("orderState", "")
    minerva_status = MARKETPLACE_STATUS_MAP.get(dk_state, "received")

    currency    = (config.locale_currency if config and config.locale_currency else None) or "USD"
    po_number   = _get_additional_field(add_fields, "customer-purchase-order-number", "")
    order_date  = _parse_date(order.get("createDateUtc"))
    deadline    = _parse_date(order.get("shippingDeadlineUtc"))

    # Billing contact (who placed the order)
    bill_first  = customer.get("firstName", "")
    bill_last   = customer.get("lastName", "")
    contact     = f"{bill_first} {bill_last}".strip()  # e.g. ANGELA LEONES

    # Shipping recipient (who receives the package — may differ from billing)
    ship_first   = addr.get("firstName", "")
    ship_last    = addr.get("lastName", "")
    ship_name_str = f"{ship_first} {ship_last}".strip()  # e.g. CHARLES GORDON P-21118
    ship_company_str = addr.get("companyName", "")        # e.g. SCIENCE CORPORATION

    # client = shipping company (or billing name if no company) — legacy field kept for display
    client    = ship_company_str or contact

    street       = " ".join(filter(None, [addr.get("street1", ""), addr.get("street2", "").strip()]))
    phone        = addr.get("phoneNumber", "")
    email        = customer.get("customerEmail", "")
    addr_country = _normalize_country(addr.get("countryCode") or "")

    # Legacy raw address block
    shipping_address_raw = "\n".join(filter(None, [
        ship_company_str,
        ship_name_str,
        street,
        f"{addr.get('city', '')}, {addr.get('postalCode', '')} {addr.get('countryCode', '')}".strip(", "),
        f"Phone: {phone}" if phone else "",
        f"Email: {email}" if email else "",
    ]))

    defaults = {
        "document_type":    "SALE",
        "affects_stock":    True,
        "order_date":       order_date,
        "status":           minerva_status,
        # Billing contact
        "contact_name":     contact,
        "email":            email,
        "client":           client,
        "phone":            phone,
        # Shipping recipient (dedicated fields)
        "ship_name":        ship_name_str,
        "ship_company":     ship_company_str,
        "ship_phone":       phone,
        "ship_email":       addr.get("emailAddress", "") or "",
        "shipping_address": shipping_address_raw,
        "total_price":      order.get("totalPrice"),
        "currency":         currency,
        "addr_street":      street,
        "addr_city":        addr.get("city", ""),
        "addr_state":       addr.get("state", "") if addr_country in ("US", "CA") else "",
        "addr_zip":         addr.get("postalCode", ""),
        "addr_country":     addr_country,
        "lieferschein_nr":  po_number,
        "shipping_deadline": deadline,
    }

    sale, created = SalesOrder.objects.get_or_create(
        source="digikey",
        order_number=order_number,
        defaults=defaults,
    )

    if created:
        stats["created"] += 1
        _change_entry = {
            "order":      order_number,
            "client":     client,
            "old_status": "—",
            "new_status": minerva_status,
        }
        stats["changes"].append(_change_entry)
        _create_marketplace_lines(sale, order, currency, stats)
        if config:
            _maybe_auto_confirm(config, order_number, sale, _change_entry)
    else:
        # Оновлюємо статус тільки вперед (не відкочуємо: DigiKey може відставати)
        if _status_can_advance(sale.status, minerva_status):
            old_status = sale.status
            sale.status = minerva_status
            sale.save(update_fields=["status"])
            stats["updated"] += 1
            stats["changes"].append({
                "order":      order_number,
                "client":     client,
                "old_status": old_status,
                "new_status": minerva_status,
            })
        else:
            stats["skipped"] += 1


def _create_marketplace_lines(sale, order: dict, currency: str, stats: dict):
    """Створює SalesOrderLine для нового Marketplace замовлення."""
    from sales.models import SalesOrderLine
    from inventory.models import Product

    for line in order.get("orderDetails") or []:
        supplier_sku = (line.get("supplierSku") or "").strip()
        mfr_pn       = (line.get("manufacturerPartNumber") or "").strip()

        product = None
        for sku in filter(None, [supplier_sku, mfr_pn]):
            product = Product.objects.filter(sku__iexact=sku).first()
            if product:
                break

        qty        = line.get("quantity") or 1
        unit_price = line.get("unitPrice")
        total      = line.get("totalPrice")
        sku_raw    = supplier_sku or mfr_pn

        if not product:
            stats["lines_skipped"] += 1
            if sku_raw and sku_raw not in stats["unmatched_skus"]:
                stats["unmatched_skus"].append(sku_raw)
            logger.warning(
                "Marketplace sync: no Product for SKU=%r MFR=%r (order %s)",
                supplier_sku, mfr_pn, sale.order_number,
            )
            continue  # пропускаємо — product NOT NULL

        SalesOrderLine.objects.create(
            order=sale,
            product=product,
            sku_raw=sku_raw,
            qty=qty,
            unit_price=unit_price,
            total_price=total,
            currency=currency,
        )
        stats["lines_created"] += 1


# ── Reconciliation (звірка існуючих замовлень з DigiKey) ─────────────────────

def reconcile_marketplace_orders(config, days_back: int = 365, dry_run: bool = False) -> dict:
    """
    Звіряє замовлення DigiKey з Minerva:
    - Якщо замовлення є в DigiKey і в Minerva → оновлює поля
    - Якщо є в DigiKey але немає в Minerva → створює і додає до stats['added']
    - Повертає детальний звіт змін
    """
    from sales.models import SalesOrder, SalesOrderLine
    from inventory.models import Product

    stats = {
        "checked":        0,
        "updated":        0,
        "added":          [],   # нові замовлення яких не було
        "unchanged":      0,
        "lines_updated":  0,
        "unmatched_skus": [],
        "errors":         [],
        "changes":        [],   # список {order_number, field, old, new}
    }

    from_dt = (
        None if days_back == 0
        else (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    offset, batch = 0, 50

    while True:
        try:
            data = get_marketplace_orders(config, offset=offset, max_results=batch,
                                          created_from=from_dt)
        except DigiKeyAPIError as e:
            stats["errors"].append(f"offset={offset}: {e}")
            break

        orders = data.get("orders") or []
        if not orders:
            break

        for order in orders:
            try:
                _reconcile_one(order, stats, dry_run=dry_run)
            except Exception as e:
                logger.exception("Reconcile error order=%s", order.get("businessId"))
                stats["errors"].append(f"order {order.get('businessId')}: {e}")

        stats["checked"] += len(orders)
        total = data.get("totalCount") or 0
        offset += batch
        if offset >= total:
            break

    return stats


def _reconcile_one(order: dict, stats: dict, dry_run: bool = False):
    """Звіряє одне DigiKey замовлення з Minerva."""
    from sales.models import SalesOrder, SalesOrderLine
    from inventory.models import Product
    from bots.models import DigiKeyConfig as _DKConfig

    order_number = str(order.get("businessId", ""))
    if not order_number:
        return

    customer   = order.get("customer") or {}
    addr       = customer.get("shippingAddress") or customer.get("billingAddress") or {}
    add_fields = order.get("additionalFields") or []

    _cfg = _DKConfig.objects.filter(pk=1).first()
    # ── Витягуємо всі поля ────────────────────────────────────────────────────
    dk_state   = order.get("orderState", "")
    new_status = MARKETPLACE_STATUS_MAP.get(dk_state, "received")
    currency   = (_cfg.locale_currency if _cfg and _cfg.locale_currency else None) or "USD"
    po_number  = _get_additional_field(add_fields, "customer-purchase-order-number", "")
    tracking   = _get_additional_field(add_fields, "internal-tracking-number", "")
    carrier    = order.get("shippingMethodLabel") or ""

    street     = " ".join(filter(None, [addr.get("street1", ""), addr.get("street2", "").strip()]))
    phone      = addr.get("phoneNumber", "")
    email      = customer.get("customerEmail", "")
    company    = addr.get("companyName", "")
    first      = customer.get("firstName", "")
    last       = customer.get("lastName", "")
    client     = company or f"{first} {last}".strip()
    contact    = f"{first} {last}".strip()

    shipping_address_raw = "\n".join(filter(None, [
        company,
        f"{addr.get('firstName', '')} {addr.get('lastName', '')}".strip(),
        street,
        f"{addr.get('city', '')}, {addr.get('postalCode', '')} {addr.get('countryCode', '')}".strip(", "),
        f"Phone: {phone}" if phone else "",
        f"Email: {email}" if email else "",
    ]))

    order_date = _parse_date(order.get("createDateUtc"))
    deadline   = _parse_date(order.get("shippingDeadlineUtc"))
    shipped_at = _parse_date(order.get("shippedDateUtc"))
    delivered_at_raw = order.get("receivedDateUtc")
    delivered_at = None
    if delivered_at_raw:
        try:
            from django.utils.dateparse import parse_datetime
            delivered_at = parse_datetime(
                str(delivered_at_raw).replace("Z", "+00:00")
            )
        except Exception:
            pass

    addr_country = _normalize_country(addr.get("countryCode") or "")

    # ── Знайти або створити SalesOrder ────────────────────────────────────────
    try:
        sale = SalesOrder.objects.get(source="digikey", order_number=order_number)
    except SalesOrder.DoesNotExist:
        # Немає в базі — реєструємо як нове
        stats["added"].append(order_number)
        if not dry_run:
            sale = SalesOrder.objects.create(
                source="digikey",
                order_number=order_number,
                document_type="SALE",
                affects_stock=True,
                order_date=order_date,
                status=new_status,
                contact_name=contact,
                email=email,
                client=client,
                phone=phone,
                shipping_address=shipping_address_raw,
                total_price=order.get("totalPrice"),
                currency=currency,
                addr_street=street,
                addr_city=addr.get("city", ""),
                addr_state=addr.get("state", ""),
                addr_zip=addr.get("postalCode", ""),
                addr_country=addr_country,
                lieferschein_nr=po_number,
                shipping_deadline=deadline,
                shipped_at=shipped_at,
                delivered_at=delivered_at,
                shipping_courier=carrier,
                tracking_number=tracking,
            )
            _create_marketplace_lines(sale, order, currency, stats)
        return

    # ── Порівнюємо і оновлюємо ────────────────────────────────────────────────
    update_fields = []

    def _check(field, new_val):
        """Порівнює поле, додає до update якщо змінилось."""
        old_val = getattr(sale, field)
        # Нормалізація: порожній рядок = None для дат
        if new_val is None and old_val is None:
            return
        if str(new_val or "").strip() == str(old_val or "").strip():
            return
        if new_val is None and not old_val:
            return
        stats["changes"].append({
            "order": order_number, "field": field,
            "old": str(old_val), "new": str(new_val),
        })
        setattr(sale, field, new_val)
        update_fields.append(field)

    # Адреса
    _check("addr_street",  street)
    _check("addr_city",    addr.get("city", ""))
    _check("addr_zip",     addr.get("postalCode", ""))
    _check("addr_country", addr_country)
    _check("phone",        phone)
    _check("email",        email)
    _check("shipping_address", shipping_address_raw)

    # Дати
    _check("order_date",       order_date)
    _check("shipping_deadline", deadline)
    _check("shipped_at",       shipped_at)
    _check("delivered_at",     delivered_at)

    # Статус — тільки якщо просунувся вперед
    status_order = ["received", "processing", "shipped", "delivered", "cancelled"]
    old_idx = status_order.index(sale.status) if sale.status in status_order else 0
    new_idx = status_order.index(new_status)  if new_status in status_order else 0
    if new_idx > old_idx:
        _check("status", new_status)

    # Кур'єр і трекінг — тільки якщо в DigiKey є значення
    if carrier:
        _check("shipping_courier", carrier)
    if tracking:
        _check("tracking_number", tracking)

    if update_fields:
        if not dry_run:
            sale.save(update_fields=update_fields)
        stats["updated"] += 1
    else:
        stats["unchanged"] += 1

    # ── Звірка рядків (ціни і кількість) ─────────────────────────────────────
    for line in order.get("orderDetails") or []:
        supplier_sku = (line.get("supplierSku") or "").strip()
        mfr_pn       = (line.get("manufacturerPartNumber") or "").strip()
        sku_raw      = supplier_sku or mfr_pn

        existing_line = sale.lines.filter(sku_raw__iexact=sku_raw).first()
        if not existing_line:
            # Рядок відсутній — знаходимо продукт і додаємо
            product = None
            for sku in filter(None, [supplier_sku, mfr_pn]):
                product = __import__('inventory').models.Product.objects.filter(
                    sku__iexact=sku).first()
                if product:
                    break
            if product:
                if not dry_run:
                    SalesOrderLine.objects.create(
                        order=sale,
                        product=product,
                        sku_raw=sku_raw,
                        qty=line.get("quantity") or 1,
                        unit_price=line.get("unitPrice"),
                        total_price=line.get("totalPrice"),
                        currency=currency,
                    )
                stats["lines_updated"] += 1
            else:
                if sku_raw and sku_raw not in stats["unmatched_skus"]:
                    stats["unmatched_skus"].append(sku_raw)
            continue

        # Оновлюємо ціни якщо змінились
        line_changed = []
        if line.get("unitPrice") and str(existing_line.unit_price or "") != str(line["unitPrice"]):
            existing_line.unit_price = line["unitPrice"]
            line_changed.append("unit_price")
        if line.get("totalPrice") and str(existing_line.total_price or "") != str(line["totalPrice"]):
            existing_line.total_price = line["totalPrice"]
            line_changed.append("total_price")
        if line.get("quantity") and str(existing_line.qty) != str(line["quantity"]):
            existing_line.qty = line["quantity"]
            line_changed.append("qty")
        if line_changed:
            if not dry_run:
                existing_line.save(update_fields=line_changed)
            stats["lines_updated"] += 1


# ── Marketplace: авто-підтвердження ──────────────────────────────────────────

def _check_stock_for_order(sale) -> bool:
    """Повертає True якщо всі рядки замовлення є на складі в достатній кількості."""
    from django.db.models import Sum
    from inventory.models import InventoryTransaction

    for line in sale.lines.select_related("product"):
        if not line.product:
            return False  # невідомий товар — не підтверджувати
        stock = (
            InventoryTransaction.objects
            .filter(product=line.product)
            .aggregate(total=Sum("quantity"))["total"] or 0
        )
        if stock < line.qty:
            return False
    return True


def _maybe_auto_confirm(config, order_id: str, sale, change_entry: dict = None) -> None:
    """
    Перевіряє auto_confirm_mode і при потребі підтверджує замовлення на DigiKey.
    Викликається одразу після створення нового Marketplace замовлення.

    Режими:
      never    — нічого не робити (мануально)
      always   — підтвердити одразу
      in_stock — підтвердити тільки якщо всі товари є на складі
    """
    mode = getattr(config, "auto_confirm_mode", "never")
    if mode == "never":
        return

    if mode == "in_stock":
        if not _check_stock_for_order(sale):
            logger.info(
                "DigiKey auto-confirm skipped (insufficient stock) order=%s", order_id
            )
            return

    result = confirm_marketplace_order(config, order_id)
    if result["ok"]:
        logger.info("DigiKey auto-confirmed order=%s mode=%s", order_id, mode)
        # Immediately advance to «processing» — bypass signal to avoid duplicate
        # notify_status_change (we send our own richer notification below).
        from sales.models import SalesOrder as _SO
        _SO.objects.filter(pk=sale.pk).update(status="processing")
        sale.status = "processing"
        if change_entry is not None:
            change_entry["new_status"] = "processing"
            change_entry["extra"] = "🤖 авто-підтверджено"
        try:
            from dashboard.notifications import notify_digikey_auto_confirmed
            notify_digikey_auto_confirmed(sale, mode)
        except Exception:
            pass
    else:
        logger.warning(
            "DigiKey auto-confirm FAILED order=%s mode=%s: %s",
            order_id, mode, result["message"],
        )


# ── Marketplace: підтвердження замовлення ────────────────────────────────────

# PUT api.digikey.com/Sales/Marketplace2/Orders/v1/orders/{orderId}/accept
# Body: {"acceptOrderDetails": [{"orderDetailId": "...", "accepted": true}, ...]}
MARKETPLACE_CONFIRM_PATH = "/Sales/Marketplace2/Orders/v1/orders/{order_id}/accept"


def _fetch_marketplace_order(config, order_id: str, token: str) -> dict:
    """GET /Sales/Marketplace2/Orders/v1/orders/{orderId} — для отримання orderDetailId."""
    import requests as req
    from tabele.api_logger import logged_request
    resp = logged_request('digikey', 'fetch_marketplace_order', 'GET',
                          f"{_base_url(config)}{MARKETPLACE_PATH}/{order_id}", req.get,
                          headers=_headers(config, token), timeout=15)
    if resp.ok:
        return resp.json()
    return {}


def confirm_marketplace_order(config, order_id: str) -> dict:
    """Підтверджує (accepts) вхідне Marketplace замовлення через DigiKey API.

    PUT /Sales/Marketplace2/Orders/v1/orders/{orderId}/accept
    Body: {"acceptOrderDetails": [{"orderDetailId": "...", "accepted": true}]}

    Спочатку отримує деталі замовлення (GET) щоб дістати orderDetailId кожного рядка,
    потім відправляє PUT з підтвердженням усіх рядків.
    Повертає {'ok': bool, 'message': str, 'raw': dict}.
    """
    import requests as req

    token = get_marketplace_token(config)

    # Отримуємо деталі замовлення — потрібні orderDetailId для кожного рядка
    order_data = _fetch_marketplace_order(config, order_id, token)
    accept_details = []
    for line in order_data.get("orderDetails") or []:
        detail_id = line.get("orderDetailId")
        if detail_id:
            accept_details.append({"orderDetailId": detail_id, "accepted": True})

    payload = {"acceptOrderDetails": accept_details}
    url = f"{_base_url(config)}{MARKETPLACE_CONFIRM_PATH.format(order_id=order_id)}"

    from tabele.api_logger import logged_request
    resp = logged_request('digikey', 'confirm_marketplace_order', 'PUT', url, req.put,
                          headers=_headers(config, token), json=payload, timeout=20)
    raw = {}
    try:
        raw = resp.json()
    except Exception:
        raw = {"text": resp.text[:500]}

    # Success: 200 з порожнім errors або errorCount=0
    if resp.status_code == 200:
        err_count = raw.get("errorCount", 0)
        if err_count == 0:
            return {"ok": True, "message": "✅ Замовлення підтверджено на DigiKey", "raw": raw}
        # Часткова помилка (окремі рядки відхилено)
        errs = raw.get("errors") or []
        msgs = [e.get("errorMessage", "") for e in errs if e.get("errorMessage")]
        detail = "; ".join(msgs) or "часткова помилка"
        logger.warning("DigiKey confirm order %s partial errors: %s", order_id, detail)
        return {"ok": False, "message": f"⚠️ Часткова помилка: {detail}", "raw": raw}

    detail = (raw.get("detail") or raw.get("title") or raw.get("error") or resp.text[:200])
    logger.error("DigiKey confirm order %s: %s %s", order_id, resp.status_code, detail)
    return {"ok": False, "message": f"❌ {resp.status_code}: {detail}", "raw": raw}


# ── Test connection helper ────────────────────────────────────────────────────

def test_connection(config) -> dict:
    """Перевіряє підключення до DigiKey API. Повертає {'ok': bool, 'message': str}."""
    try:
        token = get_token(config)
        # Try to fetch 1 order (just to confirm the endpoint works)
        data  = search_orders(config, page=1, page_size=1)
        total = data.get("TotalOrders", 0)
        mode  = "Sandbox" if config.use_sandbox else "Production"
        return {
            "ok":      True,
            "message": f"✅ Підключено ({mode}). Знайдено замовлень: {total}.",
            "token":   token[:12] + "…",
        }
    except DigiKeyAPIError as e:
        return {"ok": False, "message": f"❌ Помилка API: {e}"}
    except Exception as e:
        return {"ok": False, "message": f"❌ {type(e).__name__}: {e}"}
