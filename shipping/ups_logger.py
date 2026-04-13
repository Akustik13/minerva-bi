"""
UPS API request/response logger.
Зберігає останні 20 записів у logs/ups_api.json.
Паролі / токени маскуються: перші 2 + **** + останні 2 символи.
"""
import json
import os
import threading
from datetime import datetime

# Файл зберігається в <project_root>/logs/ups_api.json
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
LOG_FILE = os.path.join(_PROJECT_ROOT, 'logs', 'ups_api.json')
_DEFAULT_MAX = 20

_lock = threading.Lock()


def _get_max_entries() -> int:
    """Читає ліміт із ShippingSettings (з БД). Fallback → _DEFAULT_MAX."""
    try:
        from shipping.models import ShippingSettings
        return max(1, ShippingSettings.get().ups_log_max_entries)
    except Exception:
        return _DEFAULT_MAX

# Назви ключів, значення яких треба маскувати
_SENSITIVE_KEYS = frozenset({
    'authorization', 'access_token', 'token', 'api_key', 'api_secret',
    'client_secret', 'password', 'secret',
})


def mask(value: str) -> str:
    """Маскує рядок: показує перші 2 і останні 2 символи, решта ****."""
    s = str(value or '')
    if len(s) <= 4:
        return '****'
    return s[:2] + '****' + s[-2:]


def _mask_headers(headers: dict) -> dict:
    """Маскує Authorization (зберігаючи префікс Basic/Bearer) та x-merchant-id."""
    out = {}
    for k, v in (headers or {}).items():
        kl = k.lower()
        if kl == 'authorization':
            parts = str(v).split(' ', 1)
            if len(parts) == 2:
                out[k] = f'{parts[0]} {mask(parts[1])}'
            else:
                out[k] = mask(str(v))
        elif kl == 'x-merchant-id':
            out[k] = mask(str(v))
        else:
            out[k] = v
    return out


def _mask_obj(obj, _depth=0):
    """Рекурсивно маскує чутливі поля в dict/list; скорочує довгі рядки (base64 тощо)."""
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
    # Скорочуємо дуже довгі рядки (напр. base64-encoded PDF мітки)
    if isinstance(obj, str) and len(obj) > 300:
        return obj[:60] + f'...[+{len(obj) - 60} chars]'
    return obj


def log_call(
    action: str,
    method: str,
    url: str,
    req_headers: dict = None,
    req_body=None,
    resp_status: int = None,
    resp_body=None,
    duration_ms: int = None,
    error: str = None,
    carrier_id=None,
):
    """Додає один запис у лог; залишає тільки останні MAX_ENTRIES записів."""
    entry = {
        'ts': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
        'action': action,
        'method': method,
        'url': url,
        'carrier_id': carrier_id,
        'duration_ms': duration_ms,
        'error': error,
        'request': {
            'headers': _mask_headers(req_headers or {}),
            'body': _mask_obj(req_body) if req_body is not None else None,
        },
        'response': {
            'status': resp_status,
            'body': _mask_obj(resp_body) if resp_body is not None else None,
        },
    }

    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    except OSError:
        return

    with _lock:
        try:
            if os.path.exists(LOG_FILE):
                with open(LOG_FILE, 'r', encoding='utf-8') as f:
                    entries = json.load(f)
            else:
                entries = []
        except (json.JSONDecodeError, OSError):
            entries = []

        entries.insert(0, entry)   # найновіший — першим
        entries = entries[:_get_max_entries()]

        try:
            with open(LOG_FILE, 'w', encoding='utf-8') as f:
                json.dump(entries, f, ensure_ascii=False, indent=2, default=str)
        except OSError:
            pass


def get_log() -> list:
    """Повертає список останніх MAX_ENTRIES записів (найновіший перший)."""
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
