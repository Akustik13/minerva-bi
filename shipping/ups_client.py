"""
UPS REST API Client для Minerva BI.
OAuth 2.0 Client Credentials flow.
Документація: https://developer.ups.com/api/reference
"""
import base64
import logging
import uuid
from datetime import timedelta
from decimal import Decimal

import requests
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


class UPSError(Exception):
    def __init__(self, message, code=None, response=None):
        super().__init__(message)
        self.code = code
        self.response = response


class UPSClient:
    """
    Клієнт UPS REST API. OAuth 2.0 Client Credentials.
    Токен кешується в БД (безпечно при multi-process / контейнер рестарт).
    """

    def __init__(self, config=None):
        from shipping.models import UPSConfig
        self.config = config or UPSConfig.get()
        if not self.config.is_configured():
            raise UPSError(
                'UPS не налаштований. '
                'Відкрий Доставка → UPS Налаштування і введи '
                'Client ID, Client Secret та Account Number.'
            )

    # ── Auth ──────────────────────────────────────────────────────────────────

    def get_token(self) -> str:
        cfg = self.config
        now = timezone.now()
        if (cfg._cached_token and cfg._token_expires_at and
                cfg._token_expires_at > now + timedelta(minutes=5)):
            return cfg._cached_token

        credentials = base64.b64encode(
            f'{cfg.client_id}:{cfg.client_secret}'.encode()
        ).decode()

        r = requests.post(
            f'{cfg.base_url}/security/v1/oauth/token',
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
        expires_in = int(data.get('expires_in', 14399))

        cfg._cached_token = token
        cfg._token_expires_at = now + timedelta(seconds=expires_in)
        cfg.save(update_fields=['cached_token', 'token_expires_at'])
        logger.info('UPS OAuth token оновлено (expires in %ss)', expires_in)
        return token

    def _headers(self) -> dict:
        return {
            'Authorization': f'Bearer {self.get_token()}',
            'Content-Type': 'application/json',
            'transId': str(uuid.uuid4())[:8],
            'transactionSrc': 'minerva-bi',
        }

    def _post(self, endpoint: str, payload: dict) -> dict:
        url = f'{self.config.base_url}{endpoint}'
        try:
            r = requests.post(url, headers=self._headers(), json=payload, timeout=60)
            if r.status_code == 401:
                # токен протух — скинути і повторити
                self.config._cached_token = ''
                self.config.save(update_fields=['cached_token'])
                r = requests.post(url, headers=self._headers(), json=payload, timeout=60)
            if not r.ok:
                self._handle_error(r)
            return r.json()
        except requests.Timeout:
            raise UPSError('UPS API не відповідає (timeout 60с)')
        except requests.ConnectionError as e:
            raise UPSError(f"Помилка з'єднання з UPS: {e}")

    def _get(self, endpoint: str, params: dict = None) -> dict:
        url = f'{self.config.base_url}{endpoint}'
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
            errors = (data.get('response', {}).get('errors', []) or
                      data.get('errors', []))
            if errors:
                msg = '; '.join(e.get('message', str(e)) for e in errors[:3])
            else:
                msg = response.text[:300]
        except Exception:
            msg = response.text[:300]
        raise UPSError(
            f'UPS API [{response.status_code}]: {msg}',
            code=response.status_code, response=response.text,
        )

    # ── Rates ─────────────────────────────────────────────────────────────────

    def get_rates(self, to_address: dict, packages: list,
                  from_address: dict = None, service_code: str = None) -> list:
        """
        Отримати тарифи. to_address: {name, address_line, city, state, postal, country, phone}
        packages: [{weight_kg, length_cm, width_cm, height_cm}]
        Повертає: [{code, name, price, currency, transit_days, guaranteed}] — відсортовано за ціною.
        """
        cfg = self.config
        shipper = from_address or self._default_shipper()

        ups_packages = []
        for pkg in packages:
            ups_packages.append({
                'PackagingType': {'Code': PACKAGING_CUSTOMER},
                'Dimensions': {
                    'UnitOfMeasurement': {'Code': 'CM'},
                    'Length': str(round(float(pkg.get('length_cm', 10)))),
                    'Width':  str(round(float(pkg.get('width_cm', 10)))),
                    'Height': str(round(float(pkg.get('height_cm', 10)))),
                },
                'PackageWeight': {
                    'UnitOfMeasurement': {'Code': 'KGS'},
                    'Weight': str(round(float(pkg.get('weight_kg', 1)), 2)),
                },
            })

        payload = {
            'RateRequest': {
                'Request': {
                    'RequestOption': 'Shop' if not service_code else 'Rate',
                    'SubVersion': '2403',
                },
                'Shipment': {
                    'Shipper': {
                        'Name': shipper.get('name', 'Shipper'),
                        'ShipperNumber': cfg.account_number,
                        'Address': self._format_address(shipper),
                    },
                    'ShipTo': {
                        'Name': to_address.get('name', 'Recipient'),
                        'Address': self._format_address(to_address),
                    },
                    'ShipFrom': {
                        'Name': shipper.get('name', 'Shipper'),
                        'Address': self._format_address(shipper),
                    },
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
                'code':         code,
                'name':         UPS_SERVICES.get(code, f'UPS Service {code}'),
                'price':        Decimal(price),
                'currency':     currency,
                'transit_days': transit,
                'guaranteed':   bool(s.get('GuaranteedDelivery')),
                'raw':          s,
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
        cfg     = self.config
        shipper = from_address or self._default_shipper()

        shipment = {
            'Shipper': {
                'Name':           shipper.get('name', ''),
                'AttentionName':  shipper.get('name', ''),
                'ShipperNumber':  cfg.account_number,
                'Phone':          {'Number': (shipper.get('phone', '') or '').replace(' ', '')},
                'Address':        self._format_address(shipper),
            },
            'ShipTo': {
                'Name':          to_address.get('name', ''),
                'AttentionName': to_address.get('name', ''),
                'Phone':         {'Number': (to_address.get('phone', '') or '').replace(' ', '')},
                'Address':       self._format_address(to_address),
            },
            'PaymentInformation': {
                'ShipmentCharge': [{'Type': '01', 'BillShipper': {'AccountNumber': cfg.account_number}}],
            },
            'Service': {'Code': service_code, 'Description': UPS_SERVICES.get(service_code, '')},
            'Package': self._build_packages(packages),
        }

        if reference:
            shipment['ReferenceNumber'] = [{'Code': '02', 'Value': reference[:35]}]

        is_international = (
            (to_address.get('country') or 'DE').upper() !=
            (shipper.get('country') or 'DE').upper()
        )
        if is_international and customs_info:
            shipment['ShipmentServiceOptions'] = {
                'InternationalForms': self._build_customs(customs_info),
            }
            if cfg.paperless_trade:
                shipment['ShipmentServiceOptions']['PaperlessShipmentIndicator'] = ''

        payload = {
            'ShipmentRequest': {
                'Request': {
                    'SubVersion': '2409',
                    'TransactionReference': {'CustomerContext': reference or 'minerva'},
                },
                'Shipment': shipment,
                'LabelSpecification': {
                    'LabelImageFormat': {'Code': cfg.label_format},
                    'LabelStockSize':   {'Height': '6', 'Width': '4'},
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

        cfg.total_shipments += 1
        cfg.last_sync_at = timezone.now()
        cfg.save(update_fields=['total_shipments', 'last_sync_at'])

        return {
            'tracking_number': tracking,
            'shipment_id':     results_data.get('ShipmentIdentificationNumber', ''),
            'label_base64':    label_b64,
            'label_format':    cfg.label_format,
            'total_charge':    Decimal(charges.get('MonetaryValue', '0')),
            'currency':        charges.get('CurrencyCode', 'USD'),
            'service_code':    service_code,
            'service_name':    UPS_SERVICES.get(service_code, ''),
        }

    # ── Tracking ──────────────────────────────────────────────────────────────

    def track(self, tracking_number: str) -> dict:
        """
        Отримати статус відправлення.
        Повертає: {tracking_number, status, status_description, estimated_delivery, events}
        """
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
            logger.error('UPS tracking parse error: %s, data: %s', e, str(data)[:300])
            return {
                'tracking_number':    tracking_number,
                'status':             'UNKNOWN',
                'status_description': 'Не вдалося отримати статус',
                'events':             [],
                'delivered':          False,
            }

    # ── Void ──────────────────────────────────────────────────────────────────

    def void_shipment(self, shipment_id: str) -> dict:
        """Анулювати відправлення (тільки в той самий день)."""
        url = f'{self.config.base_url}/api/shipments/v2409/void/cancel/{shipment_id}'
        try:
            r = requests.delete(url, headers=self._headers(), timeout=30)
            if not r.ok:
                self._handle_error(r)
            return r.json()
        except requests.Timeout:
            raise UPSError('UPS API не відповідає')
        except requests.ConnectionError as e:
            raise UPSError(f"Помилка з'єднання: {e}")

    # ── Address Validation ────────────────────────────────────────────────────

    def validate_address(self, address: dict) -> dict:
        payload = {
            'XAVRequest': {
                'AddressKeyFormat': {
                    'AddressLine':        address.get('address_line', ''),
                    'PoliticalDivision2': address.get('city', ''),
                    'PoliticalDivision1': address.get('state', ''),
                    'PostcodePrimaryLow': address.get('postal', ''),
                    'CountryCode':        address.get('country', 'DE'),
                },
            },
        }
        data = self._post('/api/addressvalidation/v2/validate', payload)
        xav  = data.get('XAVResponse', {})

        candidates = xav.get('Candidate', [])
        if isinstance(candidates, dict):
            candidates = [candidates]

        suggestions = []
        for c in candidates[:3]:
            addr = c.get('AddressKeyFormat', {})
            suggestions.append({
                'address': addr.get('AddressLine', ''),
                'city':    addr.get('PoliticalDivision2', ''),
                'state':   addr.get('PoliticalDivision1', ''),
                'postal':  addr.get('PostcodePrimaryLow', ''),
                'country': addr.get('CountryCode', ''),
                'quality': c.get('AddressClassification', {}).get('Description', ''),
            })

        return {
            'valid':       bool(candidates),
            'ambiguous':   len(candidates) > 1,
            'suggestions': suggestions,
            'quality':     xav.get('AddressClassification', {}).get('Description', ''),
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _default_shipper(self) -> dict:
        cfg = self.config
        return {
            'name':         cfg.shipper_name,
            'address_line': cfg.shipper_address,
            'city':         cfg.shipper_city,
            'state':        cfg.shipper_state,
            'postal':       cfg.shipper_postal,
            'country':      cfg.shipper_country,
            'phone':        cfg.shipper_phone,
        }

    def _format_address(self, addr: dict) -> dict:
        result = {
            'AddressLine': [addr.get('address_line', '')],
            'City':        addr.get('city', ''),
            'PostalCode':  addr.get('postal', ''),
            'CountryCode': (addr.get('country') or 'DE').upper(),
        }
        if addr.get('state'):
            result['StateProvinceCode'] = addr['state'].upper()
        return result

    def _build_packages(self, packages: list) -> list:
        result = []
        for pkg in packages:
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
            result.append(p)
        return result

    def _build_customs(self, info: dict) -> dict:
        cfg = self.config
        items_list = info.get('items', [])
        if not items_list:
            items_list = [{
                'description': info.get('description', 'Goods'),
                'quantity':    1,
                'value':       float(info.get('value_usd', 0)),
                'weight_kg':   0.5,
                'hs_code':     '',
                'country':     cfg.shipper_country,
            }]

        products = []
        for item in items_list:
            prod = {
                'Description': item.get('description', 'Goods')[:35],
                'Unit': {
                    'Number':            str(item.get('quantity', 1)),
                    'UnitOfMeasurement': {'Code': 'PCS'},
                    'EstimatedValue':    str(round(float(item.get('value', 0)), 2)),
                    'CurrencyCode':      info.get('currency', 'EUR'),
                },
                'OriginCountryCode': item.get('country', cfg.shipper_country),
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

        invoice = {
            'FormType':             '01',
            'Product':              products,
            'ReasonForExport':      reason,
            'CurrencyCode':         info.get('currency', 'EUR'),
            'DeclarationStatement': 'I hereby certify that the information is true and correct.',
        }
        if cfg.eori_number or cfg.vat_number:
            invoice['Contacts'] = {
                'ForwardAgent': {'TaxIdentificationNumber': cfg.eori_number or cfg.vat_number},
            }
        return invoice
