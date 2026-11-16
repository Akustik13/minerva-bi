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
import re
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


def home_country(carrier) -> str:
    return (carrier.sender_country or "DE").upper()[:2]


def account_for(carrier, origin_country: str) -> tuple:
    """(номер акаунта DHL, вид) за країною відправника: з країни перевізника (DE) — Account № (експорт /
    Innerdeutsch), з-за кордону (посилка до нас або між двома іншими країнами) — «акаунт імпорту / третьої
    країни», якщо вписаний. Вид: export (з DE, і Innerdeutsch) | import (з-за кордону)."""
    home = home_country(carrier)
    origin = (origin_country or home).upper()[:2]
    export_acct = (carrier.connection_uuid or "").strip()
    if origin == home:
        return export_acct, "export"
    import_acct = (getattr(carrier, "import_account", "") or "").strip()
    return (import_acct or export_acct), "import"


ACCOUNT_CHOICES = ("auto", "export", "import")


def account_choice(carrier, origin_country: str, choice: str = "auto") -> tuple:
    """(номер, вид) з вибору людини в порівнянні тарифів / оформленні: auto — `account_for` за країною
    відправника, export — Account № (Export + Innerdeutsch), import — акаунт імпорту / третьої країни
    (не вписаний — Account №)."""
    export_acct = (carrier.connection_uuid or "").strip()
    import_acct = (getattr(carrier, "import_account", "") or "").strip()
    if choice == "export":
        return export_acct, "export"
    if choice == "import":
        return (import_acct or export_acct), "import"
    return account_for(carrier, origin_country)


def customs_needed(origin_country: str, dest_country: str) -> bool:
    """Митна декларація потрібна, коли посилка перетинає кордон ЄС (з / до країни поза ЄС)."""
    o, d = (origin_country or "").upper()[:2], (dest_country or "").upper()[:2]
    if not o or not d or o == d:
        return False
    return not (o in _EU_COUNTRIES and d in _EU_COUNTRIES)


def _planned(ship_date, hour=10, minute=0) -> str:
    """Дата й час для DHL: «2026-11-17T10:00:00 GMT+01:00» (пояс Europe/Berlin)."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        off = datetime(ship_date.year, ship_date.month, ship_date.day, hour, minute,
                       tzinfo=ZoneInfo("Europe/Berlin")).strftime("%z")
        off = f"{off[:3]}:{off[3:]}"
    except Exception:  # noqa: BLE001
        off = "+01:00"
    return f"{ship_date.isoformat()}T{hour:02d}:{minute:02d}:00 GMT{off}"


def _rate_packages(packages, weight_kg, length_cm, width_cm, height_cm) -> list:
    """Коробки для /rates: [{weight_kg, length_cm, width_cm, height_cm, quantity}] → кожна окремо."""
    out = []
    for p in packages or []:
        try:
            box = {"weight": round(max(0.1, float(p.get("weight_kg") or 1)), 3),
                   "dimensions": {"length": max(1, int(float(p.get("length_cm") or 20))),
                                  "width": max(1, int(float(p.get("width_cm") or 15))),
                                  "height": max(1, int(float(p.get("height_cm") or 10)))}}
        except (TypeError, ValueError):
            continue
        out += [dict(box) for _ in range(max(1, int(p.get("quantity") or 1)))]
    if not out:
        out = [{"weight": round(max(0.1, float(weight_kg or 1)), 3),
                "dimensions": {"length": int(length_cm or 20), "width": int(width_cm or 15),
                               "height": int(height_cm or 10)}}]
    return out


def get_rates(carrier, destination_country: str, destination_postal: str,
              destination_city: str, weight_kg: float,
              length_cm: float, width_cm: float, height_cm: float,
              is_customs_declarable: bool = False,
              account_number: str = "", packages=None, origin=None) -> dict:
    """
    POST /rates — тарифи DHL Express для кожної коробки окремо (packages: [{weight_kg, length_cm, width_cm,
    height_cm, quantity}], без них — одна коробка з weight/length/width/height). origin {country, postal, city} —
    справжній відправник (посилка до нас), без нього — адреса перевізника. Акаунт — `account_for`, митниця —
    `customs_needed` (з / до країни поза ЄС).

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

    origin = origin or {}
    o_country = (origin.get("country") or home_country(carrier)).upper()[:2]
    o_postal = origin.get("postal") if origin.get("country") else carrier.sender_zip
    o_city = origin.get("city") if origin.get("country") else carrier.sender_city
    dest = (destination_country or "").upper()[:2]
    acct = account_number or account_for(carrier, o_country)[0]

    # Дата відправлення: наступний робочий день
    ship_date = date.today() + timedelta(days=1)
    while ship_date.weekday() >= 5:
        ship_date += timedelta(days=1)
    body = {
        "customerDetails": {
            "shipperDetails":  {"postalCode": _norm_postal(o_postal, o_country), "cityName": o_city or "",
                                "countryCode": o_country},
            "receiverDetails": {"postalCode": _norm_postal(destination_postal, dest),
                                "cityName": destination_city or "", "countryCode": dest},
        },
        "accounts": [{"typeCode": "shipper", "number": acct}] if acct else [],
        "plannedShippingDateAndTime": _planned(ship_date),
        "unitOfMeasurement": "metric",
        "isCustomsDeclarable": customs_needed(o_country, dest) or bool(is_customs_declarable and o_country != dest),
        "packages": _rate_packages(packages, weight_kg, length_cm, width_cm, height_cm),
    }

    try:
        from tabele.api_logger import logged_request
        resp = logged_request('dhl', 'get_rates', 'POST', f"{base}/rates", req.post,
                              json=body,
                              auth=(carrier.api_key, carrier.api_secret),
                              headers={"Accept": "application/json", "Content-Type": "application/json"},
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

    # Той самий продукт з тією самою ціною DHL інколи повертає двічі (різні дати / мережі) — лишаємо найшвидший
    best = {}
    for p in products:
        key = (p["code"], p["price"], p["currency"])
        old = best.get(key)
        if old is None or (p["transit_days"] or 99) < (old["transit_days"] or 99):
            best[key] = p
    products = list(best.values())
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
    "Sample":     "COMMERCIAL_PURPOSE_OR_SALE",  # DHL: no Sample, nearest = Commercial
    "Return":     "RETURN",
    "Repair":     "RETURN",                       # DHL: repair treated as Return
    "Personal":   "PERSONAL_BELONGINGS_OR_PERSONAL_USE",
    "Other":      "OTHER",
    "Claim":      "OTHER",                        # legacy compat
}


def create_shipment(carrier, shipment, product_code: str,
                    product_name: str = "", price: float = 0.0,
                    request_pickup: bool = False,
                    pickup_date: str = "",
                    pickup_ready_time: str = "09:00",
                    pickup_close_time: str = "18:00",
                    pickup_location: str = "reception",
                    include_customs: bool | None = None,
                    dry_run: bool = False, account: str = "auto") -> dict:
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

    dest_country = (shipment.recipient_country or "").upper()

    # shipment.sender_* має пріоритет над carrier.sender_* (якщо заповнені у формі)
    _sname    = shipment.sender_name    or carrier.sender_name    or carrier.sender_company or "Sender"
    _scompany = shipment.sender_company or carrier.sender_company or carrier.sender_name    or "Company"
    _sstreet  = shipment.sender_street  or carrier.sender_street  or ""
    _scity    = shipment.sender_city    or carrier.sender_city    or ""
    _szip     = shipment.sender_zip     or carrier.sender_zip     or ""
    _scountry = (shipment.sender_country or carrier.sender_country or "DE").upper()
    _semail   = shipment.sender_email   or carrier.sender_email   or ""

    # Акаунт: відправник у DE — експорт / Innerdeutsch, з-за кордону — імпорт / третя країна
    acct, acct_kind = account_choice(carrier, _scountry, account)
    is_customs = customs_needed(_scountry, dest_country)
    # Явний override з форми (чекбокс)
    if include_customs is not None:
        is_customs = include_customs
    customs_data = shipment.customs_articles or {}
    incoterm = (customs_data.get("incoterm") or "DAP").upper()[:3]
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
                    "email":       _semail,
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
            "incoterm":              incoterm,
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

        # Якщо є розподіл товарів по коробках, використати вагу з нього
        packages = list(shipment.packages.all()) if hasattr(shipment, 'packages') else []
        pkg_weight_map = {}  # {item_description: weight_per_item}

        if packages:
            for pkg in packages:
                if pkg.items_distribution:
                    dist = pkg.items_distribution
                    for dist_item in dist.get("items", []):
                        desc = dist_item.get("description", "")
                        if desc:
                            pkg_weight_map[desc] = dist.get("weight_per_item", weight_per_item)

        for idx, item in enumerate(items, start=1):
            # Вага рядка (DHL — за весь рядок): «Вага/шт (кг)» митної декларації × к-сть,
            # інакше з розподілу по коробках або загальна вага порівну між рядками
            item_desc = item.get("description", "")
            try:
                per_unit = float(item.get("weight") or 0)
            except (TypeError, ValueError):
                per_unit = 0.0
            if per_unit > 0 and not item.get("weight_auto"):
                item_weight = per_unit * max(1, int(item.get("quantity") or 1))
            else:
                item_weight = pkg_weight_map.get(item_desc, weight_per_item)
            item_weight = round(max(0.001, item_weight), 3)

            li: dict = {
                "number":      idx,
                "description": (item_desc or "Goods")[:35],
                "price":       round(float(item.get("value") or 0), 2),
                "priceCurrency": item.get("currency", "EUR"),
                "quantity": {
                    "unitOfMeasurement": "PCS",
                    "value": int(item.get("quantity") or 1),
                },
                "weight": {
                    "netValue":   item_weight,
                    "grossValue": item_weight,
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
            # Інвойс DHL (шаблон COMMERCIAL_INVOICE_P_10) — PDF у відповіді, зберігається в Minerva
            # (як митна декларація UPS) і йде в «Друкувати всі документи»
            payload["outputImageProperties"]["imageOptions"].append({
                "typeCode": "invoice", "templateName": "COMMERCIAL_INVOICE_P_10",
                "isRequested": True, "invoiceType": "commercial",
            })
            # Paperless Trade (WY): DHL бере інвойс в електронному вигляді, папір до посилки не потрібен.
            # Якщо акаунт не має цієї послуги — повтор без неї (нижче)
            payload["valueAddedServices"] = [{"serviceCode": "WY"}]

    if dry_run:
        return payload

    notes = []
    for attempt in range(2):
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
        # Акаунт без Paperless Trade (WY): DHL відмовляє (400 / 422) — ще раз без неї, інвойс лишається PDF
        if (attempt == 0 and not resp.ok and resp.status_code in (400, 422) and payload.get("valueAddedServices")
                and re.search(r"\bWY\b|paperless|value.?added", str(data), re.I)):
            payload.pop("valueAddedServices", None)
            notes.append("Paperless Trade (WY) для цього акаунта DHL не ввімкнено — інвойс треба роздрукувати "
                         "й покласти до посилки.")
            continue
        break

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

    # Документи у відповіді (base64 PDF): етикетка (одна на всі коробки) й інвойс DHL
    def _doc(type_code):
        for doc in (data.get("documents") or []):
            if doc.get("typeCode") == type_code and doc.get("content"):
                try:
                    return base64.b64decode(doc["content"])
                except Exception:  # noqa: BLE001
                    return None
        return None

    package_tracking = [p.get("trackingNumber") for p in (data.get("packages") or [])
                        if isinstance(p, dict) and p.get("trackingNumber")]

    return {
        "success":          True,
        "tracking_number":  tracking_number,
        "package_tracking": package_tracking,
        "label_bytes":      _doc("label"),
        "invoice_bytes":    _doc("invoice"),
        "account":          acct,
        "account_kind":     acct_kind,
        "paperless":        bool(payload.get("valueAddedServices")),
        "notes":            notes,
        "carrier_service":  product_name or product_code,
        "carrier_price":    price,
        "raw_request":      payload,
        "raw_response":     data,
        "error":            None,
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
