"""
tabele/api_logger.py — Загальний API logger для всіх зовнішніх інтеграцій.

Використання:
    from tabele.api_logger import logged_request, log_call, get_log, clear_log

    # Заміна req.get/post/etc:
    resp = logged_request('dhl', 'get_rates', 'GET', url, req.get,
                          params=params, auth=(user, pwd), timeout=20)

Лог-файли: <project_root>/logs/{service}_api.json
Маскування: перші 2 + **** + останні 2 символи (Authorization, токени, секрети).
"""
import base64 as _b64
import json
import os
import threading
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
_LOGS_DIR = os.path.join(_PROJECT_ROOT, 'logs')

_DEFAULT_MAX = 20
_lock = threading.Lock()

_SENSITIVE_KEYS = frozenset({
    'authorization', 'access_token', 'token', 'api_key', 'api_secret',
    'client_secret', 'client_id', 'password', 'secret',
})


# ── Masking ────────────────────────────────────────────────────────────────────

def mask(value: str) -> str:
    """ab****cd — показує перші 2 і останні 2 символи."""
    s = str(value or '')
    if len(s) <= 4:
        return '****'
    return s[:2] + '****' + s[-2:]


def _mask_headers(headers: dict) -> dict:
    out = {}
    for k, v in (headers or {}).items():
        kl = k.lower()
        if kl == 'authorization':
            parts = str(v).split(' ', 1)
            if len(parts) == 2:
                out[k] = f'{parts[0]} {mask(parts[1])}'
            else:
                out[k] = mask(str(v))
        elif kl in ('x-auth-token', 'x-merchant-id', 'x-digikey-client-id',
                    'x-digikey-locale-site', 'x-ibm-client-id'):
            out[k] = mask(str(v))
        else:
            out[k] = v
    return out


def _mask_obj(obj, _depth=0):
    if _depth > 10:
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = k.lower()
            if (kl in _SENSITIVE_KEYS
                    or 'secret' in kl or 'password' in kl or 'token' in kl):
                out[k] = mask(str(v)) if v else v
            else:
                out[k] = _mask_obj(v, _depth + 1)
        return out
    if isinstance(obj, list):
        return [_mask_obj(i, _depth + 1) for i in obj]
    if isinstance(obj, str) and len(obj) > 300:
        return obj[:60] + f'...[+{len(obj) - 60} chars]'
    return obj


# ── Storage ────────────────────────────────────────────────────────────────────

def _log_file(service: str) -> str:
    return os.path.join(_LOGS_DIR, f'{service}_api.json')


def _get_max_entries() -> int:
    """Читає ліміт з ShippingSettings.api_log_max_entries; fallback → _DEFAULT_MAX."""
    try:
        from shipping.models import ShippingSettings
        s = ShippingSettings.get()
        return max(1, s.api_log_max_entries)
    except Exception:
        return _DEFAULT_MAX


def log_call(
    service: str,
    action: str,
    method: str,
    url: str,
    req_headers: dict = None,
    req_body=None,
    resp_status: int = None,
    resp_body=None,
    duration_ms: int = None,
    error: str = None,
):
    """Зберігає один запис у logs/{service}_api.json."""
    entry = {
        'ts':          datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
        'action':      action,
        'method':      method,
        'url':         url,
        'duration_ms': duration_ms,
        'error':       error,
        'request': {
            'headers': _mask_headers(req_headers or {}),
            'body':    _mask_obj(req_body) if req_body is not None else None,
        },
        'response': {
            'status': resp_status,
            'body':   _mask_obj(resp_body) if resp_body is not None else None,
        },
    }

    try:
        os.makedirs(_LOGS_DIR, exist_ok=True)
    except OSError:
        return

    path = _log_file(service)
    with _lock:
        try:
            entries = json.loads(open(path, encoding='utf-8').read()) if os.path.exists(path) else []
        except (json.JSONDecodeError, OSError):
            entries = []

        entries.insert(0, entry)
        entries = entries[:_get_max_entries()]

        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(entries, f, ensure_ascii=False, indent=2, default=str)
        except OSError:
            pass


def get_log(service: str) -> list:
    """Повертає список останніх записів для сервісу."""
    try:
        with open(_log_file(service), encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def clear_log(service: str) -> None:
    """Очищає лог-файл сервісу."""
    path = _log_file(service)
    with _lock:
        try:
            if os.path.exists(path):
                with open(path, 'w', encoding='utf-8') as f:
                    f.write('[]')
        except OSError:
            pass


# ── Drop-in request wrapper ────────────────────────────────────────────────────

def logged_request(service: str, action: str, method: str, url: str, req_fn, **kwargs):
    """
    Виконує HTTP-запит і логує його.

    Аргументи:
        service  — ім'я сервісу ('dhl', 'jumingo', 'fedex', 'digikey', …)
        action   — назва операції ('get_rates', 'create_shipment', …)
        method   — HTTP метод ('GET', 'POST', …)
        url      — повний URL
        req_fn   — requests.get / requests.post / etc.
        **kwargs — передаються в req_fn(url, **kwargs) без змін

    Повертає requests.Response (ту саму що і req_fn).
    """
    t0 = time.time()
    r = None
    resp_body = None
    error_str = None

    # ── Build headers for logging ──────────────────────────────────────────────
    log_headers = {}
    auth = kwargs.get('auth')
    if auth:
        try:
            creds = _b64.b64encode(f'{auth[0]}:{auth[1]}'.encode()).decode()
            log_headers['Authorization'] = f'Basic {creds}'
        except Exception:
            pass
    log_headers.update(kwargs.get('headers') or {})

    # ── Body / params for logging ──────────────────────────────────────────────
    log_body = kwargs.get('json') or kwargs.get('data') or kwargs.get('params') or None

    try:
        r = req_fn(url, **kwargs)
        try:
            resp_body = r.json()
        except Exception:
            resp_body = {'_raw': r.text[:500]}
        return r
    except Exception as e:
        error_str = str(e)
        raise
    finally:
        log_call(
            service=service,
            action=action,
            method=method,
            url=url,
            req_headers=log_headers,
            req_body=log_body,
            resp_status=r.status_code if r is not None else None,
            resp_body=resp_body,
            duration_ms=int((time.time() - t0) * 1000),
            error=error_str,
        )
