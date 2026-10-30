"""Журнал запитів і обмеження ключів API (/api/v1/). Логіка лише тут:

  client_ip     адреса клієнта: від проксі DSM (TRUSTED_PROXIES) — X-Real-IP → останній X-Forwarded-For, інакше REMOTE_ADDR
  ip_allowed    «Дозволені IP-адреси» ключа (адреси й мережі CIDR)
  check_quota   «Запитів за годину / добу» (лічильники в спільному кеші, вікна — календарна година / доба)
  shape         відповідь для ключа: лише дозволені поля, «різних записів за добу» (вже бачене — завжди)
  record        рядок журналу APIRequestLog (api/middleware.APIRequestLogMiddleware) — за «Журналом» ключа
  usage         що ключ використав зараз (для картки ключа)

Хвилинний ліміт — api/throttling.APIKeyRateThrottle (свій у ключа або API_RATE_LIMIT).
"""
from __future__ import annotations

import ipaddress
import logging
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

SECRET_PARAMS = ("token", "key", "secret", "password", "session", "sig", "signature", "code")
IDENT_FIELDS = ("sku", "id", "code", "order_number")
LOG_DAYS = int(getattr(settings, "API_LOG_DAYS", 60) or 60)


def _cache():
    from core.shared_cache import cache
    return cache


# ── Адреса ────────────────────────────────────────────────────────────────────

def client_ip(request) -> str | None:
    """Хто звернувся — те саме правило, що для журналу аудиту (`core.utils.client_ip`): заголовки X-Real-IP /
    X-Forwarded-For — лише від нашого проксі (`settings.TRUSTED_PROXIES`), інакше REMOTE_ADDR."""
    from core.utils import client_ip as _client_ip
    return _client_ip(request)


def parse_networks(text: str) -> list:
    """«1.2.3.4, 10.0.0.0/24» / по рядку → мережі; ValueError — яка адреса не так."""
    out = []
    for part in (text or "").replace(";", ",").replace("\n", ",").split(","):
        part = part.split("#")[0].strip()
        if not part:
            continue
        try:
            out.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            raise ValueError(f"«{part}» — не IP-адреса і не мережа (приклад: 203.0.113.5 або 10.0.0.0/24).")
    return out


def ip_allowed(key, ip: str | None) -> bool:
    if not (key.allowed_ips or "").strip():
        return True
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in parse_networks(key.allowed_ips))
    except ValueError:
        return False


# ── Ліміти запитів (година / доба) ────────────────────────────────────────────

WINDOWS = (("limit_hour", "h", 3600, "за годину"), ("limit_day", "d", 86400, "за добу"))


def _bucket(span: int, now: float) -> tuple[int, int]:
    """Номер календарного вікна (у часовому поясі Minerva) і скільки секунд до його кінця."""
    local = datetime.fromtimestamp(now, tz=timezone.get_current_timezone())
    if span == 3600:
        start = local.replace(minute=0, second=0, microsecond=0)
    else:
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(seconds=span)
    return int(start.timestamp()), max(1, int((end - local).total_seconds()))


def check_quota(key, now: float | None = None) -> tuple[bool, int, str]:
    """(можна, через скільки секунд, пояснення). Рахує запит у годині й добі (обидва — щоб видно було
    використання), лише якщо його пропущено; перевіряє ті вікна, де в ключа є ліміт."""
    now = now or time.time()
    cache = _cache()
    counted = []
    for field, tag, span, label in WINDOWS:
        limit = getattr(key, field, None)
        bucket, left = _bucket(span, now)
        name = f"apiq:{key.pk}:{tag}:{bucket}"
        cache.add(name, 0, span + 60)
        try:
            used = cache.incr(name)              # атомарно: паралельні запити не проскочать разом за ліміт
        except ValueError:                       # ключ щойно зник із кешу
            cache.set(name, 1, span + 60)
            used = 1
        counted.append(name)
        if limit and used > limit:
            for done in counted:                 # відхилений запит не рахується
                try:
                    cache.decr(done)
                except ValueError:
                    pass
            return False, left, f"Ліміт ключа: {limit} запитів {label} — вичерпано."
    return True, 0, ""


def usage(key, now: float | None = None) -> dict:
    """Скільки ключ уже використав у поточних вікнах (картка ключа)."""
    now = now or time.time()
    cache = _cache()
    out = {}
    for field, tag, span, _ in WINDOWS:
        bucket, _left = _bucket(span, now)
        out[field] = cache.get(f"apiq:{key.pk}:{tag}:{bucket}") or 0
    out["items_per_day"] = len(cache.get(_items_name(key, now)) or [])
    return out


# ── Форма відповіді: поля й «різних записів за добу» ─────────────────────────

def fields_of(key) -> list[str]:
    return [f.strip() for f in (key.fields or "").replace("\n", ",").split(",") if f.strip()]


def _ident(item: dict):
    for name in IDENT_FIELDS:
        if item.get(name) not in (None, ""):
            return f"{name}:{item[name]}"
    return None


def _items(data):
    """(записи, як повернути змінений список, чи це один запис) — або (None, …) для інших відповідей."""
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return data["results"], "page", False
    if isinstance(data, dict) and isinstance(data.get("items"), list)             and all(isinstance(i, dict) for i in data["items"]):
        return data["items"], "items", False              # POST /stock/check/ та інші {"items": […]}
    if isinstance(data, list) and all(isinstance(i, dict) for i in data):
        return data, "list", False
    if isinstance(data, dict) and _ident(data) is not None:
        return [data], "one", True
    return None, None, False


def _items_name(key, now=None) -> str:
    bucket, _ = _bucket(86400, now or time.time())
    return f"apiq:{key.pk}:items:{bucket}"


def shape(key, data, request=None, response=None):
    """Відповідь для ключа з обмеженнями «поля» й «різних записів за добу». Інші відповіді — як є."""
    fields = fields_of(key)
    limit = key.items_per_day
    if not fields and not limit:
        return data
    items, kind, single = _items(data)
    if items is None:
        return data
    note = ""
    if limit:
        cache = _cache()
        name = _items_name(key)
        seen = list(cache.get(name) or [])
        known = set(seen)
        kept, hidden = [], 0
        for item in items:
            ident = _ident(item)
            if ident is None or ident in known:
                kept.append(item)
            elif len(known) < limit:
                known.add(ident)
                seen.append(ident)
                kept.append(item)
            else:
                hidden += 1
        cache.set(name, seen, 2 * 86400)
        if response is not None:
            response["X-Items-Per-Day"] = str(limit)
            response["X-Items-Remaining"] = str(max(0, limit - len(known)))
        if hidden:
            note = f"приховано {hidden}: ліміт {limit} різних записів за добу"
        if single and not kept:
            if response is not None:
                response.status_code = 429
            _note(request, note)
            if request is not None:
                getattr(request, "_request", request).api_log_sent = (0, [])
            return {"detail": f"Ліміт ключа: {limit} різних записів за добу вичерпано. Уже переглянуті сьогодні "
                              "записи доступні й далі; нові — завтра.", "code": "daily_items_limit",
                    "items_per_day": limit}
        items = kept
    if fields:
        items = [{k: v for k, v in item.items() if k in fields} for item in items]
    _note(request, note)
    if request is not None:                                 # журнал — те, що ключ справді отримав
        getattr(request, "_request", request).api_log_sent = (len(items), [_ident(i) for i in items])
    if kind == "one":
        return items[0]
    if kind == "list":
        return items
    out = dict(data)
    out["items" if kind == "items" else "results"] = items
    if limit:
        out["quota"] = {"items_per_day": limit, "used": len(known), "hidden": hidden}
    return out


def _note(request, text: str):
    if request is not None and text:
        target = getattr(request, "_request", request)
        target.api_log_note = text


# ── Журнал ────────────────────────────────────────────────────────────────────

def _query(request) -> str:
    pairs = []
    for name, values in request.GET.lists():
        secret = any(s in name.lower() for s in SECRET_PARAMS)
        for value in values:
            pairs.append((name, "***" if secret else value[:100]))
    return urlencode(pairs)[:500]


def should_log(key, method: str, status: int) -> bool:
    from api.models import APIKey
    mode = getattr(key, "log_mode", APIKey.LOG_ALL) if key is not None else APIKey.LOG_ALL
    if mode == APIKey.LOG_OFF:
        return False
    if mode == APIKey.LOG_ERRORS:
        return status >= 400
    if mode == APIKey.LOG_CHANGES:
        return status >= 400 or method not in ("GET", "HEAD", "OPTIONS")
    return True


def _summary(data):
    """(скільки записів, перші артикули / номери, код помилки)."""
    items, _kind, _single = _items(data)
    code = ""
    if isinstance(data, dict):
        code = str(data.get("code") or "")[:50]
    if items is None:
        return None, "", code
    idents = []
    for item in items:
        ident = _ident(item)
        if ident:
            idents.append(ident.split(":", 1)[1])
    text = ", ".join(idents[:10]) + (f" … +{len(idents) - 10}" if len(idents) > 10 else "")
    return len(items), text[:300], code


def record(request, response, started: float, key=None, key_hint: str = ""):
    """Пише рядок журналу. Ніколи не ламає відповідь API."""
    from api.models import APIRequestLog
    try:
        status = int(getattr(response, "status_code", 0) or 0)
        if key is not None and not should_log(key, request.method, status):
            return None
        data = getattr(response, "data", None)
        items, objects, code = _summary(data) if data is not None else (None, "", "")
        sent = getattr(request, "api_log_sent", None)
        if sent is not None:
            idents = [i.split(":", 1)[1] for i in sent[1] if i]
            items = sent[0]
            objects = (", ".join(idents[:10]) + (f" … +{len(idents) - 10}" if len(idents) > 10 else ""))[:300]
        note = getattr(request, "api_log_note", "") or ""
        view = ""
        ctx = getattr(response, "renderer_context", None) or {}
        drf_view = ctx.get("view")
        if drf_view is not None:
            action = getattr(drf_view, "action", None)
            view = drf_view.__class__.__name__ + (f".{action}" if action else "")
            from api.permissions import required_scope
            scope = required_scope(ctx.get("request") or request, drf_view) or ""
        else:
            scope = ""
        if not note and status >= 400 and isinstance(data, dict) and data.get("detail"):
            note = str(data["detail"])                      # пояснення відмови — у примітку, код — коротким
        return APIRequestLog.objects.create(
            key=key, key_name=(key.name if key is not None else "")[:100], key_hint=key_hint[:20],
            ip=client_ip(request), method=request.method[:8], path=request.path[:300], query=_query(request),
            view=view[:100], scope=scope[:40], status=status, code=code, items=items, objects_text=objects,
            note=note[:300],
            duration_ms=int((time.monotonic() - started) * 1000),
            user_agent=(request.META.get("HTTP_USER_AGENT", "") or "")[:200])
    except Exception:
        logger.exception("api request log")
        return None


def cleanup(days: int | None = None) -> int:
    from api.models import APIRequestLog
    edge = timezone.now() - timedelta(days=days or LOG_DAYS)
    deleted, _ = APIRequestLog.objects.filter(created_at__lt=edge).delete()
    return deleted


def stats(key, days: int = 7) -> dict:
    """Картка ключа: запити сьогодні / за N днів, помилки, адреси й що найчастіше робив."""
    from django.db.models import Count, Max
    from api.models import APIRequestLog
    now = timezone.now()
    today = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    qs = APIRequestLog.objects.filter(key=key)
    week = qs.filter(created_at__gte=now - timedelta(days=days))
    return {
        "today": qs.filter(created_at__gte=today).count(),
        "today_errors": qs.filter(created_at__gte=today, status__gte=400).count(),
        "week": week.count(),
        "ips": list(week.exclude(ip=None).values("ip").annotate(n=Count("id"), last=Max("created_at"))
                    .order_by("-last")[:10]),
        "top": list(week.values("method", "view").annotate(n=Count("id")).order_by("-n")[:6]),
        "recent": list(qs.order_by("-created_at")[:10]),
    }
