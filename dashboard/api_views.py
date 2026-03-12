"""
dashboard/api_views.py — API Overview + Developer Console proxy
"""
import json
import time

from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST


# ── API Overview ──────────────────────────────────────────────────────────────

def api_index(request):
    context = admin.site.each_context(request)

    # Internal REST API
    from api.models import APIKey
    api_keys = APIKey.objects.filter(is_active=True)
    api_keys_count = api_keys.count()

    # Shipping carriers
    from shipping.models import Carrier
    jumingo_carrier = Carrier.objects.filter(carrier_type='jumingo', is_active=True).first()
    dhl_carrier     = Carrier.objects.filter(carrier_type='dhl', is_active=True).first()
    dhl_track_carrier = (Carrier.objects
                         .filter(carrier_type='dhl', is_active=True)
                         .exclude(api_key='')
                         .first())

    # DigiKey
    try:
        from bots.models import DigiKeyConfig
        digikey = DigiKeyConfig.objects.filter(pk=1).first()
        digikey_ok = bool(digikey and digikey.client_id and digikey.client_secret)
    except Exception:
        digikey    = None
        digikey_ok = False

    dhl_track_ok = bool(dhl_track_carrier)

    context.update({
        'api_keys_count':  api_keys_count,
        'api_keys':        api_keys,
        'jumingo_carrier': jumingo_carrier,
        'dhl_carrier':     dhl_carrier,
        'dhl_track_ok':    dhl_track_ok,
        'digikey':         digikey,
        'digikey_ok':      digikey_ok,
    })
    return render(request, 'dashboard/api_index.html', context)

api_index = staff_member_required(api_index)


# ── Developer Console ─────────────────────────────────────────────────────────

def api_console(request):
    context = admin.site.each_context(request)

    # Pre-populate presets based on configured integrations
    from shipping.models import Carrier
    from api.models import APIKey

    carriers      = Carrier.objects.filter(is_active=True)
    active_tokens = APIKey.objects.filter(is_active=True).values('name', 'key')

    try:
        from bots.models import DigiKeyConfig
        digikey = DigiKeyConfig.objects.filter(pk=1).first()
    except Exception:
        digikey = None

    context.update({
        'carriers':      carriers,
        'active_tokens': list(active_tokens),
        'digikey':       digikey,
    })
    return render(request, 'dashboard/api_console.html', context)

api_console = staff_member_required(api_console)


# ── Proxy endpoint ────────────────────────────────────────────────────────────

@staff_member_required
@require_POST
def api_proxy(request):
    """Makes an HTTP request server-side and returns the result.
    Only for staff — developer use only."""
    import requests as req

    try:
        payload = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': 'Invalid JSON payload'}, status=400)

    url     = (payload.get('url') or '').strip()
    method  = (payload.get('method') or 'GET').upper()
    headers = payload.get('headers') or {}
    body    = payload.get('body') or ''
    auth_cfg = payload.get('auth') or {}
    timeout = min(int(payload.get('timeout') or 15), 60)

    if not url:
        return JsonResponse({'error': 'URL is required'}, status=400)
    if method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'):
        return JsonResponse({'error': f'Invalid method: {method}'}, status=400)

    # Auth
    req_auth = None
    auth_type = auth_cfg.get('type', '')
    if auth_type == 'basic':
        req_auth = (auth_cfg.get('user', ''), auth_cfg.get('pass', ''))
    elif auth_type == 'bearer':
        headers = dict(headers)
        headers['Authorization'] = f"Bearer {auth_cfg.get('token', '')}"
    elif auth_type == 'token':
        headers = dict(headers)
        headers['Authorization'] = f"Token {auth_cfg.get('token', '')}"

    # Body
    req_data = body.encode('utf-8') if body and method not in ('GET', 'HEAD') else None

    request_info = {
        'url':     url,
        'method':  method,
        'headers': {k: v for k, v in headers.items() if k.lower() != 'authorization'},
        'auth':    auth_type or 'none',
        'body':    body or None,
    }

    t0 = time.time()
    try:
        resp = req.request(
            method,
            url,
            headers=headers,
            data=req_data,
            auth=req_auth,
            timeout=timeout,
            allow_redirects=True,
        )
        duration_ms = int((time.time() - t0) * 1000)

        # Parse response body
        try:
            resp_parsed = resp.json()
            body_str    = json.dumps(resp_parsed, ensure_ascii=False, indent=2)
            is_json     = True
        except Exception:
            body_str = resp.text[:100_000]
            is_json  = False

        return JsonResponse({
            'ok':          resp.ok,
            'status':      resp.status_code,
            'status_text': resp.reason,
            'headers':     dict(resp.headers),
            'body':        body_str,
            'is_json':     is_json,
            'duration_ms': duration_ms,
            'request':     request_info,
        })

    except req.exceptions.Timeout:
        return JsonResponse({'error': f'Timeout після {timeout}s'})
    except req.exceptions.ConnectionError as e:
        return JsonResponse({'error': f'Connection error: {str(e)[:200]}'})
    except Exception as e:
        return JsonResponse({'error': f'{type(e).__name__}: {str(e)[:300]}'})
