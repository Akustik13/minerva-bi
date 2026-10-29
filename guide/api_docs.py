"""
API для гіда — з коду, а не з тексту: маршрути /api/v1/ (api/urls.py), методи, право ключа
(`resource_scope` + `action_scopes` в'юшки, як `api.permissions.required_scope`), опис — перший рядок
docstring дії / класу, приклади curl і Python з тілом запиту за полями серіалізатора. Нова дія API
з'являється в гіді сама — сторінку писати не треба.
"""
import json
import re

from django.urls import URLPattern, URLResolver

PREFIX = "/api/v1/"
SKIP_METHODS = {"options", "head", "trace"}
_PARAM_RE = re.compile(r"\(\?P<(\w+)>[^)]*\)")


def _route(pattern) -> str:
    """Шаблон маршруту → «stock/{sku}/» (і для path(), і для regex роутера DRF)."""
    raw = str(pattern.pattern)
    if raw.startswith("^") or raw.endswith("$"):
        raw = raw.strip("^$")
        raw = _PARAM_RE.sub(lambda m: "{" + m.group(1) + "}", raw)
        raw = raw.replace("\\.", ".").replace("\\", "")
    else:
        raw = re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", raw)
    return raw


def _walk(patterns, prefix=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, prefix + _route(p))
        elif isinstance(p, URLPattern):
            route = prefix + _route(p)
            if "{format}" not in route:                  # варіанти «.json» роутера DRF — не окремі адреси
                yield route, p.callback


def _first_line(doc) -> str:
    for line in (doc or "").strip().splitlines():
        if line.strip():
            return line.strip()
    return ""


def _example_value(field):
    from rest_framework import serializers as s
    if isinstance(field, s.BooleanField):
        return True
    if isinstance(field, (s.IntegerField,)):
        return 1
    if isinstance(field, (s.DecimalField, s.FloatField)):
        return "9.90"
    if isinstance(field, s.ListSerializer) and isinstance(field.child, s.Serializer):
        return [_example_object(field.child, 8)]                   # позиції замовлення тощо
    if isinstance(field, (s.ListField, s.ListSerializer, s.ManyRelatedField)):
        return []
    if isinstance(field, (s.DictField, s.JSONField)):
        return {}
    if isinstance(field, s.DateTimeField):
        return "2026-10-29T09:00:00Z"
    if isinstance(field, s.DateField):
        return "2026-10-29"
    if isinstance(field, s.EmailField):
        return "kunde@example.com"
    if isinstance(field, s.ChoiceField):
        choices = list(field.choices)
        return choices[0] if choices else ""
    if isinstance(field, s.Serializer):
        return {}
    return "…"


def _example_object(serializer, limit, partial=False) -> dict:
    body = {}
    for name, f in serializer.fields.items():
        if f.read_only or name in ("id", "pk", "url", "created_at", "updated_at"):
            continue
        if partial and not f.required:
            continue
        body[name] = _example_value(f)
        if len(body) >= limit:
            break
    return body


DEFAULT_SUMMARY = {"list": "список", "retrieve": "один запис", "create": "створити", "update": "замінити цілком",
                   "partial_update": "змінити поля", "destroy": "видалити"}


def _serializer_class(cls, action):
    """Серіалізатор дії: get_serializer_class в'юшки (для create буває окремий), інакше serializer_class."""
    try:
        view = cls()
        view.action, view.request, view.format_kwarg, view.kwargs = action, None, None, {}
        return view.get_serializer_class()
    except Exception:  # noqa: BLE001 — в'юшка хоче справжній запит
        return getattr(cls, "serializer_class", None)


def _body(cls, action):
    """Приклад тіла запиту за записуваними полями серіалізатора (без ключів і службових полів)."""
    serializer_class = _serializer_class(cls, action)
    if serializer_class is None:
        return None
    try:
        body = _example_object(serializer_class(), 20, partial=action == "partial_update")
    except Exception:  # noqa: BLE001 — серіалізатор хоче контекст запиту
        return None
    return body or None


def _methods(callback):
    cls = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
    if cls is None:
        return None, []
    actions = getattr(callback, "actions", None)
    if actions:
        pairs = [(m, a) for m, a in actions.items() if m not in SKIP_METHODS]
    else:
        pairs = [(m, m) for m in ("get", "post", "put", "patch", "delete") if hasattr(cls, m)]
    return cls, pairs


def endpoints(prefix: str = "") -> list:
    """[{path, methods: [{method, action, scope, summary, body}]}] для /api/v1/<prefix>…."""
    from api import urls as api_urls
    from api.permissions import HasAPIKeyScope
    out, seen = [], set()
    for route, callback in _walk(api_urls.urlpatterns):
        if prefix and not route.startswith(prefix.strip("/")):
            continue
        cls, pairs = _methods(callback)
        if cls is None or not pairs or (route, cls) in seen or route in ("", "{format}"):
            continue
        seen.add((route, cls))
        resource = getattr(cls, "resource_scope", None)
        overrides = getattr(cls, "action_scopes", None) or {}
        uses_scopes = HasAPIKeyScope in (getattr(cls, "permission_classes", None) or [])
        is_viewset = bool(getattr(callback, "actions", None))
        methods = []
        for method, action in pairs:
            handler = getattr(cls, action, None)
            mode = overrides.get(action) or ("read" if method == "get" else "write")
            scope = f"{resource}:{mode}" if resource and uses_scopes else ""
            standard = action in DEFAULT_SUMMARY or not is_viewset          # окремі дії (check, cancel…) — без
            body = _body(cls, action) if method in ("post", "put", "patch") and standard else None   # чужих полів
            own = handler is not None and handler.__doc__ and handler.__doc__ != getattr(
                getattr(cls.__mro__[1], action, None), "__doc__", None)          # docstring саме цієї дії
            summary = (_first_line(handler.__doc__) if own else "") or DEFAULT_SUMMARY.get(action) \
                or (action.replace("_", " ") if is_viewset else _first_line(cls.__doc__))
            methods.append({"method": method.upper(), "action": action, "scope": scope, "summary": summary,
                            "body": body})
        out.append({"path": PREFIX + route, "view": cls.__name__, "methods": methods})
    return out


def _json(body) -> str:
    return "{ … }" if body is None else json.dumps(body, ensure_ascii=False)


def curl(base_url: str, path: str, method: str, body=None) -> str:
    """Приклад curl; для POST / PUT / PATCH без відомих полів — тіло «{ … }» (поля — в описі сторінки)."""
    url = base_url.rstrip("/") + path
    lines = [f'curl{" -X " + method if method != "GET" else ""} "{url}" \\', '  -H "Authorization: Token $MINERVA_KEY"']
    if method in ("POST", "PUT", "PATCH"):
        lines[-1] += " \\"
        lines.append('  -H "Content-Type: application/json" \\')
        lines.append("  -d '" + _json(body) + "'")
    return "\n".join(lines)


def python(base_url: str, path: str, method: str, body=None) -> str:
    call = method.lower()
    lines = ["import requests", "",
             'BASE = "' + base_url.rstrip("/") + '"', 'KEY = "…"  # ключ API з Minerva (REST API → API Ключі)', "",
             f'r = requests.{call}(BASE + "{path}", headers={{"Authorization": f"Token {{KEY}}"}}'
             + (f", json={json.dumps(body, ensure_ascii=False) if body is not None else '{}'}"
                if method in ("POST", "PUT", "PATCH") else "") + ", timeout=30)",
             "r.raise_for_status()", "print(r.json())" if method != "DELETE" else "print(r.status_code)"]
    return "\n".join(lines)


def directive(base_url: str):
    """Обробник {{api_endpoints[:префікс]}} для однієї сторінки: {{api_endpoints}} без префікса після інших —
    лише адреси, яких на сторінці ще не було (нові з'являються самі, без повторів)."""
    shown = []

    def render(_name, prefix):
        prefix = (prefix or "").strip("/")
        if prefix:
            shown.append(prefix)
            return render_html(base_url, prefix)
        return render_html(base_url, "", exclude=tuple(shown))
    return render


def render_html(base_url: str, prefix: str = "", exclude=()) -> str:
    """HTML для {{api_endpoints}}: кожна адреса — розгортається в методи з прикладами curl і Python."""
    import html
    items = [ep for ep in endpoints(prefix)
             if not any(ep["path"].startswith(PREFIX + p) for p in exclude)]
    if not items:
        return '<p class="gd-muted">Таких адрес API немає.</p>'
    out = ['<div class="gd-api">']
    for ep in items:
        for m in ep["methods"]:
            scope = f'<span class="gd-scope">🔑 {html.escape(m["scope"])}</span>' if m["scope"] else \
                '<span class="gd-scope gd-muted">особливі права — див. опис</span>'
            summary = f' — {html.escape(m["summary"])}' if m["summary"] else ""
            out.append(
                f'<details class="gd-ep"><summary><code class="gd-m gd-m-{m["method"].lower()}">{m["method"]}</code> '
                f'<code>{html.escape(ep["path"])}</code>{summary} {scope}</summary>'
                f'<div class="gd-code"><button type="button" class="gd-copy" title="Копіювати">📋</button>'
                f'<pre><code class="lang-bash">{html.escape(curl(base_url, ep["path"], m["method"], m["body"]))}'
                f'</code></pre></div>'
                f'<div class="gd-code"><button type="button" class="gd-copy" title="Копіювати">📋</button>'
                f'<pre><code class="lang-python">{html.escape(python(base_url, ep["path"], m["method"], m["body"]))}'
                f'</code></pre></div></details>')
    out.append("</div>")
    return "".join(out)
