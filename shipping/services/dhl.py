"""
shipping/services/dhl.py — DHL Express MyDHL API

Тарифи (rate shopping):
  GET https://express.api.dhl.com/mydhlapi/rates
  Auth: BasicAuth (api_key = username, api_secret = password)

Створення відправлення:
  POST https://express.api.dhl.com/mydhlapi/shipments
  Body: JSON з деталями відправника, отримувача, посилки

Трекінг:
  GET https://express.api.dhl.com/mydhlapi/shipments/{trackingNumber}/tracking

Документація: https://developer.dhl.com/api-reference/dhl-express-mydhl-api
"""
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

_PROD_BASE = "https://express.api.dhl.com/mydhlapi"
_TEST_BASE = "https://express.api.dhl.com/mydhlapi/test"


class DHLAPIError(Exception):
    pass


def _norm_postal(postal: str, country: str) -> str:
    """Normalize postal code to country-specific format required by DHL."""
    p = (postal or "").strip()
    c = (country or "").upper().strip()

    if c == "US":
        digits = p.replace("-", "").replace(" ", "")
        if len(digits) == 9:
            return f"{digits[:5]}-{digits[5:]}"
        return digits[:5]  # take first 5 digits only

    if c == "CA":
        # Canada: A1A 1A1 (with space)
        clean = p.replace(" ", "").upper()
        if len(clean) == 6:
            return f"{clean[:3]} {clean[3:]}"
        return p

    return p


def _split_address(addr: str, max_len: int = 45) -> dict:
    """Split a long address string into addressLine1/2/3 (max 45 chars each)."""
    addr = (addr or "").strip()
    if len(addr) <= max_len:
        return {"addressLine1": addr}
    # split at last space before max_len
    result = {}
    keys = ["addressLine1", "addressLine2", "addressLine3"]
    for key in keys:
        if not addr:
            break
        if len(addr) <= max_len:
            result[key] = addr
            addr = ""
        else:
            cut = addr.rfind(" ", 0, max_len)
            if cut == -1:
                cut = max_len
            result[key] = addr[:cut].strip()
            addr = addr[cut:].strip()
    return result


def get_rates(carrier, destination_country: str, destination_postal: str,
              destination_city: str, weight_kg: float,
              length_cm: float, width_cm: float, height_cm: float,
              is_customs_declarable: bool = False,
              account_number: str = "") -> dict:
    """
    GET /rates — повертає список DHL Express продуктів з цінами.

    carrier.api_key    = DHL API Username
    carrier.api_secret = DHL API Password
    carrier.api_url    = "test" або пусто (production)

    Повертає dict:
      {
        "products": [
          {
            "name": "EXPRESS WORLDWIDE",
            "code": "P",
            "price": 45.50,
            "currency": "EUR",
            "transit_days": 3,
            "delivery_date": "2026-03-09",
          },
          ...
        ],
        "error": None або str,
      }
    """
    import requests as req

    use_test = (carrier.api_url or "").strip().lower() == "test"
    base      = _TEST_BASE if use_test else _PROD_BASE

    acct = account_number or carrier.connection_uuid or ""

    # Дата відправлення: наступний робочий день
    ship_date = date.today() + timedelta(days=1)
    # Якщо неділя → понеділок
    if ship_date.weekday() == 6:
        ship_date += timedelta(days=1)
    params = {
        "accountNumber":          acct,
        "originCountryCode":      carrier.sender_country or "DE",
        "originPostalCode":       _norm_postal(carrier.sender_zip, carrier.sender_country or "DE"),
        "originCityName":         carrier.sender_city   or "",
        "destinationCountryCode": destination_country,
        "destinationPostalCode":  _norm_postal(destination_postal, destination_country),
        "destinationCityName":    destination_city,
        "weight":                 round(float(weight_kg), 3),
        "length":                 int(length_cm),
        "width":                  int(width_cm),
        "height":                 int(height_cm),
        "plannedShippingDate":    ship_date.isoformat(),   # YYYY-MM-DD, без часу
        "isCustomsDeclarable":    str(is_customs_declarable).lower(),
        "unitOfMeasurement":      "metric",
    }

    try:
        from tabele.api_logger import logged_request
        resp = logged_request('dhl', 'get_rates', 'GET', f"{base}/rates", req.get,
                              params=params,
                              auth=(carrier.api_key, carrier.api_secret),
                              headers={"Accept": "application/json"},
                              timeout=20)
    except req.exceptions.Timeout:
        return {"products": [], "error": "Timeout — DHL API не відповідає"}
    except Exception as e:
        return {"products": [], "error": str(e)}

    if not resp.ok:
        body = {}
        try:
            body = resp.json()
        except Exception:
            pass
        detail = (body.get("detail")
                  or body.get("message")
                  or body.get("title")
                  or str(body)
                  or resp.text[:200])
        return {"products": [], "error": f"HTTP {resp.status_code}: {detail}"}

    try:
        data = resp.json()
    except Exception as e:
        return {"products": [], "error": f"JSON parse error: {e}"}

    products = []
    for p in data.get("products") or []:
        name = p.get("productName") or p.get("localProductName") or p.get("productCode", "—")
        code = p.get("productCode", "")

        # Ціна: шукаємо PUBLISHED або першу доступну
        price_list = p.get("totalPrice") or []
        price = None
        currency = "EUR"
        for tp in price_list:
            if tp.get("priceType") in ("PUBLISHED", "ACCOUNT", "INCENTIVE"):
                price    = tp.get("price") or tp.get("priceCurrency")
                currency = tp.get("priceCurrency", "EUR")
                break
        if price is None and price_list:
            price    = price_list[0].get("price")
            currency = price_list[0].get("priceCurrency", "EUR")

        # Транзитний час
        dc = p.get("deliveryCapabilities") or {}
        transit_days  = dc.get("totalTransitDays")
        delivery_date = (dc.get("estimatedDeliveryDateAndTime") or "")[:10]

        products.append({
            "name":          name,
            "code":          code,
            "price":         float(price) if price is not None else None,
            "currency":      currency,
            "transit_days":  transit_days,
            "delivery_date": delivery_date,
        })

    # Сортуємо від найдешевшого
    products.sort(key=lambda x: x["price"] if x["price"] is not None else 9999)

    return {"products": products, "error": None}


def get_tracking(carrier, tracking_number: str) -> dict:
    """
    GET /shipments/{trackingNumber}/tracking — статус і події трекінгу.

    Повертає dict:
      {
        "status":        "delivered",
        "description":   "Delivered - Signed for by: JOHN DOE",
        "timestamp":     "2026-03-09T10:00:00",
        "location":      "Warsaw, PL",
        "estimated_delivery": "2026-03-09",
        "proof_of_delivery":  "JOHN DOE",
        "events": [
          {
            "timestamp":   "2026-03-09T10:00:00",
            "location":    "Warsaw, PL",
            "description": "Delivered",
          },
          ...
        ],
        "error": None або str,
      }
    """
    import requests as req

    use_test = (carrier.api_url or "").strip().lower() == "test"
    base     = _TEST_BASE if use_test else _PROD_BASE

    try:
        from tabele.api_logger import logged_request
        resp = logged_request('dhl', 'get_tracking', 'GET',
                              f"{base}/shipments/{tracking_number}/tracking", req.get,
                              params={"trackingView": "all-checkpoints"},
                              auth=(carrier.api_key, carrier.api_secret),
                              headers={"Accept": "application/json"},
                              timeout=20)
    except req.exceptions.Timeout:
        return {"events": [], "error": "Timeout — DHL API не відповідає"}
    except Exception as e:
        return {"events": [], "error": str(e)}

    if not resp.ok:
        body = {}
        try:
            body = resp.json()
        except Exception:
            pass
        detail = (body.get("detail")
                  or body.get("message")
                  or body.get("title")
                  or resp.text[:300])
        return {"events": [], "error": f"HTTP {resp.status_code}: {detail}"}

    try:
        data = resp.json()
    except Exception as e:
        return {"events": [], "error": f"JSON parse error: {e}"}

    shipments = data.get("shipments") or []
    if not shipments:
        use_test = (carrier.api_url or "").strip().lower() == "test"
        hint = " (Carrier у Sandbox режимі — для реальних номерів очисти поле API URL)" if use_test else \
               " (MyDHL API трекає тільки DHL Express відправлення, не DHL Paket/Post)"
        return {"events": [], "error": f"Номер не знайдено.{hint}"}

    s = shipments[0]

    def _loc(loc_obj):
        if not loc_obj:
            return ""
        addr = loc_obj.get("address") or {}
        city    = addr.get("addressLocality", "")
        country = addr.get("countryCode", "")
        return ", ".join(filter(None, [city, country]))

    events = []
    for ev in (s.get("events") or []):
        events.append({
            "timestamp":   ev.get("timestamp", ""),
            "location":    _loc(ev.get("location")),
            "description": ev.get("description", ""),
        })

    return {
        "status":             s.get("status", ""),
        "description":        s.get("description", ""),
        "timestamp":          s.get("timestamp", ""),
        "location":           _loc(s.get("location")),
        "estimated_delivery": (s.get("estimatedDeliveryTime") or "")[:10],
        "proof_of_delivery":  s.get("proofOfDeliverySignatory", ""),
        "events":             events,
        "error":              None,
    }


_EU_COUNTRIES = {
    'AT', 'BE', 'BG', 'CY', 'CZ', 'DE', 'DK', 'EE', 'ES', 'FI', 'FR',
    'GR', 'HR', 'HU', 'IE', 'IT', 'LT', 'LU', 'LV', 'MT', 'NL', 'PL',
    'PT', 'RO', 'SE', 'SI', 'SK',
}

_EXPORT_REASON_MAP = {
    "Commercial": "COMMERCIAL_PURPOSE_OR_SALE",
    "Gift":       "GIFT",
    "Personal":   "PERSONAL_BELONGINGS_OR_PERSONAL_USE",
    "Return":     "RETURN",
    "Claim":      "OTHER",
}


def create_shipment(carrier, shipment, product_code: str,
                    product_name: str = "", price: float = 0.0,
                    request_pickup: bool = False,
                    pickup_date: str = "",
                    pickup_ready_time: str = "09:00",
                    pickup_close_time: str = "18:00",
                    pickup_location: str = "reception",
                    include_customs: bool | None = None,
                    dry_run: bool = False) -> dict:
    """
    POST /shipments — створює відправлення DHL Express.

    carrier.api_key        = DHL API Username
    carrier.api_secret     = DHL API Password
    carrier.connection_uuid = DHL Account Number
    carrier.api_url        = "test" або пусто (production)

    Повертає dict:
      {
        "success":       bool,
        "tracking_number": str,
        "label_bytes":   bytes | None,  # decoded PDF
        "carrier_service": str,
        "carrier_price": float,
        "raw_request":   dict,
        "raw_response":  dict,
        "error":         None або str,
      }
    """
    import base64
    import requests as req
    from datetime import datetime, timedelta, date as _date

    use_test = (carrier.api_url or "").strip().lower() == "test"
    base     = _TEST_BASE if use_test else _PROD_BASE

    # Дата відправлення: з форми або наступний робочий день
    if pickup_date:
        try:
            ship_date = _date.fromisoformat(pickup_date)
        except (ValueError, AttributeError):
            ship_date = _date.today() + timedelta(days=1)
            while ship_date.weekday() >= 5:
                ship_date += timedelta(days=1)
    else:
        ship_date = _date.today() + timedelta(days=1)
        while ship_date.weekday() >= 5:          # 5=сб, 6=нд
            ship_date += timedelta(days=1)

    # Час готовності: з форми або 10:00
    try:
        _rh, _rm = (int(x) for x in (pickup_ready_time or "09:00").split(":")[:2])
    except (ValueError, AttributeError):
        _rh, _rm = 9, 0

    # Будуємо рядок часу з timezone Europe/Berlin
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Berlin")
        ship_dt   = datetime(ship_date.year, ship_date.month, ship_date.day, _rh, _rm, 0, tzinfo=tz)
        offset    = ship_dt.strftime("%z")          # "+0100" or "+0200"
        offset_f  = f"{offset[:3]}:{offset[3:]}"   # "+01:00"
        planned_dt = f"{ship_date.isoformat()}T{_rh:02d}:{_rm:02d}:00 GMT{offset_f}"
    except Exception:
        planned_dt = f"{ship_date.isoformat()}T{_rh:02d}:{_rm:02d}:00 GMT+01:00"

    acct         = carrier.connection_uuid or ""
    dest_country = (shipment.recipient_country or "").upper()
    is_customs   = dest_country not in _EU_COUNTRIES
    # Явний override з форми (чекбокс)
    if include_customs is not None:
        is_customs = include_customs

    # shipment.sender_* має пріоритет над carrier.sender_* (якщо заповнені у формі)
    _sname    = shipment.sender_name    or carrier.sender_name    or carrier.sender_company or "Sender"
    _scompany = shipment.sender_company or carrier.sender_company or carrier.sender_name    or "Company"
    _sstreet  = shipment.sender_street  or carrier.sender_street  or ""
    _scity    = shipment.sender_city    or carrier.sender_city    or ""
    _szip     = shipment.sender_zip     or carrier.sender_zip     or ""
    _scountry = shipment.sender_country or carrier.sender_country or "DE"
    sender_name    = _sname
    sender_company = _scompany
    recv_name      = shipment.recipient_name or shipment.recipient_company or "Recipient"
    recv_phone     = shipment.recipient_phone or "+4900000000"
    send_phone     = shipment.sender_phone or carrier.sender_phone or "+4900000000"

    # ── Пакети: multi-package якщо є ShipmentPackage, інакше — один з полів Shipment ──
    pkg_qs = list(shipment.packages.order_by("pk")) if hasattr(shipment, "packages") else []
    if pkg_qs:
        dhl_packages = []
        for pkg in pkg_qs:
            box = {
                "weight": round(float(pkg.weight_kg or 1), 3),
                "dimensions": {
                    "length": int(pkg.length_cm or 20),
                    "width":  int(pkg.width_cm  or 15),
                    "height": int(pkg.height_cm or 10),
                },
            }
            for _ in range(max(1, int(pkg.quantity or 1))):
                dhl_packages.append(box.copy())
    else:
        dhl_packages = [{
            "weight": round(float(shipment.weight_kg or 1), 3),
            "dimensions": {
                "length": int(shipment.length_cm or 20),
                "width":  int(shipment.width_cm  or 15),
                "height": int(shipment.height_cm or 10),
            },
        }]

    payload: dict = {
        "plannedShippingDateAndTime": planned_dt,
        "pickup": {
            "isRequested": request_pickup,
            **({"readyTime": pickup_ready_time, "closeTime": pickup_close_time, "location": pickup_location}
               if request_pickup else {}),
        },
        "productCode": product_code,
        "accounts":    [{"typeCode": "shipper", "number": acct}] if acct else [],
        "customerDetails": {
            "shipperDetails": {
                "postalAddress": {
                    "postalCode":  _norm_postal(_szip, _scountry or "DE"),
                    "cityName":    _scity,
                    "countryCode": _scountry or "DE",
                    **_split_address(_sstreet),
                },
                "contactInformation": {
                    "fullName":    sender_name,
                    "companyName": sender_company,
                    "phone":       send_phone,
                    "email":       carrier.sender_email or "",
                },
            },
            "receiverDetails": {
                "postalAddress": {
                    "postalCode":  _norm_postal(shipment.recipient_zip, dest_country),
                    "cityName":    shipment.recipient_city or "",
                    "countryCode": dest_country,
                    **_split_address(shipment.recipient_street),
                },
                "contactInformation": {
                    "fullName":    recv_name,
                    "companyName": shipment.recipient_company or "",
                    "phone":       recv_phone,
                    "email":       shipment.recipient_email   or "",
                },
            },
        },
        "content": {
            "packages": dhl_packages,
            "isCustomsDeclarable":   is_customs,
            "description":           (shipment.description or "Goods")[:35],
            "incoterm":              "DAP",
            "unitOfMeasurement":     "metric",
            "declaredValue":         round(float(shipment.declared_value or 0), 2),
            "declaredValueCurrency": shipment.declared_currency or "EUR",
        },
        "outputImageProperties": {
            "printerDPI":     300,
            "encodingFormat": "pdf",
            "imageOptions": [
                {
                    "typeCode":     "label",
                    "templateName": "ECOM26_84_001",
                    "isRequested":  True,
                }
            ],
        },
    }

    # Референс замовлення
    if shipment.reference:
        payload["customerReferences"] = [
            {"typeCode": "CU", "value": str(shipment.reference)[:35]}
        ]

    # Митна декларація (non-EU)
    if is_customs and shipment.customs_articles:
        items = (shipment.customs_articles.get("customs_line_items") or [])
        export_items = []
        total_weight = round(float(shipment.weight_kg or 1), 3)
        n_items = len(items) or 1
        weight_per_item = round(max(0.1, total_weight / n_items), 3)

        for idx, item in enumerate(items, start=1):
            li: dict = {
                "number":      idx,
                "description": (item.get("description") or "Goods")[:35],
                "price":       round(float(item.get("value") or 0), 2),
                "priceCurrency": item.get("currency", "EUR"),
                "quantity": {
                    "unitOfMeasurement": "PCS",
                    "value": int(item.get("quantity") or 1),
                },
                "weight": {
                    "netValue":   weight_per_item,
                    "grossValue": weight_per_item,
                },
                "commodityCodes":   [],
                "exportReasonType": "permanent",
                "manufacturerCountry": (item.get("origin_country") or "DE").upper()[:2],
            }
            hs = (item.get("customs_number") or "").strip()
            if hs:
                li["commodityCodes"] = [{"typeCode": "outbound", "value": hs}]
            export_items.append(li)

        if export_items:
            payload["content"]["exportDeclaration"] = {
                "lineItems": export_items,
                "invoice": {
                    "number": str(shipment.reference or shipment.pk)[:35],
                    "date":   ship_date.isoformat(),
                },
                "exportReason":      _EXPORT_REASON_MAP.get(
                    shipment.export_reason, "COMMERCIAL_PURPOSE_OR_SALE"
                ),
                "additionalCharges": [],
                "placeOfIncoterm":   _scity or "Frankfurt",
            }

    if dry_run:
        return payload

    try:
        from tabele.api_logger import logged_request
        resp = logged_request('dhl', 'create_shipment', 'POST', f"{base}/shipments", req.post,
                              json=payload,
                              auth=(carrier.api_key, carrier.api_secret),
                              headers={"Accept": "application/json", "Content-Type": "application/json"},
                              timeout=30)
    except req.exceptions.Timeout:
        return {"success": False, "error": "Timeout — DHL API не відповідає",
                "raw_request": payload, "raw_response": None}
    except Exception as e:
        return {"success": False, "error": str(e),
                "raw_request": payload, "raw_response": None}

    try:
        data = resp.json()
    except Exception:
        data = {"raw_text": resp.text[:3000]}

    if not resp.ok:
        title = (data.get("title") or data.get("message") or "Error")
        # Витягуємо additionalDetails — масив конкретних помилок від DHL
        extras = data.get("additionalDetails") or []
        if isinstance(extras, list) and extras:
            lines = []
            for item in extras:
                if isinstance(item, dict):
                    msg  = item.get("message") or item.get("detail") or str(item)
                    path = item.get("path") or item.get("name") or ""
                    lines.append(f"• {path}: {msg}" if path else f"• {msg}")
                else:
                    lines.append(f"• {item}")
            detail = title + "\n" + "\n".join(lines)
        elif data.get("detail"):
            detail = f"{title} — {data['detail']}"
        else:
            detail = title
        return {"success": False, "error": f"HTTP {resp.status_code}: {detail}",
                "raw_request": payload, "raw_response": data}

    tracking_number = data.get("shipmentTrackingNumber", "")

    # Витягуємо base64 PDF label
    label_bytes = None
    for doc in (data.get("documents") or []):
        if doc.get("typeCode") == "label":
            b64 = doc.get("content", "")
            if b64:
                try:
                    label_bytes = base64.b64decode(b64)
                except Exception:
                    pass
            break

    return {
        "success":         True,
        "tracking_number": tracking_number,
        "label_bytes":     label_bytes,
        "carrier_service": product_name or product_code,
        "carrier_price":   price,
        "raw_request":     payload,
        "raw_response":    data,
        "error":           None,
    }


def cancel_shipment(carrier, tracking_number: str) -> dict:
    """
    DELETE /shipments/{trackingNumber} — скасування відправлення DHL Express.
    Пробує production endpoint першим (реальний трекінг = реальна посилка).
    Повертає {'success': bool, 'message': str, 'url': str}.
    """
    import requests as req

    # Реальний трекінг завжди скасовуємо через production.
    # Якщо carrier в test-режимі — все одно пробуємо production першим.
    use_test = (carrier.api_url or "").strip().lower() == "test"
    candidates = [_PROD_BASE]
    if use_test:
        candidates.append(_TEST_BASE)   # fallback для тестових номерів

    last_status = None
    last_msg    = ""
    last_url    = ""

    for base in candidates:
        url = f"{base}/shipments/{tracking_number}"
        last_url = url
        try:
            from tabele.api_logger import logged_request
            resp = logged_request('dhl', 'cancel_shipment', 'DELETE', url, req.delete,
                                  auth=(carrier.api_key, carrier.api_secret),
                                  headers={
                                      "Accept": "application/json",
                                      "Message-Reference": f"cancel-{tracking_number}",
                                      "Message-Reference-Date": __import__('datetime').datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S GMT+00:00"),
                                  },
                                  timeout=15)
        except Exception as e:
            return {"success": False, "message": str(e), "url": url}

        if resp.status_code in (200, 204):
            return {"success": True, "message": "Відправлення скасовано у DHL.", "url": url}

        last_status = resp.status_code
        try:
            data    = resp.json()
            last_msg = data.get("detail") or data.get("message") or data.get("title") or resp.text[:300]
        except Exception:
            last_msg = resp.text[:300]

    if last_status == 405:
        last_msg = (
            "DHL повернув 405 — посилка або вже передана кур'єру, або акаунт не має права скасовувати через API. "
            f"URL: {last_url}"
        )
    elif last_status == 404:
        last_msg = f"Посилка {tracking_number} не знайдена в DHL (можливо вже скасована або неправильний трекінг)."

    return {"success": False, "message": f"HTTP {last_status}: {last_msg}", "url": last_url}


def test_connection(carrier) -> dict:
    """
    Перевіряє підключення до DHL API.
    Використовує GET /rates з мінімальними sandbox-параметрами.
    Повертає {'ok': bool, 'message': str, 'mode': str}.
    """
    use_test = (carrier.api_url or "").strip().lower() == "test"
    mode     = "Sandbox" if use_test else "Production"

    result = get_rates(
        carrier=carrier,
        destination_country="PL",
        destination_postal="00-001",
        destination_city="Warsaw",
        weight_kg=1.0,
        length_cm=20,
        width_cm=15,
        height_cm=10,
    )
    if result.get("error"):
        return {"ok": False, "message": f"❌ {result['error']}", "mode": mode}

    count = len(result.get("products") or [])
    return {
        "ok":      True,
        "message": f"✅ Підключено ({mode}). Отримано тарифів: {count}.",
        "mode":    mode,
    }
