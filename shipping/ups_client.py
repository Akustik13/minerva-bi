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
import time
import unicodedata
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

# Packaging code: '02'=Customer Supplied Package — required by Ship API v2409.
# '00' (Unknown) is accepted by Rating API but rejected by Ship API with 400.
# Rate fallback already overrides to '00' when '02' fails for rating queries.
PACKAGING_CUSTOMER = '02'

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
        from .ups_logger import log_call
        cache_key = f'ups_token_{self.carrier.pk}'
        token = cache.get(cache_key)
        if token:
            return token

        credentials = base64.b64encode(
            f'{self.carrier.api_key}:{self.carrier.api_secret}'.encode()
        ).decode()

        url = f'{self.base_url}/security/v1/oauth/token'
        req_headers = {
            'Authorization': f'Basic {credentials}',
            'Content-Type': 'application/x-www-form-urlencoded',
            'x-merchant-id': self.carrier.connection_uuid,
        }
        t0 = time.time()
        r = None
        resp_body = None
        error_str = None

        try:
            r = requests.post(url, headers=req_headers, data='grant_type=client_credentials', timeout=30)
            try:
                resp_body = r.json()
            except Exception:
                resp_body = {'_raw': r.text[:200]}
        except requests.Timeout:
            error_str = 'Timeout (30s)'
        except requests.ConnectionError as e:
            error_str = str(e)
        finally:
            log_call(
                action='oauth_token',
                method='POST', url=url,
                req_headers=req_headers,
                req_body={'grant_type': 'client_credentials'},
                resp_status=r.status_code if r is not None else None,
                resp_body=resp_body,
                duration_ms=int((time.time() - t0) * 1000),
                error=error_str,
                carrier_id=self.carrier.pk,
            )

        if error_str:
            raise UPSError(f'UPS OAuth помилка: {error_str}')
        if r.status_code != 200:
            raise UPSError(
                f'Помилка аутентифікації UPS [{r.status_code}]: {r.text[:200]}',
                code=r.status_code, response=r.text,
            )

        data = resp_body if isinstance(resp_body, dict) else {}
        token = data.get('access_token', '')
        if not token:
            raise UPSError('UPS Auth: відповідь не містить access_token')
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
        from .ups_logger import log_call
        url = f'{self.base_url}{endpoint}'
        headers = self._headers()
        t0 = time.time()
        r = None
        resp_body = None
        error_str = None

        try:
            r = requests.post(url, headers=headers, json=payload, timeout=60)
            if r.status_code == 401:
                # Token expired mid-session — refresh and retry once
                cache.delete(f'ups_token_{self.carrier.pk}')
                headers = self._headers()
                r = requests.post(url, headers=headers, json=payload, timeout=60)
            try:
                resp_body = r.json()
            except Exception:
                resp_body = {'_raw': r.text[:500]}
        except UPSError as e:
            error_str = str(e)
            raise
        except requests.Timeout:
            error_str = 'Timeout (60s)'
        except requests.ConnectionError as e:
            error_str = str(e)
        finally:
            log_call(
                action=endpoint.rsplit('/', 1)[-1],
                method='POST', url=url,
                req_headers=headers, req_body=payload,
                resp_status=r.status_code if r is not None else None,
                resp_body=resp_body,
                duration_ms=int((time.time() - t0) * 1000),
                error=error_str,
                carrier_id=self.carrier.pk,
            )

        if error_str == 'Timeout (60s)':
            raise UPSError('UPS API не відповідає (timeout 60с)')
        if error_str:
            raise UPSError(f"Помилка з'єднання з UPS: {error_str}")
        if not r.ok:
            self._handle_error(r)
        return resp_body

    def _get(self, endpoint: str, params: dict = None) -> dict:
        from .ups_logger import log_call
        url = f'{self.base_url}{endpoint}'
        headers = self._headers()
        t0 = time.time()
        r = None
        resp_body = None
        error_str = None

        try:
            r = requests.get(url, headers=headers, params=params, timeout=30)
            try:
                resp_body = r.json()
            except Exception:
                resp_body = {'_raw': r.text[:500]}
        except requests.Timeout:
            error_str = 'Timeout (30s)'
        except requests.ConnectionError as e:
            error_str = str(e)
        finally:
            log_call(
                action=endpoint.rsplit('/', 1)[-1],
                method='GET', url=url,
                req_headers=headers, req_body=params or None,
                resp_status=r.status_code if r is not None else None,
                resp_body=resp_body,
                duration_ms=int((time.time() - t0) * 1000),
                error=error_str,
                carrier_id=self.carrier.pk,
            )

        if error_str == 'Timeout (30s)':
            raise UPSError('UPS API не відповідає')
        if error_str:
            raise UPSError(f"Помилка з'єднання: {error_str}")
        if not r.ok:
            self._handle_error(r)
        return resp_body

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

    # Time-in-Transit API service codes → Rating API numeric codes
    _TTI_CODE_MAP = {
        '1DM': '14', '1DA': '01', '1DP': '13', '2DM': '59', '2DA': '02',
        '3DS': '12', 'GND': '03', 'WXS': '54', 'WXP': '07',
        'WDA': '08', 'WES': '65', 'STD': '11', 'WXF': '96',
    }

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

    def _build_rate_shipment(self, to_address, packages, from_address,
                            service_code=None, use_packaging_key=False):
        """
        use_packaging_key=True  → Package uses 'Packaging'   (Shoptimeintransit / Ship API)
        use_packaging_key=False → Package uses 'PackagingType' (Rate / Shop API)
        """
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
            'Package':  [self._pkg_dict(p, for_ship=use_packaging_key) for p in packages],
        }
        if service_code:
            shipment['Service'] = {'Code': service_code}
        return shipment

    def _rate_shop(self, to_address, packages, from_address):
        """
        POST /api/rating/v2409/Shop?additionalinfo=timeintransit
        Standard Shop payload (RequestOption=Shop, PackagingType=02).
        The query param requests ETA alongside rates in the same call —
        no special packaging, no fake addresses, no extra endpoints.
        Falls back to plain Shop (prices only) on any error.
        """
        shipper  = from_address or self._default_shipper()
        shipment = self._build_rate_shipment(to_address, packages, shipper)
        payload  = {'RateRequest': {
            'Request': {
                'RequestOption': 'Shop',
                'TransactionReference': {'CustomerContext': 'minerva-bi'},
            },
            'Shipment': shipment,
        }}

        # Try with ETA query param
        try:
            data = self._post(
                f'/api/rating/{_API_VERSION}/Shop?additionalinfo=timeintransit', payload)
            self._last_rate_payload  = payload
            self._last_rate_response = data
            return self._parse_rated_shipments(data, with_transit=True)
        except UPSError as e:
            logger.warning('Shop?additionalinfo=timeintransit failed (%s) — plain Shop', e)

        # Plain Shop fallback (prices only, ETA null)
        data = self._post(f'/api/rating/{_API_VERSION}/Shop', payload)
        self._last_rate_payload  = payload
        self._last_rate_response = data
        return self._parse_rated_shipments(data, with_transit=False)

    def _rate_timeintransit_single(self, to_address, packages, from_address,
                                   service_code: str, total_weight_kg: float = None):
        """
        POST /api/rating/v2409/Ratetimeintransit for ONE service code.
        Avoids 111212: each isolated call only evaluates one service, so an
        incompatible service in the Shop set can't poison the whole request.
        Returns a single rate dict (with ETA fields) or None.
        """
        from datetime import date as _date
        shipper = from_address or self._default_shipper()
        if total_weight_kg is None:
            total_weight_kg = sum(float(p.get('weight_kg', 0.5)) for p in packages)

        shipment = self._build_rate_shipment(to_address, packages, shipper,
                                             service_code=service_code)
        shipment['DeliveryTimeInformation'] = {
            'PackageBillType': '03',
            'Pickup': {'Date': _date.today().strftime('%Y%m%d'), 'Time': '1000'},
        }
        shipment['InvoiceLineTotal']    = {'CurrencyCode': 'EUR', 'MonetaryValue': '1.00'}
        shipment['ShipmentTotalWeight'] = {
            'UnitOfMeasurement': {'Code': 'KGS'},
            'Weight': str(round(total_weight_kg, 2)),
        }
        payload = {'RateRequest': {
            'Request': {
                'RequestOption': 'Ratetimeintransit',
                'TransactionReference': {'CustomerContext': 'minerva-bi'},
            },
            'Shipment': shipment,
        }}
        data  = self._post(f'/api/rating/{_API_VERSION}/Ratetimeintransit', payload)
        rates = self._parse_rated_shipments(data, with_transit=True)
        return rates[0] if rates else None

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
        self._last_rate_payload  = payload
        self._last_rate_response = data
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

    def _get_transit_times(self, to_address, packages, from_address=None) -> dict:
        """
        POST /api/shipments/v2409/transittimes
        Returns {rating_service_code: {transit_days, delivery_date}} or {} on failure.
        """
        from datetime import date as _date
        shipper      = from_address or self._default_shipper()
        total_weight = sum(float(p.get('weight_kg', 0.5)) for p in packages)
        dest_country = (to_address.get('country') or 'DE').upper()

        # Strip ZIP+4 for US addresses (94501-1192 → 94501)
        dest_postal = to_address.get('postal', '')
        if dest_country == 'US' and '-' in dest_postal:
            dest_postal = dest_postal.split('-')[0]

        payload = {
            'originCountryCode':            (shipper.get('country') or 'DE').upper(),
            'originCityName':               self._ascii_city(shipper.get('city', '')),
            'originPostalCode':             shipper.get('postal', ''),
            'destinationCountryCode':       dest_country,
            'destinationCityName':          self._ascii_city(to_address.get('city', '')),
            'destinationPostalCode':        dest_postal,
            'weight':                       str(round(total_weight, 2)),
            'weightUnitOfMeasure':          'KGS',
            'shipmentContentsValue':        '1.00',
            'shipmentContentsCurrencyCode': 'EUR',
            'billType':                     '03',
            'shipDate':                     _date.today().strftime('%Y-%m-%d'),
            'shipTime':                     '10:00:00',
            'numberOfPackages':             str(len(packages)),
        }
        if to_address.get('state'):
            payload['destinationStateProvinceCode'] = to_address['state'].upper()
        if shipper.get('state'):
            payload['originStateProvinceCode'] = shipper['state'].upper()

        self._last_tti_payload  = payload
        self._last_tti_response = None
        try:
            data = self._post(f'/api/shipments/{_API_VERSION}/transittimes', payload)
            self._last_tti_response = data
            services = data.get('emsResponse', {}).get('services', [])
            if isinstance(services, dict):
                services = [services]
            result = {}
            for svc in services:
                raw_code = svc.get('serviceLevel', {}).get('code', '')
                code     = self._TTI_CODE_MAP.get(raw_code, raw_code)
                arrival  = svc.get('serviceSummary', {}).get('estimatedArrival', {})
                t_days   = arrival.get('businessDaysInTransit', '')
                arr_dt   = arrival.get('arrival', {}).get('date', '')
                if code:
                    result[code] = {
                        'transit_days':  int(t_days) if str(t_days).isdigit() else None,
                        'delivery_date': (f'{arr_dt[:4]}-{arr_dt[4:6]}-{arr_dt[6:]}'
                                         if len(arr_dt) == 8 else arr_dt),
                    }
            logger.info('UPS Transit Times: %d services', len(result))
            return result
        except UPSError as e:
            logger.warning('UPS Transit Times API failed: %s', e)
            return {}

    def _parse_rated_shipments(self, data, with_transit=False) -> list:
        """
        Parse RateResponse. with_transit=True reads TimeInTransit from Shoptimeintransit
        response and populates all ETA fields. with_transit=False leaves ETA null.
        """
        shipments = data.get('RateResponse', {}).get('RatedShipment', [])
        if isinstance(shipments, dict):
            shipments = [shipments]
        results = []
        for s in shipments:
            code   = s.get('Service', {}).get('Code', '')
            retail = s.get('TotalCharges', {})
            neg    = s.get('NegotiatedRateCharges', {})

            retail_val = retail.get('MonetaryValue', '0') or '0'
            retail_cur = retail.get('CurrencyCode', 'EUR')
            reference_total = Decimal(retail_val)

            neg_charge = neg.get('TotalCharge', {}) if neg else {}
            neg_val    = neg_charge.get('MonetaryValue') if neg_charge else None
            if neg_val:
                negotiated_total = Decimal(neg_val)
                currency         = neg_charge.get('CurrencyCode') or retail_cur
                pricing_source   = 'negotiated'
            else:
                negotiated_total = None
                currency         = retail_cur
                pricing_source   = 'reference'

            display_price = negotiated_total if negotiated_total is not None else reference_total
            savings       = (reference_total - display_price) if negotiated_total else None

            if with_transit:
                tti          = s.get('TimeInTransit', {})
                est_arrival  = tti.get('EstimatedArrival', {})
                t_days_raw   = (est_arrival.get('BusinessDaysInTransit', '') or
                                s.get('GuaranteedDelivery', {}).get('BusinessDaysInTransit', ''))
                raw_date     = est_arrival.get('Arrival', {}).get('Date', '')
                transit_int  = int(t_days_raw) if str(t_days_raw).isdigit() else None
                delivery_date = (f'{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}'
                                 if len(raw_date) == 8 else raw_date)
                delivery_label = self._fmt_eta_label(transit_int, delivery_date)
            else:
                transit_int    = None
                delivery_date  = ''
                delivery_label = None

            results.append({
                'code':                    code,
                'name':                    UPS_SERVICES.get(code, f'UPS {code}'),
                'negotiated_total':        negotiated_total,
                'reference_total':         reference_total,
                'display_price':           display_price,
                'booking_price':           display_price,
                'pricing_source':          pricing_source,
                'savings':                 savings,
                'price':                   display_price,
                'currency':                currency,
                # ETA (null when Shop fallback is used)
                'transit_days':            transit_int,
                'delivery_date':           delivery_date,
                'guaranteed':              bool(s.get('GuaranteedDelivery') or transit_int),
                'delivery_days':           transit_int,
                'delivery_date_estimated': delivery_date,
                'delivery_label':          delivery_label,
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
                'EMailAddress':  shipper.get('email', '') or '',
                'Address':       self._fmt_addr(shipper),
            },
            'ShipTo': {
                'Name':          to_address.get('name', ''),
                'AttentionName': to_address.get('name', ''),
                'Phone':         {'Number': (to_address.get('phone', '') or '').replace(' ', '')},
                'EMailAddress':  to_address.get('email', '') or '',
                'Address':       self._fmt_addr(to_address),
            },
            'ShipFrom': {
                'Name':          shipper.get('name', ''),
                'AttentionName': shipper.get('name', ''),
                'Phone':         {'Number': (shipper.get('phone', '') or '').replace(' ', '')},
                'EMailAddress':  shipper.get('email', '') or '',
                'Address':       self._fmt_addr(shipper),
            },
            'PaymentInformation': {
                'ShipmentCharge': {'Type': '01', 'BillShipper': {'AccountNumber': self.carrier.connection_uuid}},
            },
            'ShipmentRatingOptions': {'NegotiatedRatesIndicator': ''},
            'Description': (customs_info or {}).get('description', 'Goods')[:50] if customs_info else 'Goods',
            'Service': {'Code': service_code, 'Description': UPS_SERVICES.get(service_code, '')},
            'Package': self._pkg_dict(packages[0], for_ship=True) if len(packages) == 1 else [self._pkg_dict(p, for_ship=True) for p in packages],
        }

        if reference:
            shipment['ReferenceNumber'] = [{'Code': '02', 'Value': reference[:35]}]

        is_intl = (
            (to_address.get('country') or 'DE').upper() !=
            (shipper.get('country') or 'DE').upper()
        )
        if is_intl and customs_info:
            shipment['ShipmentServiceOptions'] = {
                'InternationalForms': self._build_customs(
                    customs_info, invoice_number=reference, sold_to=to_address),
            }

        payload = {
            'ShipmentRequest': {
                'Request': {
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

        self._last_payload = payload  # exposed for debug logging
        data = self._post(f'/api/shipments/{_API_VERSION}/ship', payload)
        resp         = data.get('ShipmentResponse', {})
        results_data = resp.get('ShipmentResults', {})
        pkg_results  = results_data.get('PackageResults', {})
        if isinstance(pkg_results, list):
            pkg_results = pkg_results[0] if pkg_results else {}

        tracking  = results_data.get('ShipmentIdentificationNumber', '') or pkg_results.get('TrackingNumber', '')
        label_b64 = pkg_results.get('ShippingLabel', {}).get('GraphicImage', '')
        # Prefer negotiated rate (same as Rate API); fall back to retail TotalCharges
        neg_charges = results_data.get('NegotiatedRateCharges', {}).get('TotalCharge', {})
        charges     = neg_charges if neg_charges.get('MonetaryValue') else results_data.get('ShipmentCharges', {}).get('TotalCharges', {})

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

    # ── Pickup scheduling ─────────────────────────────────────────────────────

    def schedule_pickup(self, shipper: dict, pickup_date: str,
                        ready_time: str = '0900', close_time: str = '1800',
                        service_code: str = '11', packages: list = None) -> dict:
        """
        POST /api/pickups/v2205/schedule
        Планує забирання кур'єром UPS.

        pickup_date  — 'YYYYMMDD'
        ready_time   — 'HHMM' (наприклад '0900')
        close_time   — 'HHMM' (наприклад '1800')
        Повертає: {'prn': str, 'success': bool, 'error': str|None}
        """
        pkgs = packages or []
        pkg_count   = sum(p.get('quantity', 1) for p in pkgs) or 1
        total_weight = sum(float(p.get('weight_kg', 1)) * p.get('quantity', 1) for p in pkgs) or 1.0

        payload = {
            'PickupCreationRequest': {
                'RatePickupIndicator': 'N',
                'Shipper': {
                    'Account': {
                        'AccountNumber':     self.carrier.connection_uuid,
                        'AccountCountryCode': (shipper.get('country') or 'DE').upper(),
                    },
                },
                'PickupDateInfo': {
                    'CloseTime':  close_time.replace(':', ''),
                    'ReadyTime':  ready_time.replace(':', ''),
                    'PickupDate': pickup_date.replace('-', ''),
                },
                'PickupAddress': {
                    'CompanyName':          shipper.get('company') or shipper.get('name', ''),
                    'AddressLine':          shipper.get('address_line', ''),
                    'City':                 shipper.get('city', ''),
                    'PostalCode':           shipper.get('postal', ''),
                    'CountryCode':          (shipper.get('country') or 'DE').upper(),
                    'ResidentialIndicator': 'N',
                },
                'OverweightIndicator': 'N',
                'PaymentMethod': '01',
                'ShipmentDetail': {
                    'PackageCount': str(pkg_count),
                    'PackageWeight': {
                        'Weight': str(round(total_weight, 2)),
                        'UnitOfMeasurement': {'Code': 'KGS'},
                    },
                    'ContainerCode': '01',
                    'ServiceCode':   service_code,
                    'NumberOfPieces': str(pkg_count),
                },
            },
        }
        try:
            data = self._post('/api/pickups/v2205/schedule', payload)
            resp = data.get('PickupCreationResponse', {})
            status_ok = resp.get('Response', {}).get('ResponseStatus', {}).get('Code') == '1'
            prn = resp.get('PRN', '')
            if status_ok or prn:
                return {'prn': prn, 'success': True, 'error': None}
            return {'prn': '', 'success': False, 'error': str(resp)}
        except UPSError as e:
            return {'prn': '', 'success': False, 'error': str(e)}

    # ── Void ──────────────────────────────────────────────────────────────────

    def void_shipment(self, shipment_id: str) -> dict:
        """DELETE /api/shipments/v2409/void/cancel/{shipmentId}"""
        from .ups_logger import log_call
        url = f'{self.base_url}/api/shipments/{_API_VERSION}/void/cancel/{shipment_id}'
        headers = self._headers()
        t0 = time.time()
        r = None
        resp_body = None
        error_str = None

        try:
            r = requests.delete(url, headers=headers, timeout=30)
            try:
                resp_body = r.json()
            except Exception:
                resp_body = {'_raw': r.text[:500]}
        except requests.Timeout:
            error_str = 'Timeout (30s)'
        except requests.ConnectionError as e:
            error_str = str(e)
        finally:
            log_call(
                action='void_shipment',
                method='DELETE', url=url,
                req_headers=headers,
                resp_status=r.status_code if r is not None else None,
                resp_body=resp_body,
                duration_ms=int((time.time() - t0) * 1000),
                error=error_str,
                carrier_id=self.carrier.pk,
            )

        if error_str == 'Timeout (30s)':
            raise UPSError('UPS API не відповідає')
        if error_str:
            raise UPSError(f"Помилка з'єднання: {error_str}")
        if not r.ok:
            self._handle_error(r)
        return resp_body

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

    @staticmethod
    def _fmt_eta_label(days, date_str: str):
        """Format ETA as '3 дн. · 17.04' / '3 дн.' / None."""
        parts = []
        if days is not None:
            parts.append(f'{days} дн.')
        if date_str and len(date_str) == 10:
            try:
                parts.append(f"{int(date_str[8:10])}.{date_str[5:7]}")
            except Exception:
                pass
        return ' · '.join(parts) or None

    @staticmethod
    def _ascii_city(city: str) -> str:
        """Normalize city to ASCII — München → Muenchen, Köln → Koeln, etc."""
        for src, dst in (('ä','ae'),('ö','oe'),('ü','ue'),('ß','ss'),
                         ('Ä','Ae'),('Ö','Oe'),('Ü','Ue')):
            city = city.replace(src, dst)
        return unicodedata.normalize('NFKD', city).encode('ascii', 'ignore').decode().strip()

    def _fmt_addr(self, addr: dict) -> dict:
        country = (addr.get('country') or 'DE').upper()
        postal  = (addr.get('postal') or '')
        # Strip ZIP+4 suffix for US addresses (94501-1192 → 94501)
        if country == 'US' and '-' in postal:
            postal = postal.split('-')[0]
        result = {
            'City':        self._ascii_city(addr.get('city', '')),
            'PostalCode':  postal,
            'CountryCode': country,
        }
        addr_line = (addr.get('address_line') or '').strip()
        if addr_line:
            result['AddressLine'] = [addr_line]
        if addr.get('state'):
            result['StateProvinceCode'] = addr['state'].upper()
        return result

    _PKG_DESCRIPTIONS = {
        '00': 'Unknown', '01': 'UPS Letter', '02': 'Customer Supplied Package',
        '03': 'Tube', '04': 'PAK', '21': 'UPS Express Box',
        '24': 'UPS 25KG Box', '25': 'UPS 10KG Box', '30': 'Pallet',
        '2a': 'Small Express Box', '2b': 'Medium Express Box', '2c': 'Large Express Box',
    }

    def _pkg_dict(self, pkg: dict, for_ship: bool = False) -> dict:
        """for_ship=True → Ship API uses 'Packaging'; Rate API uses 'PackagingType'."""
        pkg_code = pkg.get('_pkg_override', PACKAGING_CUSTOMER)
        pkg_key  = 'Packaging' if for_ship else 'PackagingType'
        p = {
            pkg_key: {'Code': pkg_code, 'Description': self._PKG_DESCRIPTIONS.get(pkg_code, 'Customer Supplied Package')},
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

    def _build_customs(self, info: dict, invoice_number: str = '', sold_to: dict | None = None) -> dict:
        """Build InternationalForms payload for UPS Ship API."""
        from datetime import date as _date
        today      = _date.today().strftime('%Y%m%d')
        inv_number = (invoice_number or info.get('invoice_number', '') or today)[:35]
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
                    'Number':            str(item.get('quantity', 1)),
                    'UnitOfMeasurement': {'Code': 'PCS', 'Description': 'PCS'},
                    'Value':             str(round(float(item.get('value', 0)), 2)),
                },
                'OriginCountryCode': item.get('country', self.carrier.sender_country or 'DE'),
                'ProductWeight': {
                    'UnitOfMeasurement': {'Code': 'KGS', 'Description': 'KGS'},
                    'Weight': str(round(float(item.get('weight_kg', 0.5)), 2)),
                },
            }
            if item.get('hs_code'):
                prod['CommodityCode'] = item['hs_code']
            products.append(prod)

        # ReasonForExport: 01=Sale, 02=Gift, 03=Sample, 04=Return, 05=Other
        contents_map = {'SALE': '01', 'GIFT': '02', 'SAMPLE': '03', 'RETURN': '04', 'OTHER': '05'}
        reason = contents_map.get(info.get('contents_type', 'SALE').upper(), '01')

        result = {
            'FormType':            '01',
            'InvoiceDate':         today,
            'InvoiceNumber':       inv_number,
            'Product':             products,
            'ReasonForExport':     reason,
            'CurrencyCode':        info.get('currency', 'EUR'),
            'DeclarationStatement': 'I hereby certify that the information on this invoice is true and correct.',
        }

        if sold_to:
            sold_to_contact = {
                'Name':          sold_to.get('name', ''),
                'AttentionName': sold_to.get('name', ''),
                'Phone':         {'Number': (sold_to.get('phone', '') or '').replace(' ', '')},
                'EMailAddress':  sold_to.get('email', '') or '',
                'Address':       self._fmt_addr(sold_to),
            }
            result['Contacts'] = {'SoldTo': sold_to_contact}

        return result
