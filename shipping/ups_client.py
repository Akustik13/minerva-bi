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

# API versions (Rating uses v2205; Ship/Track/Pickup use v2409)
_API_VERSION        = 'v2409'
_RATING_API_VERSION = 'v2205'


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
            'transID':        str(uuid.uuid4())[:32],
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
    # '96' (Worldwide Express Freight) excluded — freight-only (68+ kg), incompatible
    # with PackagingType 00/02 for small parcels → causes UPS error 111500.
    _FALLBACK_SERVICES = ['11', '07', '08', '65', '54', '03', '02', '01', '12', '59']

    # Time-in-Transit API service codes → Rating API numeric codes
    _TTI_CODE_MAP = {
        # v1 TimeInTransit API numeric codes → Rate API service codes
        '01': '07',  # UPS Express+ / Worldwide Express
        '05': '08',  # UPS Expedited / Worldwide Expedited
        '28': '65',  # UPS Express Saver / Worldwide Saver
        '29': '96',  # UPS Worldwide Express Freight
        '11': '11',  # UPS Standard
        '54': '54',  # UPS Worldwide Express Plus
        # Legacy text codes (older API versions)
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
            if not rates:
                rates = self._rate_fallback(to_address, packages, from_address)
        except UPSError as e:
            logger.info('UPS Shop failed (%s), switching to per-service fallback', e)
            rates = self._rate_fallback(to_address, packages, from_address)

        # Enrich with transit times from separate TimeInTransit v1 API
        if rates:
            transit = self._get_transit_times(to_address, packages, from_address)
            if transit:
                for rate in rates:
                    tti = transit.get(rate['code'])
                    if tti:
                        rate['transit_days']            = tti['transit_days']
                        rate['delivery_days']           = tti['transit_days']
                        rate['delivery_date']           = tti['delivery_date']
                        rate['delivery_date_estimated'] = tti['delivery_date']
                        rate['guaranteed']              = bool(tti['transit_days'])
                        rate['delivery_label']          = self._fmt_eta_label(
                            tti['transit_days'], tti['delivery_date'])
        return rates

    def _build_rate_shipment(self, to_address, packages, from_address,
                            service_code=None, use_packaging_key=False):
        """
        use_packaging_key=True  → Package uses 'Packaging'   (Shoptimeintransit / Ship API)
        use_packaging_key=False → Package uses 'PackagingType' (Rate / Shop API)

        UPS rule: Shipper.ShipperNumber country must match Shipper.Address country.
        Solution:
          - Shipper   = carrier account holder (always DE + ShipperNumber)
          - ShipFrom  = actual physical pickup address (can be US when swapped)
          - ShipTo    = recipient
        """
        pickup  = from_address or self._default_shipper()   # physical pickup location
        account = self._default_shipper()                   # carrier account (billing, always DE)
        shipment = {
            'Shipper': {
                'Name':          account.get('name', 'Shipper'),
                'ShipperNumber': self.carrier.connection_uuid,
                'Address':       self._fmt_addr(account),
            },
            'ShipTo':   {'Name': to_address.get('name', 'Recipient'), 'Address': self._fmt_addr(to_address, addr_fallback=True)},
            'ShipFrom': {'Name': pickup.get('name', 'Shipper'),       'Address': self._fmt_addr(pickup)},
            'PaymentDetails': {
                'ShipmentCharge': {
                    'Type': '01',
                    'BillShipper': {'AccountNumber': self.carrier.connection_uuid},
                },
            },
            'ShipmentRatingOptions': {'NegotiatedRatesIndicator': 'Y'},
            'Package': [self._pkg_dict(p, for_ship=use_packaging_key) for p in packages],
        }
        if service_code:
            shipment['Service'] = {'Code': service_code}
        return shipment

    def _rate_shop(self, to_address, packages, from_address):
        """POST /api/rating/v2205/Shop — prices for all services. ETA added separately."""
        pickup   = from_address or self._default_shipper()
        shipment = self._build_rate_shipment(to_address, packages, pickup)
        # is_intl = physical ShipFrom country ≠ ShipTo country
        is_intl  = (to_address.get('country', 'DE').upper() !=
                    (pickup.get('country') or 'DE').upper())
        if is_intl:
            shipment['InvoiceLineTotal'] = {'CurrencyCode': 'EUR', 'MonetaryValue': '1.00'}

        payload = {'RateRequest': {
            'Request': {
                'RequestOption': 'Shop',
                'TransactionReference': {'CustomerContext': 'minerva-bi'},
            },
            'Shipment': shipment,
        }}
        data = self._post(f'/api/rating/{_RATING_API_VERSION}/Shop', payload)
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
            'PackageBillType': '02',
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
        data  = self._post(f'/api/rating/{_RATING_API_VERSION}/Ratetimeintransit', payload)
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
        data = self._post(f'/api/rating/{_RATING_API_VERSION}/Rate', payload)
        self._last_rate_payload  = payload
        self._last_rate_response = data
        return self._parse_rated_shipments(data)

    # Service codes valid only for domestic US shipments (error 111210 on intl routes)
    _DOMESTIC_ONLY_SERVICES = {'01', '02', '03', '12', '13', '14', '59'}

    def _rate_fallback(self, to_address, packages, from_address):
        """Перебирає service codes по одному, повертає ті що відповіли успішно.
        Domestic-only codes (01/02/03/12/13/14/59) skipped for international routes."""
        pickup = from_address or self._default_shipper()
        is_intl = (pickup.get('country') or 'DE').upper() != (to_address.get('country') or 'DE').upper()

        results = []
        for code in self._FALLBACK_SERVICES:
            if is_intl and code in self._DOMESTIC_ONLY_SERVICES:
                continue
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
        POST /api/shipments/v1/transittimes
        Returns {rating_service_code: {transit_days, delivery_date}} or {} on failure.
        """
        from datetime import date as _date
        shipper      = from_address or self._default_shipper()
        total_weight = max(1.0, sum(float(p.get('weight_kg', 0.5)) for p in packages))
        dest_country = (to_address.get('country') or 'DE').upper()

        dest_postal = to_address.get('postal', '')
        if dest_country == 'US' and '-' in dest_postal:
            dest_postal = dest_postal.split('-')[0]

        payload = {
            'originCountryCode':      (shipper.get('country') or 'DE').upper(),
            'originPostalCode':       shipper.get('postal', ''),
            'destinationCountryCode': dest_country,
            'destinationPostalCode':  dest_postal,
            'weight':                 str(round(total_weight, 2)),
            'weightUnitOfMeasure':    'KGS',
            'shipDate':               _date.today().strftime('%Y-%m-%d'),
            'shipTime':               '10:00:00',
            'residentialIndicator':   '02',
        }
        if to_address.get('city'):
            payload['destinationCityName'] = self._ascii_city(to_address['city'])
        if shipper.get('city'):
            payload['originCityName'] = self._ascii_city(shipper['city'])
        if to_address.get('state'):
            payload['destinationStateProvinceCode'] = to_address['state'].upper()
        if shipper.get('state'):
            payload['originStateProvinceCode'] = shipper['state'].upper()

        self._last_tti_payload  = payload
        self._last_tti_response = None
        try:
            data = self._post('/api/shipments/v1/transittimes', payload)
            self._last_tti_response = data
            services = data.get('emsResponse', {}).get('services', [])
            if isinstance(services, dict):
                services = [services]
            result = {}
            for svc in services:
                raw_code = svc.get('serviceLevel', '')  # v1: string, not dict
                code     = self._TTI_CODE_MAP.get(raw_code, raw_code)
                t_days   = svc.get('businessTransitDays', '')
                d_date   = svc.get('deliveryDate', '')  # already YYYY-MM-DD in v1
                if code:
                    result[code] = {
                        'transit_days':  int(t_days) if str(t_days).isdigit() else None,
                        'delivery_date': d_date,
                    }
            logger.info('UPS Transit Times v1: %d services', len(result))
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
                # Shoptimeintransit response: TimeInTransit.ServiceSummary.EstimatedArrival
                tti         = s.get('TimeInTransit', {})
                svc_summary = tti.get('ServiceSummary', {})
                est_arrival = svc_summary.get('EstimatedArrival', {})
                t_days_raw  = (est_arrival.get('BusinessDaysInTransit', '') or
                               s.get('GuaranteedDelivery', {}).get('BusinessDaysInTransit', ''))
                raw_date    = est_arrival.get('Arrival', {}).get('Date', '') or est_arrival.get('Date', '')
                transit_int   = int(t_days_raw) if str(t_days_raw).isdigit() else None
                delivery_date = raw_date  # already YYYY-MM-DD from Shoptimeintransit
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
        pickup  = from_address or self._default_shipper()  # physical pickup (can be US)
        account = self._default_shipper()                  # carrier account holder (always DE)
        label_format = 'PDF'

        shipment = {
            # Shipper = account holder (billing) — always carrier's DE address + ShipperNumber
            'Shipper': {
                'Name':          account.get('name', ''),
                'AttentionName': account.get('name', ''),
                'ShipperNumber': self.carrier.connection_uuid,
                'Phone':         {'Number': (account.get('phone', '') or '').replace(' ', '')},
                'EMailAddress':  account.get('email', '') or '',
                'Address':       self._fmt_addr(account),
            },
            'ShipTo': {
                'Name':          to_address.get('name', ''),
                'AttentionName': to_address.get('name', ''),
                'Phone':         {'Number': (to_address.get('phone', '') or '').replace(' ', '')},
                'EMailAddress':  to_address.get('email', '') or '',
                'Address':       self._fmt_addr(to_address),
            },
            # ShipFrom = physical pickup location (can differ from Shipper when swapped)
            'ShipFrom': {
                'Name':          pickup.get('name', ''),
                'AttentionName': pickup.get('name', ''),
                'Phone':         {'Number': (pickup.get('phone', '') or '').replace(' ', '')},
                'EMailAddress':  pickup.get('email', '') or '',
                'Address':       self._fmt_addr(pickup),
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
            (pickup.get('country') or 'DE').upper()
        )
        if is_intl and customs_info:
            # InvoiceLineTotal at Shipment level (required for international)
            items_list = customs_info.get('items') or []
            total_val = float(
                customs_info.get('value_usd') or
                sum(float(i.get('value', 0)) for i in items_list) or 0
            )
            if total_val > 0:
                shipment['InvoiceLineTotal'] = {
                    'CurrencyCode':  customs_info.get('currency', 'USD'),
                    'MonetaryValue': str(round(total_val, 2)),
                }
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

        # Customs form (commercial invoice) — present for international shipments
        forms = results_data.get('Form', [])
        if isinstance(forms, dict):
            forms = [forms]
        customs_b64 = ''
        for form in forms:
            img = form.get('Image', {})
            if img.get('GraphicImage'):
                customs_b64 = img['GraphicImage']
                break

        return {
            'tracking_number': tracking,
            'shipment_id':     results_data.get('ShipmentIdentificationNumber', ''),
            'label_base64':    label_b64,
            'label_format':    label_format,
            'customs_base64':  customs_b64,
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

            # UPS API: currentStatus has {description, code} but NO 'type'.
            # The 'type' (D/I/X/M/P) is only in activity[].status.
            # Bug fix: get status_type from activity[0].status, not currentStatus.
            current_status   = package.get('currentStatus', {})
            activity_list    = package.get('activity', [])
            first_act_status = activity_list[0].get('status', {}) if activity_list else {}

            status_type = first_act_status.get('type', '')
            status_description = (
                current_status.get('description', '') or
                first_act_status.get('description', '')
            ).strip()

            events = []
            for act in activity_list:
                loc        = act.get('location', {}).get('address', {})
                date       = act.get('date', '')
                time_      = act.get('time', '')
                act_status = act.get('status', {})   # Bug fix: description is nested here
                events.append({
                    'date':        f'{date[:4]}-{date[4:6]}-{date[6:]}' if len(date) == 8 else date,
                    'time':        f'{time_[:2]}:{time_[2:4]}' if len(time_) >= 4 else time_,
                    'description': act_status.get('description', '').strip(),
                    'status_type': act_status.get('type', ''),
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

            # Actual delivery datetime for delivered packages (events newest-first)
            actual_delivery = ''
            if status_type == 'D' and events:   # Bug fix: was status.get('type') == 'D'
                ev = events[0]
                ev_date = ev.get('date', '')    # "YYYY-MM-DD"
                ev_time = ev.get('time', '')    # "HH:MM"
                if ev_date:
                    actual_delivery = f"{ev_date}T{ev_time}:00" if ev_time else ev_date

            return {
                'tracking_number':    tracking_number,
                'status':             status_type,           # Bug fix: was status.get('type', '')
                'status_description': status_description,    # Bug fix: was status.get('description', '')
                'estimated_delivery': est_delivery,
                'actual_delivery':    actual_delivery,
                'location':           events[0]['location'] if events else '',
                'events':             events,
                'delivered':          status_type == 'D',   # Bug fix: was status.get('type') == 'D'
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
                        service_code: str = '11', packages: list = None,
                        destination_country: str = 'DE',
                        pickup_point: str = 'RECEPTION',
                        tracking_number: str = '') -> dict:
        """
        POST /api/pickupcreation/v1/pickup
        Планує забирання кур'єром UPS.

        pickup_date  — 'YYYYMMDD'
        ready_time   — 'HHMM' (наприклад '0900')
        close_time   — 'HHMM' (наприклад '1800')
        Повертає: {'prn': str, 'success': bool, 'error': str|None}
        """
        pkgs = packages or []
        pkg_count   = sum(p.get('quantity', 1) for p in pkgs) or 1
        total_weight = sum(float(p.get('weight_kg', 1)) * p.get('quantity', 1) for p in pkgs) or 1.0

        phone = (shipper.get('phone', '') or '').replace(' ', '').lstrip('+')
        payload = {
            'PickupCreationRequest': {
                'RatePickupIndicator': 'N',
                'Shipper': {
                    'Account': {
                        'AccountNumber':      self.carrier.connection_uuid,
                        'AccountCountryCode': (shipper.get('country') or 'DE').upper(),
                    },
                },
                'PickupDateInfo': {
                    'PickupDate': pickup_date.replace('-', ''),
                    'ReadyTime':  ready_time.replace(':', ''),
                    'CloseTime':  close_time.replace(':', ''),
                },
                'PickupAddress': {
                    'CompanyName':  shipper.get('company') or shipper.get('name', ''),
                    'ContactName':  shipper.get('name', ''),
                    'AddressLine':  shipper.get('address_line', ''),
                    'City':         shipper.get('city', ''),
                    'PostalCode':   shipper.get('postal', ''),
                    'CountryCode':          (shipper.get('country') or 'DE').upper(),
                    'ResidentialIndicator': 'N',
                    'PickupPoint':          pickup_point,
                    'Phone':                {'Number': phone},
                },
                'AlternateAddressIndicator': 'Y',
                'PickupPiece': [{
                    'ServiceCode':            service_code.zfill(3),
                    'Quantity':               str(pkg_count),
                    'DestinationCountryCode': destination_country.upper(),
                    'ContainerCode':          '01',
                }],
                'TotalWeight': {
                    'Weight':            str(round(total_weight, 1)),
                    'UnitOfMeasurement': 'KGS',
                },
                'OverweightIndicator': 'N',
                'PaymentMethod': '01',
                **({'TrackingData': [{'TrackingNumber': tracking_number}]}
                   if tracking_number else {}),
            },
        }
        try:
            data = self._post('/api/pickupcreation/v1/pickup', payload)
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
            'state':        c.sender_state or '',
            'postal':       c.sender_zip or '',
            'country':      c.sender_country or 'DE',
            'phone':        c.sender_phone or '',
            'email':        c.sender_email or '',
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

    @staticmethod
    def _split_addr_line(line: str, max_len: int = 35) -> list:
        """Split a long address line into ≤max_len-char chunks (word-aware, max 3 lines)."""
        if len(line) <= max_len:
            return [line]
        words = line.split()
        chunks, current = [], ""
        for word in words:
            if not current:
                current = word[:max_len]
            elif len(current) + 1 + len(word) <= max_len:
                current += " " + word
            else:
                chunks.append(current)
                current = word[:max_len]
        if current:
            chunks.append(current)
        return chunks[:3]  # UPS allows max 3 address lines

    def _fmt_addr(self, addr: dict, addr_fallback: bool = False) -> dict:
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
            # UPS requires each address line ≤ 35 chars.
            # Respect explicit newlines entered by the user (line order matters),
            # then word-wrap each part individually.
            explicit_parts = [p.strip() for p in addr_line.splitlines() if p.strip()]
            if len(explicit_parts) > 1:
                lines = []
                for part in explicit_parts:
                    lines.extend(self._split_addr_line(part))
                result['AddressLine'] = lines[:3]
            else:
                result['AddressLine'] = self._split_addr_line(addr_line)
        elif addr_fallback:
            result['AddressLine'] = ['-']
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
                'Weight': str(round(max(0.1, float(pkg.get('weight_kg', 0.1))), 2)),
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
            qty       = max(1, int(item.get('quantity', 1)))
            total_val = float(item.get('value', 0))
            unit_val  = round(total_val / qty, 4)  # price per unit, not total
            prod = {
                'Description': item.get('description', 'Goods')[:35],
                'Unit': {
                    'Number':            str(qty),
                    'UnitOfMeasurement': {'Code': 'PCS', 'Description': 'PCS'},
                    'Value':             str(unit_val),  # per-unit value
                },
                'OriginCountryCode': item.get('country', self.carrier.sender_country or 'DE'),
                'ProductWeight': {
                    'UnitOfMeasurement': {'Code': 'KGS', 'Description': 'KGS'},
                    # weight_kg is per-unit; ProductWeight = total for the line
                    'Weight': str(round(float(item.get('weight_kg', 0.1)) * qty, 3)),
                },
            }
            if item.get('hs_code'):
                prod['CommodityCode'] = item['hs_code']
            products.append(prod)

        # ReasonForExport: UPS expects text values ("Sale", "Gift", etc.), not codes
        reason_map = {
            'SALE': 'Sale', 'GIFT': 'Gift', 'SAMPLE': 'Sample',
            'RETURN': 'Return', 'OTHER': 'Other',
        }
        reason = reason_map.get(info.get('contents_type', 'SALE').upper(), 'Sale')

        result = {
            'FormType':             '01',
            'InvoiceDate':          today,
            'InvoiceNumber':        inv_number,
            'Product':              products,
            'ReasonForExport':      reason,
            'CurrencyCode':         info.get('currency', 'EUR'),
            'DeclarationStatement': 'I hereby certify that the information on this invoice is true and correct.',
        }

        # TermsOfShipment (Incoterm): DAP, DDP, EXW, FOB, CIF …
        terms = info.get('terms_of_shipment', '')
        if terms:
            result['TermsOfShipment'] = terms

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
