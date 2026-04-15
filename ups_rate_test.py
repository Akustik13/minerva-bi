"""
UPS Rate + Transit Time — standalone test script.
Run: python ups_rate_test.py

Fills credentials from environment or edit the constants below.
"""

import json
import os
import base64
import urllib.request
import urllib.parse
from datetime import date

# ── Credentials — edit or set env vars ───────────────────────────────────────
CLIENT_ID     = os.getenv('UPS_CLIENT_ID',     'YOUR_CLIENT_ID')
CLIENT_SECRET = os.getenv('UPS_CLIENT_SECRET', 'YOUR_CLIENT_SECRET')
ACCOUNT       = os.getenv('UPS_ACCOUNT',       'YOUR_ACCOUNT_NUMBER')
SANDBOX       = os.getenv('UPS_SANDBOX', '0') == '1'

BASE_URL = 'https://wwwcie.ups.com' if SANDBOX else 'https://onlinetools.ups.com'

# ── Test shipment ─────────────────────────────────────────────────────────────
SHIPPER = {
    'name':         'Sergey Sevskiy',
    'address_line': 'Schatzbogen 43',
    'city':         'Muenchen',
    'postal':       '81829',
    'country':      'DE',
}
RECIPIENT = {
    'city':    'GEEBUNG',
    'postal':  '4034',
    'country': 'AU',
    'address_line': '-',
}
PACKAGE = {'weight_kg': 1.0, 'length_cm': 40, 'width_cm': 30, 'height_cm': 15}
TODAY = date.today().strftime('%Y%m%d')


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _post_json(url, payload, headers):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    req  = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}')


def get_token():
    creds = base64.b64encode(f'{CLIENT_ID}:{CLIENT_SECRET}'.encode()).decode()
    status, data = _post_json(
        f'{BASE_URL}/security/v1/oauth/token',
        urllib.parse.urlencode({'grant_type': 'client_credentials'}).encode(),
        {'Authorization': f'Basic {creds}', 'Content-Type': 'application/x-www-form-urlencoded'},
    )
    token = data.get('access_token', '')
    print(f'Token: {"OK" if token else "FAILED"} (status {status})')
    return token


def rate_headers(token):
    return {
        'Authorization':  f'Bearer {token}',
        'Content-Type':   'application/json',
        'transID':        'test-minerva-001',
        'transactionSrc': 'minerva-bi',
    }


def build_shipment(*, with_tti=False):
    addr_line = SHIPPER['address_line']
    s = {
        'Shipper': {
            'Name':          SHIPPER['name'],
            'ShipperNumber': ACCOUNT,
            'Address': {
                'AddressLine': [addr_line],
                'City':        SHIPPER['city'],
                'PostalCode':  SHIPPER['postal'],
                'CountryCode': SHIPPER['country'],
            },
        },
        'ShipTo': {
            'Name': 'Recipient',
            'Address': {
                'AddressLine': [RECIPIENT.get('address_line', '-')],
                'City':        RECIPIENT['city'],
                'PostalCode':  RECIPIENT['postal'],
                'CountryCode': RECIPIENT['country'],
            },
        },
        'ShipFrom': {
            'Name': SHIPPER['name'],
            'Address': {
                'AddressLine': [addr_line],
                'City':        SHIPPER['city'],
                'PostalCode':  SHIPPER['postal'],
                'CountryCode': SHIPPER['country'],
            },
        },
        'PaymentDetails': {
            'ShipmentCharge': {
                'Type': '01',
                'BillShipper': {'AccountNumber': ACCOUNT},
            },
        },
        'ShipmentRatingOptions': {'NegotiatedRatesIndicator': 'Y'},
        'InvoiceLineTotal': {'CurrencyCode': 'EUR', 'MonetaryValue': '1.00'},
        'Package': [{
            'PackagingType': {'Code': '02', 'Description': 'Customer Supplied Package'},
            'Dimensions': {
                'UnitOfMeasurement': {'Code': 'CM'},
                'Length': str(int(PACKAGE['length_cm'])),
                'Width':  str(int(PACKAGE['width_cm'])),
                'Height': str(int(PACKAGE['height_cm'])),
            },
            'PackageWeight': {
                'UnitOfMeasurement': {'Code': 'KGS'},
                'Weight': str(max(1.0, PACKAGE['weight_kg'])),
            },
        }],
    }
    if with_tti:
        s['DeliveryTimeInformation'] = {
            'PackageBillType': '02',
            'Pickup': {'Date': TODAY, 'Time': '1000'},
        }
    return s


# ── Test cases ────────────────────────────────────────────────────────────────
def test(label, url, payload, headers):
    print(f'\n{"-"*60}')
    print(f'TEST: {label}')
    print(f'URL:  {url}')
    status, data = _post_json(url, payload, headers)
    print(f'HTTP: {status}')
    if status != 200:
        print('ERROR:', json.dumps(data, indent=2))
        return None
    shipments = data.get('RateResponse', {}).get('RatedShipment', [])
    if isinstance(shipments, dict):
        shipments = [shipments]
    print(f'Services returned: {len(shipments)}')
    for s in shipments[:3]:
        code    = s.get('Service', {}).get('Code', '?')
        charge  = s.get('TotalCharges', {}).get('MonetaryValue', '?')
        tti     = s.get('TimeInTransit', {})
        summary = tti.get('ServiceSummary', {})
        arrival = summary.get('EstimatedArrival', {})
        days    = arrival.get('BusinessDaysInTransit', '—')
        d_date  = arrival.get('Arrival', {}).get('Date', '—')
        print(f'  [{code}] {charge} EUR  |  ETA: {days} days, {d_date}')
    return data


def main():
    if CLIENT_ID == 'YOUR_CLIENT_ID':
        print('ERROR: set UPS_CLIENT_ID / UPS_CLIENT_SECRET / UPS_ACCOUNT env vars')
        print('  or edit the constants at the top of this file')
        return

    token = get_token()
    if not token:
        return

    h = rate_headers(token)

    # Test 1: plain Shop (baseline — should always work)
    test(
        'Shop — plain prices',
        f'{BASE_URL}/api/rating/v2205/Shop',
        {'RateRequest': {'Request': {'RequestOption': 'Shop', 'TransactionReference': {'CustomerContext': 'test'}}, 'Shipment': build_shipment()}},
        h,
    )

    # Test 2: Shop?additionalinfo=timeintransit (approach from docs)
    test(
        'Shop?additionalinfo=timeintransit + DeliveryTimeInformation',
        f'{BASE_URL}/api/rating/v2205/Shop?additionalinfo=timeintransit',
        {'RateRequest': {'Request': {'RequestOption': 'Shop', 'TransactionReference': {'CustomerContext': 'test'}}, 'Shipment': build_shipment(with_tti=True)}},
        h,
    )

    # Test 3: Shoptimeintransit endpoint
    test(
        'Shoptimeintransit endpoint + DeliveryTimeInformation',
        f'{BASE_URL}/api/rating/v2205/Shoptimeintransit',
        {'RateRequest': {'Request': {'RequestOption': 'Shoptimeintransit', 'TransactionReference': {'CustomerContext': 'test'}}, 'Shipment': build_shipment(with_tti=True)}},
        h,
    )

    # Test 4: Shoptimeintransit з PackageBillType=04 (PAK)
    s4 = build_shipment(with_tti=True)
    s4['DeliveryTimeInformation']['PackageBillType'] = '04'
    test(
        'Shoptimeintransit PackageBillType=04 (PAK)',
        f'{BASE_URL}/api/rating/v2205/Shoptimeintransit',
        {'RateRequest': {'Request': {'RequestOption': 'Shoptimeintransit', 'TransactionReference': {'CustomerContext': 'test'}}, 'Shipment': s4}},
        h,
    )

    # Test 5: TimeInTransit окремий API v1
    print(f'\n{"-"*60}')
    print('TEST: TimeInTransit API v1')
    tti_url = f'{BASE_URL}/api/shipments/v1/transittimes'
    print(f'URL:  {tti_url}')
    tti_payload = {
        'originCountryCode':      SHIPPER['country'],
        'originPostalCode':       SHIPPER['postal'],
        'destinationCountryCode': RECIPIENT['country'],
        'destinationPostalCode':  RECIPIENT['postal'],
        'weight':                 str(PACKAGE['weight_kg']),
        'weightUnitOfMeasure':    'KGS',
        'shipDate':               date.today().strftime('%Y-%m-%d'),
        'shipTime':               '1000',
        'residentialIndicator':   '02',
    }
    status, data = _post_json(tti_url, tti_payload, h)
    print(f'HTTP: {status}')
    if status != 200:
        print('ERROR:', json.dumps(data, indent=2))
    else:
        print('RAW:', json.dumps(data, indent=2)[:800])


if __name__ == '__main__':
    main()
