"""
FedEx REST API Client для Minerva BI.
OAuth 2.0 Client Credentials flow.
Credentials зберігаються в моделі Carrier:
  api_key        = Client ID
  api_secret     = Client Secret
  connection_uuid = Account Number
  api_url        = 'sandbox' | '' (production)
Токен кешується через Django cache framework (TTL 55 хв).

Документація: https://developer.fedex.com/api/en-us/catalog.html
Перевірені ендпоінти:
  Auth:   POST /oauth/token
  Rates:  POST /rate/v1/rates/quotes
  Ship:   POST /ship/v1/shipments
  Track:  POST /track/v1/trackingnumbers
  Cancel: PUT  /ship/v1/shipments/cancel
"""
import logging
from decimal import Decimal
from datetime import datetime, timezone as _tz

import requests
from django.core.cache import cache

logger = logging.getLogger('shipping.fedex')

# ── Service code → human name ──────────────────────────────────────────────────
FEDEX_SERVICES = {
    # Domestic USA
    'FEDEX_GROUND':              'FedEx Ground',
    'FEDEX_EXPRESS_SAVER':       'FedEx Express Saver',
    'FEDEX_2_DAY':               'FedEx 2Day',
    'FEDEX_2_DAY_AM':            'FedEx 2Day A.M.',
    'STANDARD_OVERNIGHT':        'FedEx Standard Overnight',
    'PRIORITY_OVERNIGHT':        'FedEx Priority Overnight',
    'FIRST_OVERNIGHT':           'FedEx First Overnight',
    # International / Europe (returned by sandbox API)
    'FEDEX_PRIORITY':                         'FedEx Priority',
    'FEDEX_PRIORITY_EXPRESS':                 'FedEx Priority Express',
    'FEDEX_PRIORITY_FREIGHT':                 'FedEx Priority Freight',
    'FEDEX_PRIORITY_EXPRESS_FREIGHT':         'FedEx Priority Express Freight',
    'FEDEX_ECONOMY':                          'FedEx Economy',
    'INTERNATIONAL_ECONOMY':                  'FedEx International Economy',
    'INTERNATIONAL_PRIORITY':                 'FedEx International Priority',
    'INTERNATIONAL_PRIORITY_EXPRESS':         'FedEx International Priority Express',
    'EUROPE_FIRST_INTERNATIONAL_PRIORITY':    'FedEx Europe First International Priority',
    'FEDEX_INTERNATIONAL_PRIORITY_EXPRESS':   'FedEx International Priority Express',
    'INTERNATIONAL_ECONOMY_FREIGHT':          'FedEx International Economy Freight',
    'INTERNATIONAL_PRIORITY_FREIGHT':         'FedEx International Priority Freight',
    # Europe intra
    'FEDEX_REGIONAL_ECONOMY':         'FedEx Regional Economy',
    'FEDEX_REGIONAL_ECONOMY_FREIGHT': 'FedEx Regional Economy Freight',
}

# Packaging type: YOUR_PACKAGING is the most permissive (customer's own box)
_PACKAGING_DEFAULT = 'YOUR_PACKAGING'

_BASE_SANDBOX    = 'https://apis-sandbox.fedex.com'
_BASE_PRODUCTION = 'https://apis.fedex.com'


class FedExError(Exception):
    def __init__(self, message, code=None, response=None):
        super().__init__(message)
        self.code = code
        self.response = response


def _get_fedex_carrier():
    """Знайти активний FedEx Carrier з credentials."""
    from shipping.models import Carrier
    c = (Carrier.objects
         .filter(carrier_type='fedex', is_active=True)
         .exclude(api_key='')
         .order_by('-is_default')
         .first())
    if not c:
        raise FedExError(
            'Немає активного FedEx перевізника. '
            'Додайте Carrier з типом FedEx і заповніть '
            'API ключ (Client ID), API Secret (Client Secret) і '
            'Account Number (Connection UUID).'
        )
    return c


class FedExClient:
    """
    Клієнт FedEx REST API. OAuth 2.0 Client Credentials.
    Передай carrier=Carrier.objects.get(...) або залиш None — буде взято автоматично.
    """

    def __init__(self, carrier=None):
        self.carrier = carrier or _get_fedex_carrier()
        c = self.carrier
        if not (c.api_key and c.api_secret and c.connection_uuid):
            raise FedExError(
                f'FedEx Carrier «{c.name}»: заповніть API ключ (Client ID), '
                'API Secret (Client Secret) і Connection UUID (Account Number).'
            )

    @property
    def _is_sandbox(self) -> bool:
        return (self.carrier.api_url or '').lower() in ('sandbox', 'test', 'staging', 'uat')

    @property
    def base_url(self) -> str:
        return _BASE_SANDBOX if self._is_sandbox else _BASE_PRODUCTION

    # ── Auth ───────────────────────────────────────────────────────────────────

    def get_token(self) -> str:
        """
        POST /oauth/token  (Client Credentials, form-encoded).
        Кешується на 55 хвилин (токен живе 60 хв).
        """
        cache_key = f'fedex_token_{self.carrier.pk}'
        token = cache.get(cache_key)
        if token:
            return token

        r = requests.post(
            f'{self.base_url}/oauth/token',
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            data={
                'grant_type':    'client_credentials',
                'client_id':     self.carrier.api_key,
                'client_secret': self.carrier.api_secret,
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise FedExError(
                f'Помилка аутентифікації FedEx [{r.status_code}]: {r.text[:300]}',
                code=r.status_code, response=r.text,
            )

        data = r.json()
        token = data.get('access_token', '')
        if not token:
            raise FedExError(f'FedEx auth: відсутній access_token у відповіді: {r.text[:200]}')

        # expires_in typically 3600 (60 min); cache with 5-min buffer
        expires_in = max(int(data.get('expires_in', 3600)) - 300, 60)
        cache.set(cache_key, token, expires_in)
        logger.info('FedEx OAuth token оновлено carrier=%s (ttl=%ss)', self.carrier.pk, expires_in)
        return token

    def _headers(self, extra: dict = None) -> dict:
        h = {
            'Authorization': f'Bearer {self.get_token()}',
            'Content-Type':  'application/json',
            'X-locale':      'en_US',
        }
        if extra:
            h.update(extra)
        return h

    def _post(self, endpoint: str, payload: dict) -> dict:
        url = f'{self.base_url}{endpoint}'
        try:
            r = requests.post(url, headers=self._headers(), json=payload, timeout=60)
            if r.status_code == 401:
                # Token expired — invalidate cache and retry once
                cache.delete(f'fedex_token_{self.carrier.pk}')
                r = requests.post(url, headers=self._headers(), json=payload, timeout=60)
            if not r.ok:
                self._handle_error(r)
            return r.json()
        except requests.Timeout:
            raise FedExError('FedEx API не відповідає (timeout 60с)')
        except requests.ConnectionError as e:
            raise FedExError(f"Помилка з'єднання з FedEx: {e}")

    def _put(self, endpoint: str, payload: dict) -> dict:
        url = f'{self.base_url}{endpoint}'
        try:
            r = requests.put(url, headers=self._headers(), json=payload, timeout=30)
            if not r.ok:
                self._handle_error(r)
            return r.json()
        except requests.Timeout:
            raise FedExError('FedEx API не відповідає')
        except requests.ConnectionError as e:
            raise FedExError(f"Помилка з'єднання: {e}")

    def _handle_error(self, response):
        try:
            data = response.json()
            errors = data.get('errors', [])
            if not errors:
                errors = data.get('output', {}).get('alerts', [])
            msg = '; '.join(
                e.get('message', '') or e.get('message', str(e))
                for e in errors[:3]
            ) if errors else response.text[:300]
        except Exception:
            msg = response.text[:300]
        raise FedExError(
            f'FedEx API [{response.status_code}]: {msg}',
            code=response.status_code,
            response=response.text,
        )

    # ── Rates ──────────────────────────────────────────────────────────────────

    def get_rates(self, to_address: dict, packages: list,
                  from_address: dict = None, service_code: str = None) -> list:
        """
        POST /rate/v1/rates/quotes
        to_address/from_address: {name, address_line, city, state, postal, country, phone}
        packages: [{weight_kg, length_cm, width_cm, height_cm}]
        service_code: якщо передано — отримати тариф лише для цього сервісу.
        Повертає: [{code, name, price, currency, transit_days, delivery_date, guaranteed}]
        """
        shipper = from_address or self._default_shipper()

        payload = {
            'accountNumber': {'value': self.carrier.connection_uuid},
            'requestedShipment': {
                'shipper': {
                    'address': self._fmt_addr(shipper),
                },
                'recipient': {
                    'address': self._fmt_addr(to_address),
                },
                'pickupType':                  'DROPOFF_AT_FEDEX_LOCATION',
                'rateRequestType':             ['ACCOUNT', 'LIST'],
                'requestedPackageLineItems':   [self._pkg_dict(p, i) for i, p in enumerate(packages)],
            },
        }

        if service_code:
            payload['requestedShipment']['serviceType'] = service_code

        data = self._post('/rate/v1/rates/quotes', payload)
        return self._parse_rate_replies(data)

    def _parse_rate_replies(self, data: dict) -> list:
        output    = data.get('output', {})
        rate_list = output.get('rateReplyDetails', [])
        results   = []

        for entry in rate_list:
            code = entry.get('serviceType', '')
            rated_shipments = entry.get('ratedShipmentDetails', [])
            if not rated_shipments:
                continue

            # Prefer ACCOUNT rate over LIST rate
            account_detail = next((r for r in rated_shipments if r.get('rateType') == 'ACCOUNT'), None)
            detail = account_detail or rated_shipments[0]

            total = detail.get('totalNetCharge', 0)
            currency = detail.get('currency', 'EUR')
            if isinstance(total, dict):
                currency = total.get('currency', currency)
                total = total.get('amount', 0)

            # Transit days
            commit = entry.get('operationalDetail', {})
            transit_days  = commit.get('transitTime', '') or ''
            delivery_date = commit.get('deliveryDate', '') or ''  # 'YYYY-MM-DD'

            # Map 'ONE_DAY' / 'TWO_DAYS' etc. → integer
            transit_int = _parse_transit_days(transit_days)

            results.append({
                'code':          code,
                'name':          FEDEX_SERVICES.get(code, f'FedEx {code}'),
                'price':         Decimal(str(total)),
                'currency':      currency,
                'transit_days':  transit_int,
                'delivery_date': delivery_date,
                'guaranteed':    bool(entry.get('commit', {}).get('label')),
            })

        return sorted(results, key=lambda x: x['price'])

    # ── Create Shipment ────────────────────────────────────────────────────────

    def create_shipment(self, to_address: dict, packages: list,
                        service_code: str = 'INTERNATIONAL_ECONOMY',
                        from_address: dict = None,
                        customs_info: dict = None,
                        reference: str = '') -> dict:
        """
        POST /ship/v1/shipments
        Повертає: {tracking_number, master_tracking, label_base64, label_format,
                   total_charge, currency, service_code, service_name}
        label_base64: PDF рядок (base64).
        """
        shipper      = from_address or self._default_shipper()
        label_format = 'PDF'

        is_intl = (
            (to_address.get('country') or 'DE').upper() !=
            (shipper.get('country') or 'DE').upper()
        )

        requested_shipment = {
            'shipper': {
                'contact': {
                    'personName':    shipper.get('name', ''),
                    'companyName':   shipper.get('company', ''),
                    'phoneNumber':   (shipper.get('phone', '') or '').replace(' ', ''),
                    'emailAddress':  shipper.get('email', ''),
                },
                'address': self._fmt_addr(shipper),
            },
            'recipients': [{
                'contact': {
                    'personName':  to_address.get('name', ''),
                    'companyName': to_address.get('company', ''),
                    'phoneNumber': (to_address.get('phone', '') or '').replace(' ', ''),
                },
                'address': self._fmt_addr(to_address),
            }],
            'serviceType':                service_code,
            'packagingType':              _PACKAGING_DEFAULT,
            'pickupType':                 'DROPOFF_AT_FEDEX_LOCATION',
            'shippingChargesPayment': {
                'paymentType': 'SENDER',
                'payor': {
                    'responsibleParty': {
                        'accountNumber': {'value': self.carrier.connection_uuid},
                    },
                },
            },
            'labelSpecification': {
                'labelFormatType': 'COMMON2D',
                'imageType':       label_format,
                'labelStockType':  'PAPER_4X6',
            },
            'requestedPackageLineItems': [self._pkg_dict(p, i, reference) for i, p in enumerate(packages)],
        }

        if reference:
            requested_shipment['customerReferences'] = [{
                'customerReferenceType': 'CUSTOMER_REFERENCE',
                'value': reference[:40],
            }]

        if is_intl and customs_info:
            requested_shipment['customsClearanceDetail'] = self._build_customs(customs_info, shipper)

        payload = {
            'labelResponseOptions':   'URL_ONLY',  # or 'LABEL'
            'requestedShipment':      requested_shipment,
            'accountNumber':          {'value': self.carrier.connection_uuid},
        }

        data = self._post('/ship/v1/shipments', payload)
        return self._parse_shipment_response(data, service_code, label_format)

    def _parse_shipment_response(self, data: dict, service_code: str, label_format: str) -> dict:
        output         = data.get('output', {})
        transaction_id = output.get('transactionId', '')
        ship_results   = output.get('transactionShipments', [{}])
        ship_result    = ship_results[0] if ship_results else {}

        master_tracking = ship_result.get('masterTrackingNumber', '')
        pkg_results     = ship_result.get('pieceResponses', [{}])
        pkg_result      = pkg_results[0] if pkg_results else {}

        tracking  = pkg_result.get('trackingNumber', '') or master_tracking
        label_url = pkg_result.get('packageDocuments', [{}])
        label_url = label_url[0].get('url', '') if label_url else ''
        label_b64 = pkg_result.get('packageDocuments', [{}])
        label_b64 = label_b64[0].get('encodedLabel', '') if label_b64 else ''

        charges = ship_result.get('shipmentRating', {}).get('shipmentRateDetails', [{}])
        charges = charges[0] if charges else {}
        total   = charges.get('totalNetCharge', 0)
        if isinstance(total, dict):
            currency = total.get('currency', 'EUR')
            total    = total.get('amount', 0)
        else:
            currency = charges.get('currency', 'EUR')

        return {
            'tracking_number': tracking,
            'master_tracking':  master_tracking,
            'shipment_id':      transaction_id,
            'label_base64':     label_b64,
            'label_url':        label_url,
            'label_format':     label_format,
            'total_charge':     Decimal(str(total)) if total else Decimal('0'),
            'currency':         currency,
            'service_code':     service_code,
            'service_name':     FEDEX_SERVICES.get(service_code, service_code),
        }

    # ── Tracking ───────────────────────────────────────────────────────────────

    def track(self, tracking_number: str) -> dict:
        """
        POST /track/v1/trackingnumbers
        Повертає: {tracking_number, status, status_description, estimated_delivery,
                   location, events, delivered}
        """
        payload = {
            'trackingInfo': [{'trackingNumberInfo': {'trackingNumber': tracking_number}}],
            'includeDetailedScans': True,
        }

        data = self._post('/track/v1/trackingnumbers', payload)

        try:
            output    = data.get('output', {})
            complete  = output.get('completeTrackResults', [{}])
            result    = complete[0] if complete else {}
            track_res = result.get('trackResults', [{}])
            track_res = track_res[0] if track_res else {}

            latest    = track_res.get('latestStatusDetail', {})
            status    = latest.get('code', 'UNKNOWN')
            status_desc = latest.get('description', '')

            date_times  = track_res.get('dateAndTimes', [])
            est_delivery = ''
            for dt in date_times:
                if dt.get('type') == 'ESTIMATED_DELIVERY':
                    raw = dt.get('dateTime', '')
                    est_delivery = raw[:10] if raw else ''
                    break

            events = []
            for scan in track_res.get('scanEvents', []):
                loc  = scan.get('scanLocation', {}).get('address', {})
                raw  = scan.get('date', '')
                events.append({
                    'date':        raw[:10] if raw else '',
                    'time':        raw[11:16] if len(raw) > 10 else '',
                    'description': scan.get('eventDescription', ''),
                    'location':    ', '.join(filter(None, [
                        loc.get('city', ''),
                        loc.get('stateOrProvinceCode', ''),
                        loc.get('countryCode', ''),
                    ])),
                })

            return {
                'tracking_number':    tracking_number,
                'status':             status,
                'status_description': status_desc,
                'estimated_delivery': est_delivery,
                'location':           events[0]['location'] if events else '',
                'events':             events,
                'delivered':          status == 'DL',
            }

        except (KeyError, IndexError) as e:
            logger.error('FedEx tracking parse error: %s', e)
            return {
                'tracking_number':    tracking_number,
                'status':             'UNKNOWN',
                'status_description': 'Не вдалося отримати статус',
                'events':             [],
                'delivered':          False,
            }

    # ── Cancel / Void ──────────────────────────────────────────────────────────

    def cancel_shipment(self, tracking_number: str,
                        shipment_date: str = '', service_code: str = '') -> dict:
        """
        PUT /ship/v1/shipments/cancel
        tracking_number: master tracking number отриманий при створенні.
        shipment_date: 'YYYY-MM-DD' (бажано передавати).
        Повертає: {'cancelled': True/False, 'message': str}
        """
        payload = {
            'accountNumber':   {'value': self.carrier.connection_uuid},
            'trackingNumber':  tracking_number,
        }
        if shipment_date:
            payload['shipDatestamp'] = shipment_date
        if service_code:
            payload['serviceType'] = service_code

        data = self._put('/ship/v1/shipments/cancel', payload)
        output = data.get('output', {})
        cancelled = output.get('cancelledShipment', False)
        msg = output.get('cancelledMessage', '') or ('Скасовано' if cancelled else 'Не скасовано')
        return {'cancelled': bool(cancelled), 'message': msg}

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _default_shipper(self) -> dict:
        c = self.carrier
        return {
            'name':         c.sender_name or c.sender_company or '',
            'company':      c.sender_company or '',
            'address_line': c.sender_street or '',
            'city':         c.sender_city or '',
            'state':        '',
            'postal':       c.sender_zip or '',
            'country':      c.sender_country or 'DE',
            'phone':        c.sender_phone or '',
            'email':        c.sender_email or '',
        }

    def _fmt_addr(self, addr: dict) -> dict:
        result = {
            'streetLines': [addr.get('address_line', '') or ''],
            'city':         addr.get('city', ''),
            'postalCode':   addr.get('postal', ''),
            'countryCode':  (addr.get('country') or 'DE').upper(),
            'residential':  False,
        }
        if addr.get('state'):
            result['stateOrProvinceCode'] = addr['state'].upper()
        return result

    def _pkg_dict(self, pkg: dict, index: int = 0, reference: str = '') -> dict:
        p = {
            'sequenceNumber': index + 1,
            'weight': {
                'units': 'KG',
                'value': round(float(pkg.get('weight_kg', 0.5)), 2),
            },
            'dimensions': {
                'length': round(float(pkg.get('length_cm', 10))),
                'width':  round(float(pkg.get('width_cm', 10))),
                'height': round(float(pkg.get('height_cm', 10))),
                'units':  'CM',
            },
        }
        if reference:
            p['customerReferences'] = [{'customerReferenceType': 'CUSTOMER_REFERENCE', 'value': reference[:40]}]
        return p

    def _build_customs(self, info: dict, shipper: dict) -> dict:
        """Build customsClearanceDetail payload for FedEx Ship API."""
        items_list = info.get('items') or [{
            'description': info.get('description', 'Goods'),
            'quantity':    1,
            'value':       float(info.get('value_usd', 0)),
            'weight_kg':   0.5,
            'hs_code':     '',
            'country':     shipper.get('country', self.carrier.sender_country or 'DE'),
        }]

        # ExportDetail reason map: SALE / GIFT / SAMPLE / RETURN / OTHER
        contents_map = {
            'SALE':   'SOLD',
            'GIFT':   'GIFT',
            'SAMPLE': 'SAMPLE',
            'RETURN': 'RETURNED_GOODS',
            'OTHER':  'OTHER',
        }
        reason = contents_map.get((info.get('contents_type') or 'SALE').upper(), 'SOLD')

        commodities = []
        for item in items_list:
            cm = {
                'description':    item.get('description', 'Goods')[:50],
                'quantity':       int(item.get('quantity', 1)),
                'quantityUnits':  'PCS',
                'unitPrice': {
                    'amount':   round(float(item.get('value', 0)), 2),
                    'currency': info.get('currency', 'EUR'),
                },
                'customsValue': {
                    'amount':   round(float(item.get('value', 0)) * int(item.get('quantity', 1)), 2),
                    'currency': info.get('currency', 'EUR'),
                },
                'weight': {
                    'units': 'KG',
                    'value': round(float(item.get('weight_kg', 0.1)), 3),
                },
                'countryOfManufacture': item.get('country', shipper.get('country', 'DE')),
            }
            if item.get('hs_code'):
                cm['harmonizedCode'] = item['hs_code']
            commodities.append(cm)

        total_customs_value = sum(
            round(float(it.get('value', 0)) * int(it.get('quantity', 1)), 2)
            for it in items_list
        )

        return {
            'dutiesPayment': {
                'paymentType': 'SENDER',
                'payor': {
                    'responsibleParty': {
                        'accountNumber': {'value': self.carrier.connection_uuid},
                    },
                },
            },
            'customsValue': {
                'amount':   round(total_customs_value, 2),
                'currency': info.get('currency', 'EUR'),
            },
            'exportDetail': {'exportComplianceStatement': reason},
            'commodities':  commodities,
        }


# ── Utility ─────────────────────────────────────────────────────────────────────

def _parse_transit_days(raw: str) -> int | None:
    """Convert FedEx transitTime string to integer days. Returns None if unparseable."""
    if not raw:
        return None
    _map = {
        'ONE_DAY':   1, 'TWO_DAYS':   2, 'THREE_DAYS': 3,
        'FOUR_DAYS': 4, 'FIVE_DAYS':  5, 'SIX_DAYS':   6,
        'SEVEN_DAYS': 7,
    }
    if raw in _map:
        return _map[raw]
    if raw.isdigit():
        return int(raw)
    return None
