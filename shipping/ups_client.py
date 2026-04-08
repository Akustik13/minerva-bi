"""
UPS REST API Client для Minerva BI.
OAuth 2.0 Client Credentials flow.
Credentials зберігаються в моделі Carrier (api_key=Client ID,
api_secret=Client Secret, connection_uuid=Account Number,
api_url='sandbox'|'' for production).
Токен кешується через Django cache framework.
"""
import base64
import logging
import uuid
from datetime import timedelta
from decimal import Decimal

import requests
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('shipping.ups')

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
    '65': 'UPS Saver',
    '96': 'UPS Worldwide Express Freight',
}

PACKAGING_CUSTOMER = '02'

# Token TTL in cache: 4h minus 10 min buffer
_TOKEN_CACHE_SECONDS = 14399 - 600


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
        expires_in = max(int(data.get('expires_in', 14399)) - 600, 60)
        cache.set(cache_key, token, expires_in)
        logger.info('UPS OAuth token оновлено для carrier=%s (ttl=%ss)', self.carrier.pk, expires_in)
        return token

    def _headers(self) -> dict:
        return {
            'Authorization': f'Bearer {self.get_token()}',
            'Content-Type': 'application/json',
            'transId': str(uuid.uuid4())[:8],
            'transactionSrc': 'minerva-bi',
        }

    def _post(self, endpoint: str, payload: dict) -> dict:
        url = f'{self.base_url}{endpoint}'
        try:
            r = requests.post(url, headers=self._headers(), json=payload, timeout=60)
            if r.status_code == 401:
                # скинути токен і повторити
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

    def get_rates(self, to_address: dict, packages: list,
                  from_address: dict = None, service_code: str = None) -> list:
        """
        Отримати тарифи.
        to_address/from_address: {name, address_line, city, state, postal, country, phone}
        packages: [{weight_kg, length_cm, width_cm, height_cm}]
        Повертає: [{code, name, price, currency, transit_days, guaranteed}]
        """
        shipper = from_address or self._default_shipper()
        ups_packages = [self._pkg_dict(p) for p in packages]

        payload = {
            'RateRequest': {
                'Request': {'RequestOption': 'Shop' if not service_code else 'Rate', 'SubVersion': '2403'},
                'Shipment': {
                    'Shipper': {
                        'Name': shipper.get('name', 'Shipper'),
                        'ShipperNumber': self.carrier.connection_uuid,
                        'Address': self._fmt_addr(shipper),
                    },
                    'ShipTo': {'Name': to_address.get('name', 'Recipient'), 'Address': self._fmt_addr(to_address)},
                    'ShipFrom': {'Name': shipper.get('name', 'Shipper'), 'Address': self._fmt_addr(shipper)},
                    'Package': ups_packages,
                    **(({'Service': {'Code': service_code}}) if service_code else {}),
                },
            },
        }

        data = self._post('/api/rating/v2403/rate', payload)
        shipments = data.get('RateResponse', {}).get('RatedShipment', [])
        if isinstance(shipments, dict):
            shipments = [shipments]

        results = []
        for s in shipments:
            code     = s.get('Service', {}).get('Code', '')
            price    = s.get('TotalCharges', {}).get('MonetaryValue', '0')
            currency = s.get('TotalCharges', {}).get('CurrencyCode', 'USD')
            transit  = s.get('GuaranteedDelivery', {}).get('BusinessDaysInTransit', '')
            results.append({
                'code': code, 'name': UPS_SERVICES.get(code, f'UPS {code}'),
                'price': Decimal(price), 'currency': currency,
                'transit_days': transit, 'guaranteed': bool(s.get('GuaranteedDelivery')),
            })
        return sorted(results, key=lambda x: x['price'])

    # ── Create Shipment ───────────────────────────────────────────────────────

    def create_shipment(self, to_address: dict, packages: list,
                        service_code: str = '11', from_address: dict = None,
                        customs_info: dict = None, reference: str = '') -> dict:
        """
        Створити відправлення і отримати мітку.
        Повертає: {tracking_number, shipment_id, label_base64, label_format, total_charge, currency}
        """
        shipper = from_address or self._default_shipper()
        label_format = 'PDF'

        shipment = {
            'Shipper': {
                'Name': shipper.get('name', ''), 'AttentionName': shipper.get('name', ''),
                'ShipperNumber': self.carrier.connection_uuid,
                'Phone': {'Number': (shipper.get('phone', '') or '').replace(' ', '')},
                'Address': self._fmt_addr(shipper),
            },
            'ShipTo': {
                'Name': to_address.get('name', ''), 'AttentionName': to_address.get('name', ''),
                'Phone': {'Number': (to_address.get('phone', '') or '').replace(' ', '')},
                'Address': self._fmt_addr(to_address),
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
                'Request': {'SubVersion': '2409', 'TransactionReference': {'CustomerContext': reference or 'minerva'}},
                'Shipment': shipment,
                'LabelSpecification': {
                    'LabelImageFormat': {'Code': label_format},
                    'LabelStockSize': {'Height': '6', 'Width': '4'},
                },
            },
        }

        data = self._post('/api/shipments/v2409/ship', payload)
        resp = data.get('ShipmentResponse', {})
        results_data = resp.get('ShipmentResults', {})
        pkg_results = results_data.get('PackageResults', {})
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
            'currency':        charges.get('CurrencyCode', 'USD'),
            'service_code':    service_code,
            'service_name':    UPS_SERVICES.get(service_code, ''),
        }

    # ── Tracking ──────────────────────────────────────────────────────────────

    def track(self, tracking_number: str) -> dict:
        try:
            data = self._get(
                f'/api/track/v1/details/{tracking_number}',
                params={'locale': 'en_US', 'returnMilestones': 'false'},
            )
        except UPSError:
            raise

        try:
            shipment = data['trackResponse']['shipment'][0]
            package  = shipment.get('package', [{}])[0]
            status   = package.get('currentStatus', {})

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
                'tracking_number': tracking_number, 'status': 'UNKNOWN',
                'status_description': 'Не вдалося отримати статус', 'events': [], 'delivered': False,
            }

    # ── Void ──────────────────────────────────────────────────────────────────

    def void_shipment(self, shipment_id: str) -> dict:
        url = f'{self.base_url}/api/shipments/v2409/void/cancel/{shipment_id}'
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
        p = {
            'Packaging': {'Code': PACKAGING_CUSTOMER},
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
        items_list = info.get('items', []) or [{
            'description': info.get('description', 'Goods'),
            'quantity': 1, 'value': float(info.get('value_usd', 0)),
            'weight_kg': 0.5, 'hs_code': '',
            'country': self.carrier.sender_country or 'DE',
        }]

        products = []
        for item in items_list:
            prod = {
                'Description': item.get('description', 'Goods')[:35],
                'Unit': {
                    'Number': str(item.get('quantity', 1)),
                    'UnitOfMeasurement': {'Code': 'PCS'},
                    'EstimatedValue': str(round(float(item.get('value', 0)), 2)),
                    'CurrencyCode': info.get('currency', 'EUR'),
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

        contents_map = {'SALE': '01', 'GIFT': '02', 'SAMPLE': '03', 'RETURN': '04', 'OTHER': '05'}
        reason = contents_map.get(info.get('contents_type', 'SALE').upper(), '01')

        return {
            'FormType': '01', 'Product': products,
            'ReasonForExport': reason, 'CurrencyCode': info.get('currency', 'EUR'),
            'DeclarationStatement': 'I hereby certify that the information is true and correct.',
        }
