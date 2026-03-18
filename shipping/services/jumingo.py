"""
shipping/services/jumingo.py — Інтеграція з Jumingo API v1
https://api.jumingo.com/v1  |  Auth: X-AUTH-TOKEN header

Flow:
  1. create_shipment()  → POST /v1/shipments    → carrier_shipment_id
  2. get_rates()        → POST /v1/shipment-rates → список тарифів з цінами
  3. Користувач обирає тариф і оплачує вручну на app.jumingo.com
  4. track()            → GET  /v1/shipments/{id} → tracking_number + status
"""
import logging
from datetime import date, timedelta

from .base import BaseCarrierService, ShipmentResult

logger = logging.getLogger(__name__)

JUMINGO_API_BASE = "https://api.jumingo.com/v1"
JUMINGO_APP_URL  = "https://www.jumingo.com"

# Мапування Jumingo tracking.status → Minerva Shipment.Status
TRACKING_STATUS_MAP = {
    "transit":     "in_transit",
    "pickup":      "in_transit",
    "undelivered": "in_transit",
    "delivered":   "delivered",
    "exception":   "error",
    "expired":     "error",
}

# Мапування Jumingo shipment.status → Minerva Shipment.Status
SHIPMENT_STATUS_MAP = {
    "completed":   "label_ready",
    "in_transit":  "in_transit",
    "in_delivery": "in_transit",
    "delivered":   "delivered",
}


def build_customs_articles(order, sender_country="DE", default_currency="EUR") -> list:
    """Будує список митних артикулів з ліній замовлення.
    1 рядок замовлення = 1 рядок митної декларації.

    Пріоритет опису: product.name_export > category.customs_description_de > product.name > sku
    Пріоритет HS-коду: product.hs_code > category.customs_hs_code
    Пріоритет країни: product.country_of_origin > category.customs_country_of_origin > sender_country
    """
    try:
        from sales.models import SalesOrderLine
        from inventory.models import ProductCategory
        lines = list(
            SalesOrderLine.objects
            .filter(order=order)
            .select_related("product")
            .order_by("pk")
        )
    except Exception:
        return []

    if not lines:
        return []

    # Підвантажити всі категорії одним запитом
    cat_slugs = {l.product.category for l in lines if l.product and l.product.category}
    cat_map   = {
        c.slug: c for c in ProductCategory.objects.filter(slug__in=cat_slugs)
    } if cat_slugs else {}

    articles = []
    for line in lines:
        p   = line.product
        cat = cat_map.get(p.category) if p and p.category else None

        qty_int = max(1, int(float(line.qty or 1)))

        # Опис: name_export > customs_description_de > name > sku
        if p and p.name_export:
            desc = p.name_export[:35]
        elif cat and cat.customs_description_de:
            desc = cat.customs_description_de[:35]
        elif p and p.name:
            desc = p.name[:35]
        elif p:
            desc = (p.sku or "Goods")[:35]
        else:
            desc = "Goods"

        # HS-код: product > category
        hs = (p.hs_code if p else "") or (cat.customs_hs_code if cat else "") or ""

        # Країна: product > category > sender
        origin = (
            (p.country_of_origin if p else "")
            or (cat.customs_country_of_origin if cat else "")
            or sender_country
            or "DE"
        )

        # Вартість
        if line.total_price:
            value = float(line.total_price)
        elif line.unit_price:
            value = float(line.unit_price) * qty_int
        else:
            value = 0.0

        currency = (line.currency if line.currency else default_currency) or default_currency

        # Вага (сумарна на рядок)
        weight = None
        if p and p.net_weight_g:
            weight = round((float(p.net_weight_g) / 1000.0) * qty_int, 3)

        item = {
            "description":    desc,
            "quantity":       qty_int,
            "value":          round(value, 2),
            "currency":       currency,
            "origin_country": origin,
            "customs_number": hs,
        }
        if weight:
            item["weight"] = weight
        articles.append(item)

    return articles


class JumingoService(BaseCarrierService):
    """
    Сервіс для роботи з Jumingo API.
    Потребує: api_key = X-AUTH-TOKEN, connection_uuid в моделі Carrier.
    """

    def _base(self) -> str:
        return (self.carrier.api_url or JUMINGO_API_BASE).rstrip("/")

    def _headers(self) -> dict:
        return {
            "X-AUTH-TOKEN": self.carrier.api_key,
            "Content-Type": "application/json",
            "Accept":       "application/json",
        }

    # ── EU країни — не потребують customs_invoice ────────────────────────────
    _EU_COUNTRIES = {
        "AT","BE","BG","CY","CZ","DE","DK","EE","ES","FI","FR","GR","HR",
        "HU","IE","IT","LT","LU","LV","MT","NL","PL","PT","RO","SE","SI","SK",
    }

    # Маппінг export_reason → Jumingo customs invoice type
    _CUSTOMS_TYPE_MAP = {
        "Commercial": "commercial",
        "Gift":       "gift",
        "Sample":     "sample",
        "Return":     "return",
        "Claim":      "return",
        "Personal":   "private",
    }

    @staticmethod
    def _to_api_item(item: dict) -> dict:
        """Конвертує внутрішній формат артикулу → Jumingo API формат.

        Внутрішній формат (customs_articles):
          description, quantity, value, currency, origin_country, customs_number, weight

        Jumingo API формат (lineItems):
          content (required), quantity, unitOfMeasurement (required),
          value, hsTariffNumber, manufacturingCountry, netWeight
        """
        mapped = {
            "content":           (item.get("description") or "")[:35],
            "quantity":          int(item.get("quantity") or 1),
            "unitOfMeasurement": "PCS",   # required; завжди штуки для електроніки
            "value":             float(item.get("value") or 0),
        }
        if item.get("customs_number"):
            mapped["hsTariffNumber"] = item["customs_number"]
        if item.get("origin_country"):
            mapped["manufacturingCountry"] = item["origin_country"]
        if item.get("weight"):
            mapped["netWeight"] = round(float(item["weight"]), 3)
        return mapped

    def _build_customs_invoice(self, shipment) -> dict | None:
        """Формує customs_invoice для позаєвропейських відправлень.

        Структура API (v1.0.3):
          customs_invoice.currency      — required
          customs_invoice.exportReason  — required: Commercial/Gift/Personal/Return/Claim
          customs_invoice.lineItems[]   — required
            .content, .quantity, .unitOfMeasurement, .value  — required
            .hsTariffNumber, .manufacturingCountry, .netWeight — optional

        Пріоритет: customs_articles (з форми) → автогенерація з ліній замовлення.
        """
        country = (shipment.recipient_country or "").upper()
        if country in self._EU_COUNTRIES or not country:
            return None

        currency     = shipment.declared_currency or "EUR"
        export_reason = shipment.export_reason or "Commercial"
        # API приймає: Commercial, Gift, Personal, Return, Claim (з великої літери)
        if export_reason not in ("Commercial", "Gift", "Personal", "Return", "Claim"):
            export_reason = "Commercial"

        # ── Пріоритет 1: збережені артикули з форми ──────────────────────────
        stored = getattr(shipment, "customs_articles", None)
        if stored:
            items = stored.get("customs_line_items") or stored.get("articles") or []
            if items:
                # currency з форми має пріоритет якщо є
                inv_currency = stored.get("currency") or currency
                return {
                    "currency":     inv_currency,
                    "exportReason": export_reason,
                    "lineItems":    [self._to_api_item(it) for it in items],
                }

        # ── Пріоритет 2: автогенерація з ліній замовлення ────────────────────
        items = build_customs_articles(
            shipment.order,
            sender_country=self.carrier.sender_country or "DE",
            default_currency=currency,
        )
        if not items:
            return None
        return {
            "currency":     currency,
            "exportReason": export_reason,
            "lineItems":    [self._to_api_item(it) for it in items],
        }

    # Країни де state обов'язковий (і не дозволений для інших)
    _STATE_REQUIRED = {"US", "CA"}

    def _build_payload(self, shipment) -> dict:
        c = self.carrier
        dest_country = (shipment.recipient_country or "").upper()

        declared_value = max(1, int(float(shipment.declared_value or 0)))

        details = {
            "value_amount":          declared_value,
            # value_currency DEPRECATED — currency тепер в customs_invoice.currency
            "content_description":   (shipment.description or "Goods")[:35],
            "reference_number":      (shipment.reference or "")[:35],
            "email":                 shipment.recipient_email or c.sender_email or "",
            "export_license":        False,
            "packaging_type":        "parcel",
            # Страхування: передаємо задекларовану вартість як страховий номінал
            "extra_insurance_value": declared_value,
            "extra_insurance_type":  "standard",
            "settings": {
                "export_reason": shipment.export_reason or "Commercial",
            },
        }

        # customs_invoice — переноситься на верхній рівень payload (не в details)
        customs_invoice = self._build_customs_invoice(shipment)

        # to_address: state тільки для US/CA (для інших країн — заборонено!)
        to_address = {
            "name":    (shipment.recipient_name or "")[:35],
            "company": (shipment.recipient_company or "")[:35],
            "street":  (shipment.recipient_street or "")[:35],
            "city":    (shipment.recipient_city or "")[:35],
            "zip":     shipment.recipient_zip or "",
            "country": shipment.recipient_country or "",
            "phone":   shipment.recipient_phone or "",
            "settings": {"email": shipment.recipient_email or ""},
        }
        if dest_country in self._STATE_REQUIRED and shipment.recipient_state:
            to_address["state"] = shipment.recipient_state[:10]

        sender_country = (c.sender_country or "DE").upper()
        from_address = {
            "name":    (c.sender_name or "")[:35],
            "company": (c.sender_company or "")[:35],
            "street":  (c.sender_street or "")[:35],
            "city":    (c.sender_city or "")[:35],
            "zip":     c.sender_zip or "",
            "country": c.sender_country or "DE",
            "phone":   c.sender_phone or "",
            "settings": {"email": c.sender_email or ""},
        }
        if sender_country in self._STATE_REQUIRED and getattr(c, "sender_state", ""):
            from_address["state"] = c.sender_state[:10]

        payload = {
            "from_address": from_address,
            "to_address":   to_address,
            "packages": [{
                "weight": max(0.1, float(shipment.weight_kg or 0.1)),
                "length": int(shipment.length_cm) if shipment.length_cm else 10,
                "width":  int(shipment.width_cm)  if shipment.width_cm  else 10,
                "height": int(shipment.height_cm) if shipment.height_cm else 10,
            }],
            "details": details,
        }
        if customs_invoice:
            payload["customs_invoice"] = customs_invoice
        if c.connection_uuid:
            payload["connection"] = {"connection_uuid": c.connection_uuid}
        return payload

    # ── Створення відправлення ────────────────────────────────────────────────

    def create_shipment(self, shipment) -> ShipmentResult:
        """POST /v1/shipments — створює відправлення, повертає carrier_shipment_id."""
        import requests as req

        payload = self._build_payload(shipment)
        try:
            resp = req.post(
                f"{self._base()}/shipments",
                headers=self._headers(),
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return ShipmentResult(
                success=True,
                carrier_shipment_id=str(data.get("shipment_id") or data.get("id", "")),
                tracking_number="",   # з'явиться після оплати
                label_url="",          # з'явиться після оплати
                raw_request=payload,
                raw_response=data,
            )
        except req.HTTPError as e:
            raw = {}
            try:
                raw = e.response.json()
            except Exception:
                pass
            msg = raw.get("message") or raw.get("error") or str(e)
            logger.error("Jumingo create_shipment HTTP error: %s | body: %s", e, raw)
            return ShipmentResult(success=False, error_message=msg, raw_response=raw)
        except req.RequestException as e:
            logger.error("Jumingo create_shipment error: %s", e)
            return ShipmentResult(success=False, error_message=str(e))

    # ── Отримання тарифів ─────────────────────────────────────────────────────

    def get_rates(self, carrier_shipment_id: str) -> dict:
        """POST /v1/shipment-rates — повертає список тарифів для відправлення."""
        import requests as req

        today    = date.today()
        tomorrow = today + timedelta(days=1)
        payload  = {
            "shipmentId":    carrier_shipment_id,
            "pickup_date":   today.strftime("%Y-%m-%dT12:00:00"),
            "delivery_date": tomorrow.strftime("%Y-%m-%dT12:00:00"),
            "settings":      {"mode": "m"},
        }
        try:
            resp = req.post(
                f"{self._base()}/shipment-rates",
                headers=self._headers(),
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            # API повертає список тарифів напряму
            if isinstance(data, list):
                return {"tariffs": data}
            # Якщо раптом прийшов словник — повернути як є
            return data if isinstance(data, dict) else {"tariffs": []}
        except req.HTTPError as e:
            raw = {}
            try:
                raw = e.response.json()
            except Exception:
                pass
            logger.error("Jumingo get_rates HTTP error: %s | body: %s", e, raw)
            detail = raw.get("message") or raw.get("error") or str(raw) or str(e)
            return {"error": f"{e} | {detail}", "tariffs": [], "raw": raw}
        except Exception as e:
            logger.error("Jumingo get_rates error: %s", e)
            return {"error": str(e), "tariffs": []}

    def get_rates_preview(self, dest_country: str, dest_postal: str, dest_city: str,
                          weight_kg: float, length_cm: int, width_cm: int, height_cm: int,
                          dest_name: str = "Recipient") -> dict:
        """
        Тимчасово створює відправлення на Jumingo (без збереження в Minerva DB),
        отримує тарифи і повертає їх.

        Повертає dict:
          {
            "tariffs":    [...],  # Jumingo tariff list
            "preview_id": str,   # тимчасовий Jumingo shipment_id
            "error":      None або str,
          }
        """
        import requests as req

        c = self.carrier
        payload = {
            "from_address": {
                "name":    c.sender_name or "Sender",
                "company": c.sender_company or "",
                "street":  c.sender_street or "Main St 1",
                "city":    c.sender_city or "City",
                "zip":     c.sender_zip or "00000",
                "country": c.sender_country or "DE",
                "phone":   c.sender_phone or "+4900000000",
                "settings": {"email": c.sender_email or ""},
            },
            "to_address": {
                "name":    dest_name or "Recipient",
                "company": "",
                "street":  "Main St 1",
                "city":    dest_city or "City",
                "zip":     dest_postal or "00000",
                "country": dest_country,
                "phone":   "+4900000000",
                "settings": {"email": ""},
            },
            "packages": [{
                "weight": max(0.1, float(weight_kg or 1)),
                "length": int(length_cm) if length_cm else 20,
                "width":  int(width_cm)  if width_cm  else 15,
                "height": int(height_cm) if height_cm else 10,
            }],
            "details": {
                "value_amount":        1,
                "value_currency":      "EUR",
                "content_description": "Goods",
                "reference_number":    "PREVIEW",
                "email":               c.sender_email or "",
                "export_license":      False,
                "packaging_type":      "parcel",
                "settings":            {"export_reason": "Commercial"},
            },
        }
        if c.connection_uuid:
            payload["connection"] = {"connection_uuid": c.connection_uuid}

        # Крок 1: Тимчасове відправлення
        try:
            r1 = req.post(f"{self._base()}/shipments",
                          headers=self._headers(), json=payload, timeout=30)
            r1.raise_for_status()
            d1 = r1.json()
        except req.HTTPError as e:
            raw = {}
            try: raw = e.response.json()
            except Exception: pass
            return {"tariffs": [], "preview_id": "",
                    "error": raw.get("message") or raw.get("error") or str(e)}
        except Exception as e:
            return {"tariffs": [], "preview_id": "", "error": str(e)}

        preview_id = str(d1.get("shipment_id") or d1.get("id") or "")
        if not preview_id:
            return {"tariffs": [], "preview_id": "", "error": "Jumingo не повернув shipment_id"}

        # Крок 2: Тарифи
        today    = date.today()
        tomorrow = today + timedelta(days=1)
        rates_payload = {
            "shipmentId":    preview_id,
            "pickup_date":   today.strftime("%Y-%m-%dT12:00:00"),
            "delivery_date": tomorrow.strftime("%Y-%m-%dT12:00:00"),
            "settings":      {"mode": "m"},
        }
        try:
            r2 = req.post(f"{self._base()}/shipment-rates",
                          headers=self._headers(), json=rates_payload, timeout=30)
            r2.raise_for_status()
            d2 = r2.json()
        except req.HTTPError as e:
            raw = {}
            try: raw = e.response.json()
            except Exception: pass
            err = raw.get("message") or raw.get("error") or str(e)
            return {"tariffs": [], "preview_id": preview_id, "error": err}
        except Exception as e:
            return {"tariffs": [], "preview_id": preview_id, "error": str(e)}

        tariffs = d2 if isinstance(d2, list) else d2.get("tariffs", [])
        return {"tariffs": tariffs, "preview_id": preview_id, "error": None}

    # ── Видалення ─────────────────────────────────────────────────────────────

    def delete_shipment(self, carrier_shipment_id: str) -> bool:
        """DELETE /v1/shipments/{id} — видаляє неоплачене відправлення з Jumingo.
        Повертає True якщо успішно (або вже не існує), False при помилці.
        """
        import requests as req
        try:
            resp = req.delete(
                f"{self._base()}/shipments/{carrier_shipment_id}",
                headers=self._headers(),
                timeout=15,
            )
            # 200/204 — успішно видалено; 404 — вже не існує (теж ок)
            return resp.status_code in (200, 204, 404)
        except Exception as e:
            logger.warning("Jumingo delete_shipment %s error: %s", carrier_shipment_id, e)
            return False

    # ── Трекінг ───────────────────────────────────────────────────────────────

    def track(self, carrier_shipment_id: str) -> dict:
        """GET /v1/shipments/{id} — повертає статус та трекінг відправлення."""
        import requests as req

        try:
            resp = req.get(
                f"{self._base()}/shipments/{carrier_shipment_id}",
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Jumingo track error for %s: %s", carrier_shipment_id, e)
            return {}

    # ── Вибір тарифу ─────────────────────────────────────────────────────────

    @staticmethod
    def _norm_time(t: str) -> str:
        """Нормалізує рядок часу до HH:MM:SS.
        '09:00' → '09:00:00', '09:00:00' → '09:00:00', '' → '00:00:00'
        """
        if not t:
            return "00:00:00"
        parts = t.strip().split(":")
        if len(parts) == 2:
            return f"{parts[0]:0>2}:{parts[1]:0>2}:00"
        if len(parts) == 3:
            return f"{parts[0]:0>2}:{parts[1]:0>2}:{parts[2]:0>2}"
        return "00:00:00"

    def patch_tariff(self, carrier_shipment_id: str, tariff_id: str, pickup_date: str,
                     pickup_min_time: str = "09:00:00", pickup_max_time: str = "18:00:00") -> dict:
        """PATCH /v1/shipments/{id} — призначає тариф на відправлення.

        Shop tariffs (id starts with 's-') use shipping_type='shop' and times '00:00:00'.
        Pickup tariffs use shipping_type='pickup' with the given time window.

        pickup_date: 'YYYY-MM-DD' — конвертується в ISO 8601 UTC datetime
        pickup_min/max_time: 'HH:MM' або 'HH:MM:SS' — нормалізується до HH:MM:SS
        """
        import requests as req

        is_shop = str(tariff_id).startswith("s-")

        # Pickup_date: API вимагає повний ISO 8601 datetime UTC
        if "T" not in str(pickup_date):
            pickup_date_iso = f"{pickup_date}T00:00:00Z"
        else:
            pickup_date_iso = str(pickup_date)

        min_t = "00:00:00" if is_shop else self._norm_time(pickup_min_time)
        max_t = "00:00:00" if is_shop else self._norm_time(pickup_max_time)

        headers = self._headers()
        headers["Content-Type"] = "application/merge-patch+json"
        payload = {
            "rate": {
                "shipper_tariff_id": tariff_id,
                "shipping_type":     "shop" if is_shop else "pickup",
                "pickup_date":       pickup_date_iso,
                "pickup_min_time":   min_t,
                "pickup_max_time":   max_t,
            }
        }
        try:
            resp = req.patch(
                f"{self._base()}/shipments/{carrier_shipment_id}",
                headers=headers,
                json=payload,
                timeout=20,
            )
            resp.raise_for_status()
            return {"success": True, "data": resp.json()}
        except req.HTTPError as e:
            raw = {}
            try:
                raw = e.response.json()
            except Exception:
                pass
            logger.error("Jumingo patch_tariff error: %s | %s", e, raw)
            return {"success": False, "error": raw.get("detail") or str(e)}
        except Exception as e:
            logger.error("Jumingo patch_tariff error: %s", e)
            return {"success": False, "error": str(e)}

    # ── Кошик / Оплата ───────────────────────────────────────────────────────

    def cart_total(self, carrier_shipment_id: str) -> dict:
        """POST /v1/cart/total — повертає суму і доступні методи оплати."""
        import requests as req

        try:
            resp = req.post(
                f"{self._base()}/cart/total",
                headers=self._headers(),
                json={"shipmentIds": [carrier_shipment_id]},
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Jumingo cart_total error: %s", e)
            return {}

    def book_order(self, carrier_shipment_id: str, payment_method: str = "bill") -> dict:
        """POST /v1/orders — оформлює замовлення (оплата)."""
        import requests as req

        try:
            resp = req.post(
                f"{self._base()}/orders",
                headers=self._headers(),
                json={
                    "paymentMethod": payment_method,
                    "shipmentIds":   [carrier_shipment_id],
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except req.HTTPError as e:
            raw = {}
            try:
                raw = e.response.json()
            except Exception:
                pass
            logger.error("Jumingo book_order error: %s | %s", e, raw)
            return {"success": False, "error": raw.get("detail") or raw.get("message") or str(e)}
        except Exception as e:
            logger.error("Jumingo book_order error: %s", e)
            return {"success": False, "error": str(e)}

    def get_order_documents(self, order_number: str) -> dict:
        """GET /v1/orders/{id}/documents — повертає label URL та документи."""
        import requests as req

        try:
            resp = req.get(
                f"{self._base()}/orders/{order_number}/documents",
                headers=self._headers(),
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Jumingo get_order_documents error: %s", e)
            return {}
