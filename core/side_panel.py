"""📌 Панель праворуч у списках: клік по рядку — подробиці запису поруч, без переходу на картку (каркас v2).

Лише тут:
  • реєстр — `register` / `get`: свої панелі (модулі в `PROVIDERS`: sales/side_panel.py, inventory/side_panel.py,
    crm/side_panel.py), загальні для списків з `GENERIC` (core/panel_generic.py, за замовчуванням вимкнені),
    ніколи — `NEVER_APPS` / `NEVER_MODELS` (секрети, приватне); модуль, якого немає або який упав, — без панелі;
  • права — `allowed` (Perm розділу / дії / зміни: «app.codename», «module:<app>», функція), `visible_*`,
    `can_change` — ролі Minerva (`user.has_perm` / `has_module_perms`) плюс права адмінки моделі;
  • вибір людини — `UserProfile.ui_prefs["panels"][модель]` (зміст: розділи, згорнуті, дії, ширина, колонки,
    з чого відкривати) і `["*"]` (вигляд для всіх списків); `save_prefs` — дельти {set, shared, reset}, null —
    як було, `seen` — нові розділи й дії доходять і до тих, хто вже налаштував; запис — під select_for_update
    рядка профілю, решта `ui_prefs` не змінюється;
  • HTML — `render` (оболонка), `render_head`, `render_block` і помічники для постачальників (`kv`, `data`,
    `admin_link`, `edit_html`…): усе екранується, дані записів — у translate="no";
  • `config` — JSON для static/admin/js/mv_panel.js (тег {% side_panel_config cl %}).
Швидкі зміни й дії списку для одного запису — core/panel_edit.py. Запис для панелі — з queryset адмінки моделі
(ті самі права й обмеження, що в списку); розділ, який упав, показує «Не вдалося показати цей блок.», решта
панелі працює.
"""
from __future__ import annotations

import importlib
import inspect
import json
import logging
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import date as _date, datetime as _datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Callable

from django.utils import timezone
from django.utils.html import conditional_escape, format_html, format_html_join, strip_tags
from django.utils.safestring import SafeString, mark_safe

logger = logging.getLogger(__name__)

# ── константи ──────────────────────────────────────────────────────────────────

PROVIDERS = {"sales.salesorder": "sales.side_panel", "inventory.product": "inventory.side_panel",
             "crm.customer": "crm.side_panel"}
GENERIC = ("shipping.shipment", "inventory.purchaseorder", "inventory.supplier", "inventory.inventorytransaction",
           "offers.offer", "projects.project", "shop.shoplisting", "bots.digikeylisting", "accounting.invoice",
           "tasks.task")
NEVER_APPS = {"email_assistant", "calendar_app", "rag_assistant", "ai_assistant", "api", "auth", "admin",
              "contenttypes", "sessions", "config"}
NEVER_MODELS = {"core.userprofile", "core.auditlog", "core.tenantaccount", "core.uitext", "core.moduleregistry",
                "shop.shop", "shop.shopcustomer", "shop.shopcustomersession", "shop.shopcustomertoken",
                "shop.shoppairing", "shop.shoppayment", "bots.digikeyconfig", "worktime.dantesettings",
                "jlcpcb.jlcconfig", "strategy.aisettings"}

MIN_WIDTH, MAX_WIDTH = 320, 960
PRESETS = (360, 440, 560, 720)
WIDTHS = PRESETS                      # стара назва (v1) лишається
DEFAULT_WIDTH = None                  # авто: 28 % вікна (static/admin/js/mv_panel.js)

NAV = ("top", "side", "none")
VIEW = ("scroll", "tabs")
CHIPS = ("auto", "icons", "labels")
DENSITY = ("normal", "compact")
ACTIONS_AT = ("top", "bottom")
PLACE = ("auto", "push", "overlay")
HELPERS = ("dock", "beside")
EMPTY = ("compact", "show")
SHARED_DEFAULTS = {"nav": "top", "view": "scroll", "chips": "auto", "density": "normal", "actions_at": "top",
                   "place": "auto", "helpers": "dock", "empty": "compact"}
SHARED_CHOICES = {"nav": NAV, "view": VIEW, "chips": CHIPS, "density": DENSITY, "actions_at": ACTIONS_AT,
                  "place": PLACE, "helpers": HELPERS, "empty": EMPTY}
BAR_DEFAULT, BAR_MAX, LIST_MAX = 4, 8, 40
COL_RE = r"^[a-z_][a-z0-9_]{0,63}$"
ADMIN_RE = r"^admin:[A-Za-z_][A-Za-z0-9_]{0,79}$"
TONES = ("", "ok", "warn", "err", "info")
EDIT_KINDS = ("choice", "bool", "text", "textarea", "date", "number")
PLACES = ("head", "bar", "memo", "block")
BADGE_TONES = ("new", "work", "ship", "done", "off", "ok", "warn", "err", "info")
LIST_KEYS = ("on", "blocks", "collapsed", "actions", "bar", "width", "hide_cols", "start")
RESETS = ("blocks", "actions", "layout", "list", "all")

EMPTY_TEXT = "Немає даних."
NA_TEXT = "Не стосується цього запису."
ERROR_TEXT = "Не вдалося показати цей блок."
NO_SECTIONS_TEXT = "Розділів немає — ⚙ або правий клік → «📑 Розділи»."

_COL = re.compile(COL_RE)
_ADMIN = re.compile(ADMIN_RE)
_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")      # ключі розділів, дій, змін — у id / селекторах сторінки
_DATA_KEY = re.compile(r"^[a-z][a-z0-9-]{0,30}$")         # data-* етикеток DYMO (Action.attrs)
_ICON = re.compile(r"^\s*([^\w\s]{1,8})\s+(\S.*)$", re.S)  # «🏷 Етикетка» → значок + решта
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_SAFE_SCHEMES = {"http", "https", "mailto", "tel"}
_BAD = object()

# ── опис панелі ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Badge:
    count: int | None = None
    tone: str = ""                         # TONES
    text: str = ""                         # словами — для підказки й читача екрана («бракує 1»)


def _two_args(fn):
    """Стара (v1) функція лише від запису `(obj)` → `(obj, request)`; решта — як є."""
    if fn is None or not callable(fn) or getattr(fn, "_mvp_legacy", False):
        return fn
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return fn
    if any(p.kind is p.VAR_POSITIONAL for p in params):
        return fn
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if len(positional) != 1:
        return fn

    def legacy(obj, request=None, _fn=fn):
        return _fn(obj)
    legacy._mvp_legacy = True
    legacy.__name__ = getattr(fn, "__name__", "legacy")
    return legacy


@dataclass
class Block:
    key: str
    title: str
    render: Callable                       # (obj, request) -> SafeString; "" — даних немає (порожній розділ)
    default: bool = True
    icon: str = "•"
    short: str = ""                        # підпис у підменю ≤ 14 символів; "" — title
    lazy: bool = False                     # вміст — лише окремим запитом (…/block/<key>/)
    collapsed: bool = False                # згорнутий за замовчуванням
    perm: object = None                    # Perm: None | "app.codename" | "module:<app>" | (request, obj|None) -> bool
    badge: Callable | None = None          # (obj, request) -> Badge | None — дешево: ≤ 1 запит або memo
    summary: Callable | None = None        # (obj, request) -> str — один рядок ≤ 80 символів
    applies: Callable | None = None        # (obj) -> bool без запитів; False — «не стосується»
    empty: str = ""                        # текст порожнього розділу («Немає даних.»)
    affects: tuple = ()                    # ключі Edit, після яких розділ перемальовується
    fieldset: int | None = None            # номер групи полів картки → «↗» на #fieldset-0-<i>-heading


@dataclass
class Action:
    key: str
    label: str                             # «🏷 Етикетка» — значок і коротка назва беруться звідси
    href: Callable | None = None           # (obj, request) -> адреса; "" — недоступно для цього запису
    default: bool = True
    new_tab: bool = False
    kind: str = "link"                     # link | svl (кнопка DYMO з labels/_label_actions.html)
    attrs: Callable | None = None          # svl: (obj, request) -> {"svl": "order", "order": 12, …} → data-*
    perm: object = None
    hotkey: str = ""                       # KeyboardEvent.code, напр. "KeyL"
    reason: Callable | None = None         # (obj, request) -> str: чому недоступно (кнопка вимкнена з підказкою)
    icon: str = ""
    short: str = ""

    def __post_init__(self):
        self.href = _two_args(self.href)
        self.attrs = _two_args(self.attrs)
        self.reason = _two_args(self.reason)


@dataclass
class Edit:
    key: str
    field: str
    label: str
    kind: str                              # EDIT_KINDS
    place: str = "block"                   # head (статус у шапці) | bar (перемикач серед дій) | memo | block
    choices: Callable | None = None        # (obj, request) -> [(value, label)]; None — choices поля моделі
    confirm: object = None                 # {value: текст} або (obj, value) -> текст; без прямого апострофа
    undo: bool = False
    cells: tuple = ()                      # колонки list_display, які перемальовуються в рядку списку
    hotkey: str = ""
    default: bool = True
    maxlength: int | None = None
    icon_on: str = "⭐"
    icon_off: str = "☆"
    perm: object = None
    tones: object = None                   # {value: tone} або (obj, value) -> tone — колір «пігулки» вибору


@dataclass
class Panel:
    model: str
    title: Callable                        # (obj) -> SafeString
    blocks: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    edits: list = field(default_factory=list)
    subtitle: Callable | None = None       # (obj, request) -> str
    label: Callable | None = None          # (obj) -> str — простий текст для читача екрана й повідомлень
    hide_cols: tuple = ()
    admin_perms: dict = field(default_factory=dict)   # назва дії списку → Perm замість типового правила
    memo: str | None = None                # ключ Edit з place="memo"
    extra_fields: tuple = ()               # поля моделі поза групами картки, які «Усі поля» все одно показують
    history_link: bool = True
    generic: bool = False
    seen_v1: dict = field(default_factory=dict)       # {"blocks": (...), "actions": (...)} — що було до v2

    def block(self, key):
        return next((b for b in self.blocks if b.key == key), None)

    def action(self, key):
        return next((a for a in self.actions if a.key == key), None)

    def edit(self, key):
        return next((e for e in self.edits if e.key == key), None)


# ── реєстр ─────────────────────────────────────────────────────────────────────

_registry: dict[str, Panel] = {}
_lock = threading.RLock()


def model_key(opts) -> str:
    return f"{opts.app_label}.{opts.model_name}"


def _generic_module():
    try:
        return importlib.import_module("core.panel_generic")
    except ImportError:
        return None
    except Exception as exc:  # noqa: BLE001 — модуль з помилкою не ламає панелей
        logger.warning("side panel: core.panel_generic failed to import: %s", exc)
        return None


def register(model: str, *, title, blocks, actions=(), edits=(), subtitle=None, label=None, hide_cols=(),
             admin_perms=None, memo=None, extra_fields=(), history_link=True,
             generic_blocks=("history", "fields", "related"), seen_v1=None, generic=False) -> Panel:
    """Панель списку моделі «app.model». Ключі розділів — без повторів, ключі дій і змін — без повторів разом;
    загальні розділи (`generic_blocks`: core/panel_generic.py), яких постачальник не задав, додаються в кінець."""
    blocks, actions, edits = list(blocks), list(actions), list(edits)
    keys = [b.key for b in blocks]
    if len(set(keys)) != len(keys):
        raise ValueError(f"side panel {model}: block keys repeat: {keys}")
    akeys = [a.key for a in actions] + [e.key for e in edits]
    if len(set(akeys)) != len(akeys):
        raise ValueError(f"side panel {model}: action / edit keys repeat: {akeys}")
    for k in keys + akeys:
        if not isinstance(k, str) or not _KEY.match(k):
            raise ValueError(f"side panel {model}: bad key {k!r}")
    for e in edits:
        if e.kind not in EDIT_KINDS or e.place not in PLACES:
            raise ValueError(f"side panel {model}: edit {e.key}: kind {e.kind!r} / place {e.place!r}")
    if generic_blocks:
        module = _generic_module()
        for key in generic_blocks:
            if module is None or key in keys:
                continue
            try:
                block = module.make(key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("side panel %s: generic block %s skipped: %s", model, key, exc)
                continue
            if block is not None and block.key not in keys:
                blocks.append(block)
                keys.append(block.key)
    if memo is None:
        memo = next((e.key for e in edits if e.place == "memo"), None)
    panel = Panel(model, title, blocks, actions, edits, subtitle=subtitle, label=label, hide_cols=tuple(hide_cols),
                  admin_perms=dict(admin_perms or {}), memo=memo, extra_fields=tuple(extra_fields),
                  history_link=history_link, generic=bool(generic), seen_v1=dict(seen_v1 or {}))
    _registry[model] = panel
    return panel


def _load_provider(model: str):
    name = PROVIDERS[model]
    with _lock:
        if model in _registry:
            return _registry[model]
        try:
            module = sys.modules.get(name)
            if module is None:
                importlib.import_module(name)
            else:                                   # модуль уже був, а панелі немає (реєстр скинули) — ще раз
                importlib.reload(module)
        except Exception as exc:  # noqa: BLE001 — немає модуля / помилка в ньому: без панелі, сторінка працює
            logger.warning("side panel provider %s for %s failed: %s", name, model, exc)
            return None
        return _registry.get(model)


def _load_generic(model: str):
    with _lock:
        if model in _registry:
            return _registry[model]
        module = _generic_module()
        maker = getattr(module, "generic_panel", None) if module is not None else None
        if maker is None:
            return None
        try:
            panel = maker(model)
        except Exception as exc:  # noqa: BLE001
            logger.warning("side panel: generic panel for %s failed: %s", model, exc)
            return None
        if panel is not None:
            _registry[model] = panel
        return panel


def get(model: str) -> Panel | None:
    if not isinstance(model, str) or "." not in model:
        return None
    if model in NEVER_MODELS or model.split(".", 1)[0] in NEVER_APPS:
        return None
    panel = _registry.get(model)
    if panel is not None:
        return panel
    if model in PROVIDERS:
        return _load_provider(model)
    if model in GENERIC:
        return _load_generic(model)
    return None


# ── права ──────────────────────────────────────────────────────────────────────

def allowed(perm, request, obj=None) -> bool:
    if perm is None:
        return True
    try:
        if callable(perm):
            return bool(perm(request, obj))
        if isinstance(perm, str):
            if perm.startswith("module:"):
                return bool(request.user.has_module_perms(perm[len("module:"):]))
            return bool(request.user.has_perm(perm))
    except Exception as exc:  # noqa: BLE001 — не вдалося перевірити = закрито
        logger.warning("side panel perm %r failed: %s", perm, exc)
    return False


def visible_blocks(panel: Panel, request) -> list:
    return [b for b in panel.blocks if allowed(b.perm, request)]


def visible_actions(panel: Panel, request, obj=None) -> list:
    return [a for a in panel.actions if allowed(a.perm, request, obj)]


def _request_cache(request, name) -> dict:
    cache = getattr(request, name, None)
    if cache is None:
        cache = {}
        try:
            setattr(request, name, cache)
        except Exception:  # noqa: BLE001
            pass
    return cache


def can_change(request, ma, obj=None) -> bool:
    """Право змінювати запис: адмінка моделі й ролі Minerva (`has_perm`), раз на запит."""
    if ma is None:
        return False
    cache = _request_cache(request, "_mvp_can_change")
    key = (ma.opts.label_lower, getattr(obj, "pk", None))
    if key not in cache:
        o = ma.opts
        try:
            cache[key] = bool(ma.has_change_permission(request, obj)
                              and request.user.has_perm(f"{o.app_label}.change_{o.model_name}"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("side panel can_change %s failed: %s", o.label_lower, exc)
            cache[key] = False
    return cache[key]


def field_editable(panel: Panel, ma, request, obj, name: str) -> bool:
    """Поле є в групах картки (або в `panel.extra_fields`) і не «лише для читання» для цієї людини."""
    if ma is None:
        return False
    cache = _request_cache(request, "_mvp_fields")
    key = (ma.opts.label_lower, getattr(obj, "pk", None))
    if key not in cache:
        from django.contrib.admin.utils import flatten_fieldsets
        try:
            names = set(flatten_fieldsets(ma.get_fieldsets(request, obj)))
            readonly = set(ma.get_readonly_fields(request, obj) or ())
        except Exception as exc:  # noqa: BLE001
            logger.warning("side panel fieldsets %s failed: %s", ma.opts.label_lower, exc)
            names, readonly = set(), set()
        cache[key] = (names, readonly)
    names, readonly = cache[key]
    return (name in names or name in (panel.extra_fields or ())) and name not in readonly


def visible_edits(panel: Panel, request, ma, obj=None) -> list:
    if not can_change(request, ma, obj):
        return []
    return [e for e in panel.edits if allowed(e.perm, request, obj)]


def edit_allowed(panel: Panel, edit: Edit, request, ma, obj) -> bool:
    return (can_change(request, ma, obj) and allowed(edit.perm, request, obj)
            and field_editable(panel, ma, request, obj, edit.field))


# ── запит панелі: панель, адмінка, вибір людини ────────────────────────────────

def bind(request, panel: Panel, ma=None, p: dict | None = None) -> dict:
    """Що панель обслуговує в цьому запиті (core/views.py): `request.mvp_panel`, `mvp_admin`, `mvp_prefs`."""
    p = p if p is not None else prefs(request.user, panel)
    try:
        request.mvp_panel, request.mvp_admin, request.mvp_prefs = panel, ma, p
    except Exception:  # noqa: BLE001
        pass
    return p


def panel_for(request) -> Panel | None:
    return getattr(request, "mvp_panel", None)


def _model_admin(model):
    from django.contrib import admin
    return admin.site._registry.get(model)


def admin_for(request, obj):
    ma = getattr(request, "mvp_admin", None)
    if ma is not None and (obj is None or isinstance(obj, ma.model)):
        return ma
    return _model_admin(type(obj)) if obj is not None else None


def memo(obj, key, fn):
    """`fn()` один раз на запис у цьому запиті — розділ, лічильник і підсумок ділять важку роботу."""
    store = obj.__dict__.setdefault("_mvp_memo", {})
    if key not in store:
        store[key] = fn()
    return store[key]


def _prefs_for(panel, request, p=None) -> dict:
    if p is not None:
        return p
    if getattr(request, "mvp_panel", None) is panel and getattr(request, "mvp_prefs", None) is not None:
        return request.mvp_prefs
    return prefs(request.user, panel)


def _ensure_bound(request, panel, ma, p):
    if getattr(request, "mvp_panel", None) is not panel or getattr(request, "mvp_admin", None) is None:
        bind(request, panel, ma, p)


# ── HTML для постачальників (усе екранується) ──────────────────────────────────

def _is_empty(v) -> bool:
    return v is None or (isinstance(v, str) and v == "") or (isinstance(v, (list, tuple)) and not v)


def data(v) -> SafeString:
    """Значення запису: `<span translate="no">…</span>` (браузерний перекладач не чіпає), порожнє — ""."""
    if _is_empty(v):
        return mark_safe("")
    return format_html('<span translate="no">{}</span>', v)


def join(parts, sep=" · ") -> SafeString:
    """Частини через « · »: рядки екрануються (слова інтерфейсу — їх перекладе 🌐), `data(...)` і готовий HTML —
    як є; порожні пропускаються. Для назви, підзаголовка й підсумків розділів."""
    return mark_safe(conditional_escape(sep).join(conditional_escape(p) for p in parts if not _is_empty(p)))


def _value_html(v) -> SafeString:
    """Готовий HTML (SafeString) — як є, решта — як дані запису."""
    return v if hasattr(v, "__html__") else data(v)


def kv(rows) -> SafeString:
    """[(назва, значення[, tone])] → `dl.mvp-kv`; рядки без значення пропускаються."""
    out = []
    for row in rows or ():
        if not row or len(row) < 2:
            continue
        label, value = row[0], row[1]
        tone = row[2] if len(row) > 2 else ""
        if _is_empty(value) or (hasattr(value, "__html__") and not str(value).strip()):
            continue
        cls = f"mvp-kv-r t-{tone}" if tone in BADGE_TONES else "mvp-kv-r"
        out.append(format_html('<div class="{}"><dt>{}</dt><dd>{}</dd></div>', cls, label, _value_html(value)))
    if not out:
        return mark_safe("")
    return format_html('<dl class="mvp-kv">{}</dl>', mark_safe("".join(out)))


def safe_href(href) -> str:
    """Лише адреси Minerva (/…, #…, відносні) і http(s) / mailto / tel; інше (javascript:, data:) — ""."""
    if href is None:
        return ""
    h = str(href).strip()
    if not h or any(ord(c) < 32 or ord(c) == 127 for c in h):   # браузер викидає їх і читає схему далі
        return ""
    m = _SCHEME.match(h)
    if m and m.group(1).lower() not in _SAFE_SCHEMES:
        return ""
    return h


def link(href, text, new_tab=False) -> SafeString:
    h = safe_href(href)
    if not h:
        return format_html("{}", text)
    if new_tab:
        return format_html('<a class="mvp-link" href="{}" target="_blank" rel="noopener">{}</a>', h, text)
    return format_html('<a class="mvp-link" href="{}">{}</a>', h, text)


def admin_link(request, obj, text=None) -> SafeString:
    """Посилання на картку запису — лише якщо людина її відкриє (роль Minerva + адмінка моделі), інакше текст."""
    if obj is None:
        return mark_safe("")
    label = str(obj) if text is None else text
    o = obj._meta
    other = _model_admin(type(obj))
    try:
        if other is not None and request.user.has_perm(f"{o.app_label}.view_{o.model_name}") \
                and other.has_view_or_change_permission(request, obj):
            from django.contrib.admin.utils import quote
            from django.urls import reverse
            url = reverse(f"admin:{o.app_label}_{o.model_name}_change", args=[quote(obj.pk)])
            return format_html('<a class="mvp-link" href="{}" translate="no">{}</a>', url, label)
    except Exception:  # noqa: BLE001 — немає сторінки / права не перевірились — просто текст
        pass
    return data(label)


def copy(value, label="Копіювати") -> SafeString:
    if _is_empty(value):
        return mark_safe("")
    aria = "Копіювати" if label == "Копіювати" else f"Копіювати: {label}"
    return format_html('<button type="button" class="mvp-copy" data-copy="{}" aria-label="{}" title="{}">📋</button>',
                       value, aria, aria)


def empty(text) -> SafeString:
    return format_html('<div class="mvp-empty">{}</div>', text)


def more(href, text) -> SafeString:
    h = safe_href(href)
    return format_html('<a class="mvp-more-link" href="{}">{}</a>', h, text) if h else mark_safe("")


def badge(text, tone="") -> SafeString:
    if _is_empty(text):
        return mark_safe("")
    if tone in BADGE_TONES:
        return format_html('<span class="mvp-badge mvp-t-{}">{}</span>', tone, text)
    return format_html('<span class="mvp-badge">{}</span>', text)


def table(head, rows) -> SafeString:
    """Таблиця: заголовки — підписи інтерфейсу, клітинки — дані запису (SafeString — як є)."""
    body = [format_html("<tr>{}</tr>", format_html_join("", "<td>{}</td>", ((_value_html(c),) for c in r)))
            for r in rows or ()]
    if not body:
        return mark_safe("")
    thead = (format_html("<thead><tr>{}</tr></thead>", format_html_join("", "<th>{}</th>", ((h,) for h in head)))
             if head else "")
    return format_html('<table class="mvp-table">{}<tbody>{}</tbody></table>', thead, mark_safe("".join(body)))


def _decimal(v):
    if v is None or isinstance(v, bool) or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def money(amount, currency="") -> str:
    """1234.5, "EUR" → «1 234,50 EUR»."""
    d = _decimal(amount)
    if d is None:
        return "" if amount is None or amount == "" else str(amount)
    d = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if d < 0 else ""
    whole, frac = f"{abs(d):.2f}".split(".")
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    text = f"{sign}{' '.join(groups)},{frac}"
    return f"{text} {currency}" if currency else text


def qty(v) -> str:
    """10.000 → «10», 2.5 → «2,5»."""
    d = _decimal(v)
    if d is None:
        return "" if v is None else str(v)
    if d == d.to_integral_value():
        return str(int(d))
    return format(d.normalize(), "f").replace(".", ",")


def date(d) -> str:
    if d is None or d == "":
        return ""
    if isinstance(d, _datetime):
        if timezone.is_aware(d):
            d = timezone.localtime(d)
        return d.strftime("%d.%m.%Y")
    if isinstance(d, _date):
        return d.strftime("%d.%m.%Y")
    return str(d)


def dt(v) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, _datetime):
        if timezone.is_aware(v):
            v = timezone.localtime(v)
        return v.strftime("%d.%m.%Y %H:%M")
    return date(v)


def value_text(v) -> str:
    """Значення поля як текст для `data-value` / `expect`: None → "", bool → "1" / "", дати — ISO."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else ""
    if isinstance(v, (_date, _datetime)):
        return v.isoformat()
    return str(v)


def field_value(obj, name):
    """Значення поля запису; для FK — ключ (attname), як його бачить форма."""
    try:
        f = obj._meta.get_field(name)
    except Exception:  # noqa: BLE001
        return getattr(obj, name, None)
    if (getattr(f, "many_to_one", False) or getattr(f, "one_to_one", False)) and getattr(f, "concrete", False):
        return getattr(obj, f.attname, None)
    return getattr(obj, name, None)


def split_icon(label) -> tuple:
    """«🏷 Етикетка» → ("🏷", "Етикетка"); без значка — ("", label)."""
    text = str(label or "")
    m = _ICON.match(text)
    return (m.group(1), m.group(2).strip()) if m else ("", text.strip())


def key_name(code) -> str:
    """KeyboardEvent.code → що написано на клавіші: KeyF → F, Digit1 → 1."""
    code = str(code or "")
    if code.startswith("Key") and len(code) == 4:
        return code[3]
    if code.startswith("Digit") and len(code) == 6:
        return code[5]
    return {"Slash": "/", "Period": ".", "Comma": ",", "BracketLeft": "[", "BracketRight": "]",
            "Minus": "-", "Equal": "="}.get(code, code)


def _with_key(text, hotkey) -> str:
    return f"{text} · {key_name(hotkey)}" if hotkey else str(text)


def _maxlength(edit: Edit, obj):
    if edit.maxlength:
        return int(edit.maxlength)
    try:
        return obj._meta.get_field(edit.field).max_length or None
    except Exception:  # noqa: BLE001
        return None


def edit_choices(edit: Edit, obj, request) -> list:
    """[(value, label)] — з `Edit.choices` або choices поля моделі."""
    raw = []
    try:
        if edit.choices is not None:
            raw = list(edit.choices(obj, request) or [])
        else:
            raw = list(obj._meta.get_field(edit.field).flatchoices or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("side panel choices %s failed: %s", edit.key, exc)
    return [(value_text(v), str(lab)) for v, lab in raw]


def _tone(edit: Edit, obj, value: str) -> str:
    tones = edit.tones
    try:
        tone = tones(obj, value) if callable(tones) else (tones or {}).get(value, "")
    except Exception:  # noqa: BLE001
        tone = ""
    return tone if tone in BADGE_TONES else ""


def _confirms(edit: Edit, obj, choices) -> dict:
    c = edit.confirm
    out = {}
    try:
        if callable(c):
            for value, _label in choices:
                text = c(obj, value)
                if text:
                    out[value] = str(text)
        elif isinstance(c, dict):
            out = {value_text(k): str(v) for k, v in c.items() if v}
    except Exception as exc:  # noqa: BLE001
        logger.warning("side panel confirm %s failed: %s", edit.key, exc)
    return out


def _choice_label(edit, obj, choices, text) -> str:
    label = dict(choices).get(text)
    if label is None:
        getter = getattr(obj, f"get_{edit.field}_display", None)
        try:
            label = getter() if callable(getter) else None
        except Exception:  # noqa: BLE001
            label = None
    return str(label) if label not in (None, "") else (text or "—")


def _attrs(items) -> SafeString:
    """[(назва, значення)] → ' назва="значення"'; True — атрибут без значення, None / False — без атрибута."""
    out = []
    for name, value in items:
        if value is None or value is False:
            continue
        if value is True:
            out.append(format_html(" {}", name))
        else:
            out.append(format_html(' {}="{}"', name, value))
    return mark_safe("".join(out))


def _edit_control(panel: Panel, edit: Edit, obj, request, editable: bool) -> SafeString:
    raw = field_value(obj, edit.field)
    text = value_text(raw)
    change_text = f"Змінити: {edit.label}"
    title = _with_key(change_text, edit.hotkey)
    if edit.kind == "choice":
        choices = edit_choices(edit, obj, request)
        current = _choice_label(edit, obj, choices, text)
        tone = _tone(edit, obj, text)
        if editable:
            confirm = _confirms(edit, obj, choices)
            return format_html(
                '<button type="button" class="mvp-pill{}" data-edit="{}" data-kind="choice" data-value="{}" '
                'data-choices="{}"{} aria-haspopup="menu" title="{}">{} ▾</button>',
                f" mvp-t-{tone}" if tone else "", edit.key, text, json.dumps(choices, ensure_ascii=False),
                _attrs([("data-confirm", json.dumps(confirm, ensure_ascii=False) if confirm else None)]),
                title, current)
        if edit.place == "head":
            return format_html('<span class="mvp-badge{}">{}</span>', f" mvp-t-{tone}" if tone else "", current)
        return format_html('<span class="mvp-ro">{}</span>', current)
    if edit.kind == "bool":
        on = bool(raw)
        icon = edit.icon_on if on else edit.icon_off
        if editable:
            return format_html(
                '<button type="button" class="mvp-tog" data-edit="{}" data-kind="bool" data-value="{}" '
                'aria-pressed="{}" data-on="{}" data-off="{}" title="{}" aria-label="{}">{}</button>',
                edit.key, text, "true" if on else "false", edit.icon_on, edit.icon_off,
                _with_key(edit.label, edit.hotkey), edit.label, icon)
        return format_html('<span class="mvp-ro" title="{}">{}</span>', edit.label, icon)
    ml = _maxlength(edit, obj)
    ml_attr = _attrs([("data-maxlength", ml)])
    if edit.kind == "textarea":
        memo_place = edit.place == "memo"
        shown = (f"📝 {raw}" if memo_place else str(raw)) if text else ""
        cls = "mvp-ed mvp-ed-memo" if memo_place else "mvp-ed mvp-ed-text"
        if not editable:
            return data(shown)
        if not text:
            add = "📝 Додати нотатку" if memo_place else "✏️ Додати"
            return format_html('<div class="{}" data-edit="{}" data-kind="textarea" data-value=""{}>'
                               '<button type="button" class="mvp-ed-btn mvp-ed-add" title="{}">{}</button></div>',
                               cls, edit.key, ml_attr, title, add)
        return format_html('<div class="{}" data-edit="{}" data-kind="textarea" data-value="{}"{}>'
                           '<span class="mvp-ed-v" translate="no">{}</span>'
                           '<button type="button" class="mvp-ed-btn" aria-label="{}" title="{}">✏️</button></div>',
                           cls, edit.key, text, ml_attr, shown, change_text, title)
    if edit.kind == "date":
        shown = date(raw)
    elif edit.kind == "number":
        shown = qty(raw)
    else:
        shown = "" if raw is None else str(raw)
    if not editable:
        return data(shown)
    return format_html('<span class="mvp-ed" data-edit="{}" data-kind="{}" data-value="{}"{}>'
                       '<span class="mvp-ed-v{}" translate="no">{}</span>'
                       '<button type="button" class="mvp-ed-btn" aria-label="{}" title="{}">✏️</button></span>',
                       edit.key, edit.kind, text, ml_attr, "" if shown else " mvp-muted", shown or "—",
                       change_text, title)


def edit_html(request, obj, key) -> SafeString:
    """Швидка зміна поля в панелі: кнопка / поле, якщо людина може змінити запис, інакше лише значення."""
    panel = panel_for(request)
    if panel is None or panel.edit(key) is None:
        panel = get(model_key(obj._meta)) or panel
    edit = panel.edit(key) if panel is not None else None
    if edit is None:
        return mark_safe("")
    ma = admin_for(request, obj)
    try:
        editable = edit_allowed(panel, edit, request, ma, obj)
    except Exception:  # noqa: BLE001
        editable = False
    return _edit_control(panel, edit, obj, request, editable)


# ── вибір людини (UserProfile.ui_prefs["panels"]) ─────────────────────────────

def _profile(user):
    try:
        return user.profile
    except Exception:  # noqa: BLE001
        return None


def _bar_edits(panel: Panel) -> list:
    return [e for e in panel.edits if e.place == "bar"]


def _action_keys(panel: Panel) -> list:
    return [a.key for a in panel.actions] + [e.key for e in _bar_edits(panel)]


def _default_actions(panel: Panel) -> list:
    return [a.key for a in panel.actions if a.default] + [e.key for e in _bar_edits(panel) if e.default]


def _all_keys(panel: Panel) -> dict:
    return {"blocks": [b.key for b in panel.blocks], "actions": _action_keys(panel)}


def defaults(panel: Panel) -> dict:
    return {
        "on": not panel.generic,
        "blocks": [b.key for b in panel.blocks if b.default],
        "collapsed": [b.key for b in panel.blocks if b.default and b.collapsed],
        "actions": _default_actions(panel),
        "bar": BAR_DEFAULT, "width": DEFAULT_WIDTH, "hide_cols": None, "start": "last",
        "seen": _all_keys(panel),
    }


def _key_list(value, known=None, pattern=None):
    if not isinstance(value, list):
        return _BAD
    out = []
    for k in value:
        if not isinstance(k, str) or k in out:
            continue
        if (known is not None and k in known) or (pattern is not None and pattern.match(k)):
            out.append(k)
        if len(out) >= LIST_MAX:
            break
    return out


def _clean_value(panel: Panel, key: str, value):
    """Одне значення вибору людини; _BAD — неправильне (тоді діє значення за замовчуванням)."""
    if value is None:
        return _BAD
    if key == "on":
        return value if isinstance(value, bool) else _BAD
    if key in ("blocks", "collapsed"):
        return _key_list(value, known={b.key for b in panel.blocks})
    if key == "actions":
        return _key_list(value, known=set(_action_keys(panel)), pattern=_ADMIN)
    if key == "hide_cols":
        return _key_list(value, pattern=_COL)
    if key == "bar":
        if isinstance(value, bool) or not isinstance(value, int):
            return _BAD
        return max(0, min(BAR_MAX, value))
    if key == "width":
        if isinstance(value, bool):
            return _BAD
        try:
            w = int(round(float(value))) if isinstance(value, (int, float, str)) else None
        except (TypeError, ValueError, OverflowError):
            return _BAD
        return w if w is not None and MIN_WIDTH <= w <= MAX_WIDTH else _BAD
    if key == "start":
        if not isinstance(value, str):
            return _BAD
        return value if value in ("last", "first") or value in {b.key for b in panel.blocks} else _BAD
    return _BAD


def clean(panel: Panel, data) -> dict:
    """Збережений вибір для списку — лише відомі ключі й правильні значення; решта (null, не той тип) —
    відкидається, тобто діє значення за замовчуванням. Ширина поза 320..960 — авто."""
    out = {}
    if not isinstance(data, dict):
        return out
    for key in LIST_KEYS:
        if key in data:
            v = _clean_value(panel, key, data[key])
            if v is not _BAD:
                out[key] = v
    seen = data.get("seen")
    if isinstance(seen, dict):
        known = _all_keys(panel)
        out["seen"] = {}
        for name in ("blocks", "actions"):
            raw = seen.get(name)
            keys = []
            for k in raw if isinstance(raw, list) else ():
                if isinstance(k, str) and k in known[name] and k not in keys:
                    keys.append(k)
            out["seen"][name] = keys
    return out


def clean_shared(data) -> dict:
    """Вигляд для всіх списків — лише відомі значення."""
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in SHARED_CHOICES and v in SHARED_CHOICES[k]}


def _seen_of(panel: Panel, stored: dict) -> dict:
    if "seen" in stored:
        return stored["seen"]
    v1 = panel.seen_v1 or {}                       # збережено ще у v1: що тоді вже було
    return {"blocks": list(v1.get("blocks") or ()), "actions": list(v1.get("actions") or ())}


def _effective_list(panel: Panel, stored: dict) -> dict:
    """За замовчуванням ← збережене ← нове за замовчуванням, якого людина ще не бачила (`seen`)."""
    out = {k: v for k, v in defaults(panel).items() if k != "seen"}
    for k, v in stored.items():
        if k != "seen":
            out[k] = list(v) if isinstance(v, list) else v
    seen = _seen_of(panel, stored)
    seen_blocks, seen_actions = set(seen.get("blocks") or ()), set(seen.get("actions") or ())
    fresh = [b for b in panel.blocks if b.default and b.key not in seen_blocks]
    if "blocks" in stored:
        out["blocks"] += [b.key for b in fresh if b.key not in out["blocks"]]
    if "collapsed" in stored:
        out["collapsed"] += [b.key for b in fresh if b.collapsed and b.key not in out["collapsed"]]
    if "actions" in stored:
        out["actions"] += [k for k in _default_actions(panel) if k not in seen_actions and k not in out["actions"]]
    return out


def _saved(user, panel: Panel) -> tuple:
    p = _profile(user)
    ui = getattr(p, "ui_prefs", None) if p is not None else None
    panels = ui.get("panels") if isinstance(ui, dict) else None
    if not isinstance(panels, dict):
        return {}, {}
    return clean(panel, panels.get(panel.model)), clean_shared(panels.get("*"))


def prefs(user, panel: Panel) -> dict:
    """Вибір людини, як він діє: зміст цього списку + вигляд для всіх списків (без `seen`)."""
    stored, shared = _saved(user, panel)
    out = _effective_list(panel, stored)
    out.update(SHARED_DEFAULTS)
    out.update(shared)
    return out


def _payload(data: dict) -> tuple:
    """(reset, set, shared) з запиту; без set / shared / reset — старий (v1) запис усього одразу."""
    if any(k in data for k in ("set", "shared", "reset")):
        reset = data.get("reset") if data.get("reset") in RESETS else None
        sets = data.get("set") if isinstance(data.get("set"), dict) else {}
        shared = data.get("shared") if isinstance(data.get("shared"), dict) else {}
        return reset, sets, shared
    reset = "list" if data.get("_reset") is True else None
    return reset, {k: v for k, v in data.items() if k != "_reset"}, {}


def save_prefs(user, panel: Panel, data) -> dict:
    """Записати зміну (дельту) й повернути вибір, як він тепер діє.

    {"set": {ключі списку}}, {"shared": {ключі вигляду}}, {"reset": blocks | actions | layout | list | all};
    разом — у порядку reset → set → shared; null — як було. Старий запис (v1) — як set, "_reset": true — reset list.
    """
    p = _profile(user)
    if p is None or not isinstance(data, dict):
        return prefs(user, panel)
    reset, sets, shared = _payload(data)
    from django.db import transaction
    from core.models import UserProfile
    with transaction.atomic():
        row = UserProfile.objects.select_for_update().get(pk=p.pk)
        ui = dict(row.ui_prefs) if isinstance(row.ui_prefs, dict) else {}
        panels = dict(ui["panels"]) if isinstance(ui.get("panels"), dict) else {}
        stored = clean(panel, panels.get(panel.model))
        star = clean_shared(panels.get("*"))
        # нове за замовчуванням, яке людина вже бачила в панелі, стає її вибором — «seen» нижче його не сховає
        effective = _effective_list(panel, stored)
        for key in ("blocks", "collapsed", "actions"):
            if key in stored:
                stored[key] = effective[key]
        if reset == "blocks":
            for key in ("blocks", "collapsed", "start"):
                stored.pop(key, None)
        elif reset == "actions":
            for key in ("actions", "bar"):
                stored.pop(key, None)
        elif reset == "layout":
            for key in ("width", "hide_cols"):
                stored.pop(key, None)
            star = {}
        elif reset == "list":
            stored = {}
        elif reset == "all":
            stored, star = {}, {}
        shared = dict(shared)
        for key, value in sets.items():
            if key in SHARED_CHOICES:                # вигляд, надісланий разом зі змістом, — туди, де йому місце
                shared.setdefault(key, value)
                continue
            if key not in LIST_KEYS:
                continue
            v = _clean_value(panel, key, value)
            if v is _BAD:
                stored.pop(key, None)
            else:
                stored[key] = v
        for key, value in shared.items():
            if key not in SHARED_CHOICES:
                continue
            if value in SHARED_CHOICES[key]:
                star[key] = value
            else:
                star.pop(key, None)
        stored["seen"] = _all_keys(panel)
        panels[panel.model] = stored
        if star:
            panels["*"] = star
        else:
            panels.pop("*", None)
        ui["panels"] = panels
        row.ui_prefs = ui
        row.save(update_fields=["ui_prefs"])
    try:
        p.ui_prefs = row.ui_prefs
    except Exception:  # noqa: BLE001
        pass
    return prefs(user, panel)


# ── налаштування для сторінки списку ───────────────────────────────────────────

def _icon_short(label, icon="", short=""):
    i, rest = split_icon(label)
    return (icon or i), (short or rest or str(label))


def _columns(ma, request) -> list:
    from django.contrib.admin.utils import label_for_field
    out = []
    try:
        names = list(ma.get_list_display(request))
    except Exception:  # noqa: BLE001
        return out
    for i, name in enumerate(names):
        if name == "action_checkbox":
            continue
        if callable(name):
            cname = getattr(name, "__name__", "")
            cname = f"lambda{i}" if cname == "<lambda>" else cname
        else:
            cname = str(name)
        if not _COL.match(cname):
            continue
        try:
            label = label_for_field(name, ma.model, model_admin=ma)
        except Exception:  # noqa: BLE001
            label = cname
        out.append({"name": cname, "label": " ".join(strip_tags(str(label)).split()) or cname})
    return out


def _page(cl):
    paginator = getattr(cl, "paginator", None)
    if cl is None or paginator is None:
        return None
    try:
        from django.contrib.admin.views.main import PAGE_VAR
        multi = getattr(cl, "multi_page", True) and not (getattr(cl, "show_all", False)
                                                         and getattr(cl, "can_show_all", False))
        return {"num": int(cl.page_num), "pages": int(paginator.num_pages) if multi else 1, "param": PAGE_VAR}
    except Exception:  # noqa: BLE001
        return None


def config(panel: Panel, request, *, url: str, prefs_url: str, cl=None) -> dict:
    """Налаштування для static/admin/js/mv_panel.js (json_script «mv-panel-config» на сторінці списку)."""
    from core import panel_edit
    ma = getattr(cl, "model_admin", None) if cl is not None else None
    if ma is None:
        from django.apps import apps
        try:
            ma = _model_admin(apps.get_model(panel.model))
        except (LookupError, ValueError):
            ma = None
    opts = ma.model._meta if ma is not None else None
    change = can_change(request, ma) if ma is not None else False
    try:
        delete = bool(ma is not None and ma.has_delete_permission(request)
                      and request.user.has_perm(f"{opts.app_label}.delete_{opts.model_name}"))
    except Exception:  # noqa: BLE001
        delete = False
    blocks = [{"key": b.key, "title": str(b.title), "short": str(b.short or b.title), "icon": str(b.icon),
               "default": bool(b.default), "lazy": bool(b.lazy), "collapsed": bool(b.collapsed)}
              for b in visible_blocks(panel, request)]
    actions = []
    for a in visible_actions(panel, request):
        icon, short = _icon_short(a.label, a.icon, a.short)
        actions.append({"key": a.key, "label": str(a.label), "short": short, "icon": icon,
                        "kind": "svl" if a.kind == "svl" else "link", "default": bool(a.default),
                        "hotkey": a.hotkey})
    edits = visible_edits(panel, request, ma) if ma is not None else []
    for e in edits:
        if e.place == "bar":
            icon, short = _icon_short(e.label)
            actions.append({"key": e.key, "label": str(e.label), "short": short, "icon": icon or e.icon_on,
                            "kind": "edit", "default": bool(e.default), "hotkey": e.hotkey})
    return {
        "v": 2, "model": panel.model,
        "name": str(opts.verbose_name_plural) if opts is not None else panel.model,
        "url": url, "prefsUrl": prefs_url, "generic": bool(panel.generic),
        "canChange": change, "canDelete": delete,
        "prefs": prefs(request.user, panel), "shared": list(SHARED_DEFAULTS),
        "blocks": blocks, "actions": actions,
        "edits": [{"key": e.key, "label": str(e.label), "kind": e.kind, "place": e.place, "hotkey": e.hotkey}
                  for e in edits] if change else [],
        "adminActions": panel_edit.admin_actions(panel, ma, request) if ma is not None else [],
        "columns": _columns(ma, request) if ma is not None else [],
        "hideDefault": list(panel.hide_cols),
        "width": {"min": MIN_WIDTH, "max": MAX_WIDTH, "presets": list(PRESETS)},
        "page": _page(cl),
    }


# ── HTML панелі ────────────────────────────────────────────────────────────────

def _call(fn, *args, default=None, what=""):
    if fn is None:
        return default
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 — одна функція постачальника не ламає панель
        logger.warning("side panel %s failed: %s", what or getattr(fn, "__name__", fn), exc)
        return default


def _html(v) -> SafeString:
    if v is None:
        return mark_safe("")
    return conditional_escape(v)


def label_of(panel: Panel, obj) -> str:
    text = _call(panel.label, obj, what="label") if panel.label else None
    if not text:
        text = strip_tags(str(obj))
    text = " ".join(str(text).split())
    return text[:120]


def _urls(panel: Panel, obj, request, ma) -> dict:
    from django.contrib.admin.utils import quote
    from django.urls import NoReverseMatch, reverse
    o = obj._meta
    base = f"admin:{o.app_label}_{o.model_name}"
    out = {"change": "", "history": None, "delete": None}
    pk = quote(obj.pk)
    try:
        out["change"] = reverse(f"{base}_change", args=[pk])
    except NoReverseMatch:
        pass
    try:
        if panel.history_link and ma is not None and ma.has_view_or_change_permission(request, obj):
            out["history"] = reverse(f"{base}_history", args=[pk])
    except Exception:  # noqa: BLE001 — немає сторінки історії / права не перевірились
        out["history"] = None
    try:
        if ma is not None and ma.has_delete_permission(request, obj) \
                and request.user.has_perm(f"{o.app_label}.delete_{o.model_name}"):
            out["delete"] = reverse(f"{base}_delete", args=[pk])
    except Exception:  # noqa: BLE001
        out["delete"] = None
    return out


def _data_attrs(attrs) -> list:
    out = []
    for k, v in (attrs or {}).items():
        if not isinstance(k, str) or not _DATA_KEY.match(k) or v is None:
            continue
        out.append((f"data-{k}", "1" if v is True else "" if v is False else str(v)))
    return out


def _action_html(a: Action, obj, request, hidden: bool) -> SafeString:
    href = safe_href(_call(a.href, obj, request, default="", what=f"action {a.key}") or "") if a.href else ""
    title = _with_key(a.label, a.hotkey)
    if a.kind == "svl":
        attrs = _data_attrs(_call(a.attrs, obj, request, default={}, what=f"action {a.key}") or {})
        if attrs:
            return format_html('<button type="button" class="mvp-act" data-act="{}"{}{} title="{}"{}>{}</button>',
                               a.key, _attrs(attrs), _attrs([("data-fallback", href or None)]), title,
                               _attrs([("hidden", hidden)]), a.label)
    if href:
        return format_html('<a class="mvp-act" data-act="{}" href="{}"{} title="{}"{}>{}</a>', a.key, href,
                           mark_safe(' target="_blank" rel="noopener"') if a.new_tab else "", title,
                           _attrs([("hidden", hidden)]), a.label)
    reason = _call(a.reason, obj, request, default="", what=f"action {a.key}") if a.reason else ""
    if reason:
        return format_html('<span class="mvp-act is-off" data-act="{}" aria-disabled="true" title="{}"{}>{}</span>',
                           a.key, reason, _attrs([("hidden", hidden)]), a.label)
    return mark_safe("")


def _bar_edit_html(panel: Panel, e: Edit, obj, request, hidden: bool) -> SafeString:
    raw = field_value(obj, e.field)
    text = value_text(raw)
    if e.kind == "bool":
        on = bool(raw)
        return format_html(
            '<button type="button" class="mvp-act mvp-tog" data-act="{}" data-edit="{}" data-kind="bool" '
            'data-value="{}" aria-pressed="{}" data-on="{}" data-off="{}" title="{}" aria-label="{}"{}>{}</button>',
            e.key, e.key, text, "true" if on else "false", e.icon_on, e.icon_off, _with_key(e.label, e.hotkey),
            e.label, _attrs([("hidden", hidden)]), e.icon_on if on else e.icon_off)
    if e.kind == "choice":
        choices = edit_choices(e, obj, request)
        confirm = _confirms(e, obj, choices)
        return format_html(
            '<button type="button" class="mvp-act" data-act="{}" data-edit="{}" data-kind="choice" data-value="{}" '
            'data-choices="{}"{} aria-haspopup="menu" title="{}"{}>{}</button>',
            e.key, e.key, text, json.dumps(choices, ensure_ascii=False),
            _attrs([("data-confirm", json.dumps(confirm, ensure_ascii=False) if confirm else None)]),
            _with_key(e.label, e.hotkey), _attrs([("hidden", hidden)]), e.label)
    return mark_safe("")


def _admin_actions(panel, ma, request) -> list:
    if ma is None:
        return []
    cache = _request_cache(request, "_mvp_admin_actions")
    key = ma.opts.label_lower
    if key not in cache:
        from core import panel_edit
        cache[key] = panel_edit.admin_actions(panel, ma, request)
    return cache[key]


def _actbar(panel: Panel, obj, request, ma, p: dict, label: str) -> SafeString:
    pinned = list(p.get("actions") or [])
    items = []                                   # (ключ, функція(hidden) -> HTML)
    for a in visible_actions(panel, request, obj):
        items.append((a.key, lambda hidden, a=a: _action_html(a, obj, request, hidden)))
    for e in visible_edits(panel, request, ma, obj):
        if e.place == "bar" and field_editable(panel, ma, request, obj, e.field):
            items.append((e.key, lambda hidden, e=e: _bar_edit_html(panel, e, obj, request, hidden)))
    for act in _admin_actions(panel, ma, request):
        name, text = act["name"], act["label"]
        items.append((f"admin:{name}", lambda hidden, name=name, text=text: format_html(
            '<button type="button" class="mvp-act" data-act="admin:{}" data-admin-action="{}" data-confirm="{}" '
            'title="{}"{}>{}</button>', name, name, f"Виконати «{text}» для {label}?", text,
            _attrs([("hidden", hidden)]), text)))
    order = {k: i for i, k in enumerate(pinned)}
    listed = sorted((x for x in items if x[0] in order), key=lambda x: order[x[0]])
    rest = [x for x in items if x[0] not in order]
    html = [build(False) for _k, build in listed] + [build(True) for _k, build in rest]
    return format_html(
        '<div class="mvp-actbar" role="toolbar" aria-label="Швидкі дії">{}'
        '<button type="button" class="mvp-act mvp-more" data-mvp-more aria-haspopup="menu" aria-label="Ще дії" '
        'title="Ще дії">⋯</button></div>', mark_safe("".join(str(h) for h in html if h)))


def _memo(panel: Panel, obj, request, ma) -> SafeString:
    e = panel.edit(panel.memo) if panel.memo else None
    if e is None:
        return mark_safe("")
    raw = field_value(obj, e.field)
    try:
        editable = edit_allowed(panel, e, request, ma, obj)
    except Exception:  # noqa: BLE001
        editable = False
    if not (value_text(raw) or editable):
        return mark_safe("")
    return format_html('<div class="mvp-memo">{}</div>', _edit_control(panel, e, obj, request, editable))


def render_head(panel: Panel, obj, request, p: dict | None = None) -> str:
    """`<header class="mvp-head">`: назва, швидкі зміни в шапці, рядок дій, нотатка."""
    p = _prefs_for(panel, request, p)
    ma = admin_for(request, obj)
    _ensure_bound(request, panel, ma, p)
    title = _call(panel.title, obj, what="title")
    if title is None or title == "":
        title = data(str(obj))
    subtitle = _call(panel.subtitle, obj, request, default="", what="subtitle") if panel.subtitle else ""
    hedit = mark_safe("".join(str(edit_html(request, obj, e.key)) for e in panel.edits if e.place == "head"))
    urls = _urls(panel, obj, request, ma)
    return format_html(
        '<header class="mvp-head"><div class="mvp-hrow">'
        '<button type="button" class="mvp-icon" data-mvp-nav="-1" aria-label="Попередній запис (↑)" '
        'title="Попередній запис (↑)">‹</button>'
        '<button type="button" class="mvp-icon" data-mvp-nav="1" aria-label="Наступний запис (↓)" '
        'title="Наступний запис (↓)">›</button>'
        '<h2 class="mvp-title" id="mvp-title" tabindex="-1"><span>{}</span></h2>'
        '<a class="mvp-icon" data-mvp-open href="{}" aria-label="Відкрити картку (Enter)" '
        'title="Відкрити картку (Enter) · Ctrl — нова вкладка">⤢</a>'
        '<button type="button" class="mvp-icon" data-mvp-menu aria-haspopup="menu" aria-label="Налаштувати панель" '
        'title="Налаштувати панель під себе (або правий клік)">⚙</button>'
        '<button type="button" class="mvp-icon" data-mvp-close aria-label="Закрити (Esc)" title="Закрити (Esc)">✕</button>'
        # статус — у другому рядку: у першому номер / назва не обрізаються (панель буває 360 px)
        '</div><div class="mvp-sub"><div class="mvp-hedit">{}</div><span class="mvp-subt">{}</span>'
        '<button type="button" class="mvp-pos" data-mvp-pos hidden></button></div>{}{}</header>',
        _html(title), urls["change"] or "#", hedit, _html(subtitle or ""),
        _actbar(panel, obj, request, ma, p, label_of(panel, obj)), _memo(panel, obj, request, ma))


def _badge_of(block: Block, obj, request):
    b = _call(block.badge, obj, request, what=f"badge {block.key}") if block.badge else None
    if b is None:
        return None
    if isinstance(b, bool):
        return None
    if isinstance(b, int):
        return Badge(count=b)
    return b if isinstance(b, Badge) else None


BRIEF_MAX = 80


def _summary_of(block: Block, obj, request) -> tuple:
    """(текст, HTML) підсумку розділу. Дані запису провайдер позначає data(...) — їх 🌐 не перекладає; задовгий
    підсумок обрізається текстом і тоді не перекладається весь (межі даних у ньому вже не видно)."""
    s = _call(block.summary, obj, request, default="", what=f"summary {block.key}") if block.summary else ""
    text = " ".join(strip_tags(str(s or "")).split())
    if len(text) > BRIEF_MAX:
        text = text[:BRIEF_MAX - 1] + "…"
        return text, data(text)
    return text, conditional_escape(s or "")


def _section(panel: Panel, block: Block, obj, request, p: dict, *, now: bool, change_url: str = "") -> SafeString:
    key = block.key
    collapsed = key in (p.get("collapsed") or ())
    na = False
    if block.applies is not None:
        try:
            na = not block.applies(obj)
        except Exception as exc:  # noqa: BLE001
            logger.warning("side panel %s.%s applies failed: %s", panel.model, key, exc)
    count = tone = btext = brief = ""
    brief_html = mark_safe("")
    if not na:
        bdg = _badge_of(block, obj, request)
        if bdg is not None:
            count = "" if bdg.count is None else str(bdg.count)
            tone = bdg.tone if bdg.tone in TONES else ""
            btext = str(bdg.text or "")
        brief, brief_html = _summary_of(block, obj, request)
    state = ""
    if na:
        state, body = "na", empty(NA_TEXT)
    elif not now:
        state, body = "deferred", mark_safe('<div class="mvp-skel" aria-hidden="true"></div>')
    else:
        try:
            inner = block.render(obj, request)
        except Exception as exc:  # noqa: BLE001 — один розділ не ламає панель
            logger.warning("side panel block %s.%s failed: %s", panel.model, key, exc)
            state = "error"
            body = format_html('<div class="mvp-empty mvp-err">{} <button type="button" data-mvp-retry="{}">'
                               '↻ Спробувати ще</button></div>', ERROR_TEXT, key)
        else:
            if inner is None or not str(inner).strip():
                state, body = "empty", empty(block.empty or EMPTY_TEXT)
            else:
                body = _html(inner)
    card = f"{change_url}#fieldset-0-{int(block.fieldset)}-heading" if change_url and block.fieldset is not None \
        else ""
    attrs = _attrs([
        ("id", f"mvp-sec-{key}"), ("data-sec", key), ("data-title", block.title),
        ("data-short", block.short or block.title), ("data-icon", block.icon), ("data-count", count),
        ("data-tone", tone), ("data-badge", btext), ("data-brief", brief), ("data-lazy", bool(block.lazy)),
        ("data-deferred", state == "deferred"), ("data-collapsed", collapsed), ("data-empty", state == "empty"),
        ("data-na", state == "na"), ("data-error", state == "error"), ("data-card", card or None)])
    card_link = format_html('<a class="mvp-icon mvp-sh-card" href="{}" aria-label="Відкрити в картці" '
                            'title="Відкрити в картці">↗</a>', card) if card else ""
    return format_html(
        '<section class="mvp-sec"{}><h3 class="mvp-sh" id="mvp-sh-{}"><button type="button" class="mvp-sh-btn" '
        'aria-expanded="{}" aria-controls="mvp-sb-{}"><span class="mvp-si" aria-hidden="true">{}</span>'
        '<span class="mvp-st">{}</span><span class="mvp-sc">{}</span><span class="mvp-sbrief">{}</span>'
        '<span class="mvp-chev" aria-hidden="true"></span></button>{}</h3>'
        '<div class="mvp-sb" id="mvp-sb-{}" role="region" aria-labelledby="mvp-sh-{}">{}</div></section>',
        attrs, key, "false" if collapsed else "true", key, block.icon, block.title, count, brief_html, card_link,
        key, key, body)


def render(panel: Panel, obj, request, p: dict | None = None, open_key: str | None = None) -> str:
    """Оболонка панелі: шапка, підменю (будує сторінка), розділи у вибраному людиною порядку, низ."""
    p = _prefs_for(panel, request, p)
    ma = admin_for(request, obj)
    _ensure_bound(request, panel, ma, p)
    visible = {b.key: b for b in visible_blocks(panel, request)}
    urls = _urls(panel, obj, request, ma)
    sections = []
    for key in p.get("blocks") or ():
        block = visible.get(key)
        if block is None:
            continue
        collapsed = key in (p.get("collapsed") or ())
        now = not collapsed and (not block.lazy or key == open_key)
        sections.append(str(_section(panel, block, obj, request, p, now=now, change_url=urls["change"])))
    body = mark_safe("".join(sections)) if sections else empty(NO_SECTIONS_TEXT)
    shell = _attrs([
        ("data-model", panel.model), ("data-pk", obj.pk), ("data-label", label_of(panel, obj)),
        ("data-change-url", urls["change"]), ("data-history-url", urls["history"]),
        ("data-delete-url", urls["delete"]), ("data-can-change", "1" if can_change(request, ma, obj) else "0")])
    stamp = timezone.localtime(timezone.now()).strftime("%H:%M")
    return format_html(
        '<div class="mvp-shell"{}>{}<nav class="mvp-nav" aria-label="Розділи запису"></nav>'
        '<div class="mvp-body" tabindex="0">{}</div>'
        '<footer class="mvp-foot"><button type="button" class="mvp-icon" data-mvp-refresh aria-label="Оновити (R)" '
        'title="Оновити (R)">↻</button><button type="button" class="mvp-icon" data-mvp-keys '
        'aria-label="Клавіші панелі (?)" title="Клавіші панелі (?)">⌨</button>'
        '<span class="mvp-stamp">оновлено {}</span></footer></div>',
        shell, render_head(panel, obj, request, p), body, stamp)


def render_block(panel: Panel, key: str, obj, request) -> str:
    """Один розділ з вмістом (окремий запит панелі, «＋ Додати розділ», після швидкої зміни)."""
    block = panel.block(key)
    if block is None or not allowed(block.perm, request):
        return ""
    p = _prefs_for(panel, request)
    ma = admin_for(request, obj)
    _ensure_bound(request, panel, ma, p)
    return str(_section(panel, block, obj, request, p, now=True, change_url=_urls(panel, obj, request, ma)["change"]))
