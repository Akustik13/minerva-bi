"""
UPS REST API Client для Minerva BI.
OAuth 2.0 Client Credentials flow.
Credentials зберігаються в моделі Carrier (api_key=Client ID,
api_secret=Client Secret, connection_uuid=Account Number,
api_url='sandbox'|'' for production).
Токен кешується через Django cache framework.

Перевірені ендпоінти (docs: developer.ups.com):
  Auth:     POST /security/v1/oauth/token
  Rates:    POST /api/rating/v2409/{Rate|Shop}          ← requestoption у ШЛЯХУ
  Ship:     POST /api/shipments/v2409/ship
  Track:    GET  /api/track/v1/details/{trackingNumber}
  Void:     DELETE /api/shipments/v2409/void/cancel/{shipmentId}
"""
import base64
import logging
import uuid
from decimal import Decimal

import requests
from django.core.cache import cache

logger = logging.getLogger('shipping.ups')

# ── Service code → human name ─────────────────────────────────────────────────
UPS_SERVICES = {
    '01': 'UPS Next Day Air',
    '02': 'UPS 2nd Day Air',
    '03': 'UPS Ground',
    '07': 'UPS Worldwide Express',
    '08': 'UPS Worldwide Expedited',
    '11': 'UPS Standard',
    '12': 'UPS 3 Day Select',
    '13': 'UPS Next Day Air Saver',
    '14': 'UPS Next Day Air Early AM',
    '54': 'UPS Worldwide Express Plus',
    '59': 'UPS 2nd Day Air AM',
    '65': 'UPS Worldwide Saver',
    '96': 'UPS Worldwide Express Freight',
}

# Packaging code: '00'=Unknown (most permissive), '02'=My Packaging
# Use '00' as default — some UPS accounts reject '02' for certain routes
PACKAGING_CUSTOMER = '00'

# API version
_API_VERSION = 'v2409'


class UPSError(Exception):
    def __init__(self, message, code=None, response=None):
        super().__init__(message)
        self.code = code
        self.response = response


def _get_ups_carrier():
    """Знайти активний UPS Carrier з credentials."""
    from shipping.models import Carrier
    c = (Carrier.objects
         .filter(carrier_type='ups', is_active=True)
         .exclude(api_key='')
         .order_by('-is_default')
         .first())
    if not c:
        raise UPSError(
            'Немає активного UPS перевізника. '
            'Додайте Carrier з типом UPS і заповніть '
            'API ключ (Client ID), API Secret (Client Secret) і '
            'Account Number (Connection UUID).'
        )
    return c


class UPSClient:
    """
    Клієнт UPS REST API. OAuth 2.0 Client Credentials.
    Передай carrier=Carrier.objects.get(...) або залиш None — буде взято автоматично.
    """

    def __init__(self, carrier=None):
        self.carrier = carrier or _get_ups_carrier()
        c = self.carrier
        if not (c.api_key and c.api_secret and c.connection_uuid):
            raise UPSError(
                f'UPS Carrier «{c.name}»: заповніть API ключ (Client ID), '
                'API Secret (Client Secret) і Connection UUID (Account Number).'
            )

    @property
    def _is_sandbox(self) -> bool:
        return (self.carrier.api_url or '').lower() in ('sandbox', 'test', 'staging', 'uat')

    @property
    def base_url(self) -> str:
        # CIE = sandbox/test environment
        return 'https://wwwcie.ups.com' if self._is_sandbox else 'https://onlinetools.ups.com'

    # ── Auth ──────────────────────────────────────────────────────────────────

    def get_token(self) -> str:
        cache_key = f'ups_token_{self.carrier.pk}'
        token = cache.get(cache_key)
        if token:
            return token

        credentials = base64.b64encode(
            f'{self.carrier.api_key}:{self.carrier.api_secret}'.encode()
        ).decode()

        r = requests.post(
            f'{self.base_url}/security/v1/oauth/token',
            headers={
                'Authorization': f'Basic {credentials}',
                'Content-Type': 'application/x-www-form-urlencoded',
                'x-merchant-id': self.carrier.connection_uuid,
            },
            data='grant_type=client_credentials',
            timeout=30,
        )
        if r.status_code != 200:
            raise UPSError(
                f'Помилка аутентифікації UPS [{r.status_code}]: {r.text[:200]}',
                code=r.status_code, response=r.text,
            )

        data = r.json()
        token = data['access_token']
        # expires_in from API (typically 3600s = 1h); cache with 60s buffer
        expires_in = max(int(data.get('expires_in', 3600)) - 60, 60)
        cache.set(cache_key, token, expires_in)
        logger.info('UPS OAuth token оновлено carrier=%s (ttl=%ss)', self.carrier.pk, expires_in)
        return token

    def _headers(self) -> dict:
        return {
            'Authorization': f'Bearer {self.get_token()}',
            'Content-Type':  'application/json',
            'transId':        str(uuid.uuid4())[:32],
            'transactionSrc': 'minerva-bi',
        }

    def _post(self, endpoint: str, payload: dict) -> dict:
        url = f'{self.base_url}{endpoint}'
        try:
            r = requests.post(url, headers=self._headers(), json=payload, timeout=60)
            if r.status_code == 401:
                # Token expired mid-session — refresh and retry once
                cache.delete(f'ups_token_{self.carrier.pk}')
                r = requests.post(url, headers=self._headers(), json=payload, timeout=60)
            if not r.ok:
                self._handle_error(r)
            return r.json()
        except requests.Timeout:
            raise UPSError('UPS API не відповідає (timeout 60с)')
        except requests.ConnectionError as e:
            raise UPSError(f"Помилка з'єднання з UPS: {e}")

    def _get(self, endpoint: str, params: dict = None) -> dict:
        url = f'{self.base_url}{endpoint}'
        try:
            r = requests.get(url, headers=self._headers(), params=params, timeout=30)
            if not r.ok:
                self._handle_error(r)
            return r.json()
        except requests.Timeout:
            raise UPSError('UPS API не відповідає')
        except requests.ConnectionError as e:
            raise UPSError(f"Помилка з'єднання: {e}")

    def _handle_error(self, response):
        try:
            data = response.json()
            errors = (data.get('response', {}).get('errors', []) or data.get('errors', []))
            msg = '; '.join(e.get('message', str(e)) for e in errors[:3]) if errors else response.text[:300]
        except Exception:
            msg = response.text[:300]
        raise UPSError(f'UPS API [{response.status_code}]: {msg}', code=response.status_code, response=response.text)

    # ── Rates ─────────────────────────────────────────────────────────────────

    # Service codes tried in fallback (Shop not available)
    _FALLBACK_SERVICES = ['11', '07', '08', '65', '54', '03', '02', '01', '12', '59', '96']

    def get_rates(self, to_address: dict, packages: list,
                  from_address: dict = None, service_code: str = None) -> list:
        """
        Отримати тарифи.
        to_address/from_address: {name, address_line, city, state, postal, country, phone}
        packages: [{weight_kg, length_cm, width_cm, height_cm}]
        Повертає: [{code, name, price, currency, transit_days, guaranteed}]
        Якщо акаунт не підтримує Shop — автоматично перебирає сервіси по одному.
        """
        if service_code:
            return self._rate_single(to_address, packages, from_address, service_code)

        # Try Shop (all services at once)
        try:
            rates = self._rate_shop(to_address, packages, from_address)
            if rates:
                return rates
            # Empty response — try per-service
            return self._rate_fallback(to_address, packages, from_address)
        except UPSError as e:
            logger.info('UPS Shop failed (%s), switching to per-service fallback', e)
            return self._rate_fallback(to_address, packages, from_address)

    def _build_rate_shipment(self, to_address, packages, from_address, service_code=None):
        shipper = from_address or self._default_shipper()
        shipment = {
            'Shipper': {
                'Name':          shipper.get('name', 'Shipper'),
                'ShipperNumber': self.carrier.connection_uuid,
                'Address':       self._fmt_addr(shipper),
            },
            'ShipTo':   {'Name': to_address.get('name', 'Recipient'), 'Address': self._fmt_addr(to_address)},
            'ShipFrom': {'Name': shipper.get('name', 'Shipper'),       'Address': self._fmt_addr(shipper)},
            'PaymentDetails': {
                'ShipmentCharge': {
                    'Type': '01',
                    'BillShipper': {'AccountNumber': self.carrier.connection_uuid},
                },
            },
            'ShipmentRatingOptions': {'NegotiatedRatesIndicator': ''},
            'Package':  [self._pkg_dict(p) for p in packages],
        }
        if service_code:
            shipment['Service'] = {'Code': service_code}
        return shipment

    def _rate_shop(self, to_address, packages, from_address):
        """
        POST /api/rating/v2409/Shoptimeintransit — rates + transit days for all services.
        PickupDate (today, YYYYMMDD) is required for TimeInTransit data to be returned.
        Falls back to plain Shop if Shoptimeintransit returns an error.
        """
        from datetime import date as _date
        shipper   = from_address or self._default_shipper()
        shipment  = self._build_rate_shipment(to_address, packages, shipper)
        # PickupDate is mandatory for Shoptimeintransit — without it TimeInTransit is absent
        shipment['DeliveryTimeInformation'] = {
            'PackageBillType': '02',  # 02 = Non-Documents
            'Pickup': {'Date': _date.today().strftime('%Y%m%d')},
        }
        payload = {'RateRequest': {
            'Request': {
                'RequestOption': 'Shoptimeintransit',
                'TransactionReference': {'CustomerContext': 'minerva-bi'},
            },
            'Shipment': shipment,
        }}
        try:
            data = self._post(f'/api/rating/{_API_VERSION}/Shoptimeintransit', payload)
        except UPSError:
            # Some accounts don't support Shoptimeintransit — fall back to plain Shop
            del shipment['DeliveryTimeInformation']
            payload['RateRequest']['Request']['RequestOption'] = 'Shop'
            data = self._post(f'/api/rating/{_API_VERSION}/Shop', payload)
        return self._parse_rated_shipments(data)

    def _rate_single(self, to_address, packages, from_address, service_code):
        """
        POST /api/rating/v2409/Rate — single service.
        """
        shipper = from_address or self._default_shipper()
        payload = {'RateRequest': {
            'Request': {
                'RequestOption': 'Rate',
                'TransactionReference': {'CustomerContext': 'minerva-bi'},
            },
            'Shipment': self._build_rate_shipment(to_address, packages, shipper, service_code),
        }}
        data = self._post(f'/api/rating/{_API_VERSION}/Rate', payload)
        return self._parse_rated_shipments(data)

    def _rate_fallback(self, to_address, packages, from_address):
        """Перебирає service codes по одному, повертає ті що відповіли успішно."""
        results = []
        for code in self._FALLBACK_SERVICES:
            try:
                results.extend(self._rate_single(to_address, packages, from_address, code))
            except UPSError:
                # retry with packaging=00 (Unknown) — some services reject 02
                try:
                    pkgs_alt = [{**p, '_pkg_override': '00'} for p in packages]
                    results.extend(self._rate_single(to_address, pkgs_alt, from_address, code))
                except UPSError:
                    pass
        return sorted(results, key=lambda x: x['price'])

    def _parse_rated_shipments(self, data) -> list:
        shipments = data.get('RateResponse', {}).get('RatedShipment', [])
        if isinstance(shipments, dict):
            shipments = [shipments]
        results = []
        for s in shipments:
            code   = s.get('Service', {}).get('Code', '')
            retail = s.get('TotalCharges', {})
            neg    = s.get('NegotiatedRateCharges', {})
            # Prefer negotiated (account) rates; fall back to retail
            if neg.get('TotalCharge', {}).get('MonetaryValue'):
                price    = neg['TotalCharge']['MonetaryValue']
                currency = neg['TotalCharge'].get('CurrencyCode') or retail.get('CurrencyCode', 'EUR')
            else:
                price    = retail.get('MonetaryValue', '0')
                currency = retail.get('CurrencyCode', 'EUR')

            # Transit days: prefer Shoptimeintransit response, fall back to GuaranteedDelivery
            tti           = s.get('TimeInTransit', {})
            est_arrival   = tti.get('EstimatedArrival', {})
            transit_days  = est_arrival.get('BusinessDaysInTransit', '') \
                            or s.get('GuaranteedDelivery', {}).get('BusinessDaysInTransit', '')

            # Delivery date from Shoptimeintransit (YYYYMMDD → YYYY-MM-DD)
            raw_date      = est_arrival.get('Arrival', {}).get('Date', '')
            delivery_date = (f'{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}' if len(raw_date) == 8 else raw_date)

            transit_int = int(transit_days) if str(transit_days).isdigit() else None

            results.append({
                'code':          code,
                'name':          UPS_SERVICES.get(code, f'UPS {code}'),
                'price':         Decimal(price),
                'currency':      currency,
                'transit_days':  transit_int,
                'delivery_date': delivery_date,
                'guaranteed':    bool(s.get('GuaranteedDelivery') or transit_days),
            })
        return sorted(results, key=lambda x: x['price'])

    # ── Create Shipment ───────────────────────────────────────────────────────

    def create_shipment(self, to_address: dict, packages: list,
                        service_code: str = '11', from_address: dict = None,
                        customs_info: dict = None, reference: str = '') -> dict:
        """
        POST /api/shipments/v2409/ship
        Повертає: {tracking_number, shipment_id, label_base64, label_format, total_charge, currency}
        """
        shipper = from_address or self._default_shipper()
        label_format = 'PDF'

        shipment = {
            'Shipper': {
                'Name':          shipper.get('name', ''),
                'AttentionName': shipper.get('name', ''),
                'ShipperNumber': self.carrier.connection_uuid,
                'Phone':         {'Number': (shipper.get('phone', '') or '').replace(' ', '')},
                'Address':       self._fmt_addr(shipper),
            },
            'ShipTo': {
                'Name':          to_address.get('name', ''),
                'AttentionName': to_address.get('name', ''),
                'Phone':         {'Number': (to_address.get('phone', '') or '').replace(' ', '')},
                'Address':       self._fmt_addr(to_address),
            },
            'ShipFrom': {
                'Name':    shipper.get('name', ''),
                'Address': self._fmt_addr(shipper),
            },
            'PaymentInformation': {
                'ShipmentCharge': [{'Type': '01', 'BillShipper': {'AccountNumber': self.carrier.connection_uuid}}],
            },
            'Service': {'Code': service_code, 'Description': UPS_SERVICES.get(service_code, '')},
            'Package': [self._pkg_dict(p) for p in packages],
        }

        if reference:
            shipment['ReferenceNumber'] = [{'Code': '02', 'Value': reference[:35]}]

        is_intl = (
            (to_address.get('country') or 'DE').upper() !=
            (shipper.get('country') or 'DE').upper()
        )
        if is_intl and customs_info:
            shipment['ShipmentServiceOptions'] = {
                'InternationalForms': self._build_customs(customs_info),
            }

        payload = {
            'ShipmentRequest': {
                'Request': {
                    'SubVersion':         _API_VERSION,
                    'RequestOption':      'nonvalidate',
                    'TransactionReference': {'CustomerContext': reference or 'minerva-bi'},
                },
                'Shipment': shipment,
                'LabelSpecification': {
                    'LabelImageFormat': {'Code': label_format},
                    'LabelStockSize':   {'Height': '6', 'Width': '4'},
                },
            },
        }

        data = self._post(f'/api/shipments/{_API_VERSION}/ship', payload)
        resp         = data.get('ShipmentResponse', {})
        results_data = resp.get('ShipmentResults', {})
        pkg_results  = results_data.get('PackageResults', {})
        if isinstance(pkg_results, list):
            pkg_results = pkg_results[0] if pkg_results else {}

        tracking  = results_data.get('ShipmentIdentificationNumber', '') or pkg_results.get('TrackingNumber', '')
        label_b64 = pkg_results.get('ShippingLabel', {}).get('GraphicImage', '')
        charges   = results_data.get('ShipmentCharges', {}).get('TotalCharges', {})

        return {
            'tracking_number': tracking,
            'shipment_id':     results_data.get('ShipmentIdentificationNumber', ''),
            'label_base64':    label_b64,
            'label_format':    label_format,
            'total_charge':    Decimal(charges.get('MonetaryValue', '0')),
            'currency':        charges.get('CurrencyCode', 'EUR'),
            'service_code':    service_code,
            'service_name':    UPS_SERVICES.get(service_code, ''),
        }

    # ── Tracking ──────────────────────────────────────────────────────────────

    def track(self, tracking_number: str) -> dict:
        """GET /api/track/v1/details/{trackingNumber}"""
        data = self._get(
            f'/api/track/v1/details/{tracking_number}',
            params={'locale': 'en_US', 'returnMilestones': 'false'},
        )

        try:
            shipment = data['trackResponse']['shipment'][0]
            package  = shipment.get('package', [{}])
            if isinstance(package, list):
                package = package[0] if package else {}

            status = package.get('currentStatus', {})
            events = []
            for act in package.get('activity', []):
                loc  = act.get('location', {}).get('address', {})
                date = act.get('date', '')
                time = act.get('time', '')
                events.append({
                    'date':        f'{date[:4]}-{date[4:6]}-{date[6:]}' if len(date) == 8 else date,
                    'time':        f'{time[:2]}:{time[2:4]}' if len(time) >= 4 else time,
                    'description': act.get('description', ''),
                    'location':    ', '.join(filter(None, [
                        loc.get('city', ''), loc.get('stateProvince', ''), loc.get('countryCode', ''),
                    ])),
                })

            delivery_dates = package.get('deliveryDate', [])
            est_delivery = ''
            for d in (delivery_dates if isinstance(delivery_dates, list) else [delivery_dates]):
                if isinstance(d, dict) and d.get('date'):
                    est_delivery = d['date']
                    break

            return {
                'tracking_number':    tracking_number,
                'status':             status.get('type', ''),
                'status_description': status.get('description', ''),
                'estimated_delivery': est_delivery,
                'location':           events[0]['location'] if events else '',
                'events':             events,
                'delivered':          status.get('type') == 'D',
            }
        except (KeyError, IndexError) as e:
            logger.error('UPS tracking parse error: %s', e)
            return {
                'tracking_number':    tracking_number,
                'status':             'UNKNOWN',
                'status_description': 'Не вдалося отримати статус',
                'events':             [],
                'delivered':          False,
            }

    # ── Void ──────────────────────────────────────────────────────────────────

    def void_shipment(self, shipment_id: str) -> dict:
        """DELETE /api/shipments/v2409/void/cancel/{shipmentId}"""
        url = f'{self.base_url}/api/shipments/{_API_VERSION}/void/cancel/{shipment_id}'
        try:
            r = requests.delete(url, headers=self._headers(), timeout=30)
            if not r.ok:
                self._handle_error(r)
            return r.json()
        except requests.Timeout:
            raise UPSError('UPS API не відповідає')
        except requests.ConnectionError as e:
            raise UPSError(f"Помилка з'єднання: {e}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _default_shipper(self) -> dict:
        c = self.carrier
        return {
            'name':         c.sender_name or c.sender_company or '',
            'address_line': c.sender_street or '',
            'city':         c.sender_city or '',
            'state':        '',
            'postal':       c.sender_zip or '',
            'country':      c.sender_country or 'DE',
            'phone':        c.sender_phone or '',
        }

    def _fmt_addr(self, addr: dict) -> dict:
        result = {
            'AddressLine': [addr.get('address_line', '')],
            'City':        addr.get('city', ''),
            'PostalCode':  addr.get('postal', ''),
            'CountryCode': (addr.get('country') or 'DE').upper(),
        }
        if addr.get('state'):
            result['StateProvinceCode'] = addr['state'].upper()
        return result

    def _pkg_dict(self, pkg: dict) -> dict:
        pkg_code = pkg.get('_pkg_override', PACKAGING_CUSTOMER)
        p = {
            'PackagingType': {'Code': pkg_code},
            'Dimensions': {
                'UnitOfMeasurement': {'Code': 'CM'},
                'Length': str(round(float(pkg.get('length_cm', 10)))),
                'Width':  str(round(float(pkg.get('width_cm', 10)))),
                'Height': str(round(float(pkg.get('height_cm', 10)))),
            },
            'PackageWeight': {
                'UnitOfMeasurement': {'Code': 'KGS'},
                'Weight': str(round(float(pkg.get('weight_kg', 0.5)), 2)),
            },
        }
        if pkg.get('reference'):
            p['ReferenceNumber'] = [{'Code': '02', 'Value': pkg['reference'][:35]}]
        return p

    def _build_customs(self, info: dict) -> dict:
        """Build InternationalForms payload for UPS Ship API."""
        items_list = info.get('items') or [{
            'description': info.get('description', 'Goods'),
            'quantity':    1,
            'value':       float(info.get('value_usd', 0)),
            'weight_kg':   0.5,
            'hs_code':     '',
            'country':     self.carrier.sender_country or 'DE',
        }]

        products = []
        for item in items_list:
            prod = {
                'Description': item.get('description', 'Goods')[:35],
                'Unit': {
                    'Number':             str(item.get('quantity', 1)),
                    'UnitOfMeasurement':  {'Code': 'PCS'},
                    'EstimatedValue':     str(round(float(item.get('value', 0)), 2)),
                    'CurrencyCode':       info.get('currency', 'EUR'),
                },
                'OriginCountryCode': item.get('country', self.carrier.sender_country or 'DE'),
                'NetWeight': {
                    'UnitOfMeasurement': {'Code': 'KGS'},
                    'Weight': str(round(float(item.get('weight_kg', 0.5)), 2)),
                },
            }
            if item.get('hs_code'):
                prod['CommodityCode'] = item['hs_code']
            products.append(prod)

        # ReasonForExport: 01=Sale, 02=Gift, 03=Sample, 04=Return, 05=Other
        contents_map = {'SALE': '01', 'GIFT': '02', 'SAMPLE': '03', 'RETURN': '04', 'OTHER': '05'}
        reason = contents_map.get(info.get('contents_type', 'SALE').upper(), '01')

        return {
            'FormType':            '01',
            'Product':             products,
            'ReasonForExport':     reason,
            'CurrencyCode':        info.get('currency', 'EUR'),
            'DeclarationStatement': 'I hereby certify that the information on this invoice is true and correct.',
        }
