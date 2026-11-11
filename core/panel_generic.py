"""📌 Панель праворуч — загальні розділи для будь-якого списку (core/side_panel.py) і загальна панель.

Розділи (`make(key)`; кожна своя панель отримує їх сама — `side_panel.register(generic_blocks=…)`):
  • «🧾 Усі поля» (`field_groups` → `fields_html`) — групи полів картки так, як картка показує їх цій людині
    (`ma.get_fieldsets(request, obj)`), але лише поля моделі. Методи адмінки й моделі (кнопки, скрипти, повні
    ключі в `data-value`) ніколи не виконуються — лише їхні назви в «Ще в картці». Ключі й паролі (назва поля або
    поле-пароль у формі картки: клас форми, без створення її екземпляра) — «🔒 збережено» / «не задано», без
    жодної частини значення, і в JSON теж. Поля поза групами картки, які постачальник назвав
    (`Panel.extra_fields`), — група «Ще поля»;
  • «🔗 Пов’язане» (`relations_of`, `related_html`) — записи інших моделей, що посилаються на цей, лише ті, які
    людина бачить: роль Minerva (`has_perm view`), права адмінки тієї моделі й її queryset (`get_queryset`).
    Пошта, календар, база знань, робочий час, службове й секрети (`RELATED_SKIP_*`, `side_panel.NEVER_*`) — ніколи;
  • «🕘 Історія» (`history_html`) — Django LogEntry: хто, коли, що змінено в картці чи панелі. Журнал аудиту й
    IP-адреси — ні.
Загальна панель (`generic_panel`) — лише для списків з `side_panel.GENERIC`, за замовчуванням вимкнена
(«📌 Панель» над списком вмикає): назва запису, «Усі поля» одразу, «Пов’язане» й «Історія» — на вимогу.
Усе лише читає базу; значення записів — екрановані й у translate="no".
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlsplit

from django.utils.html import format_html, format_html_join, strip_tags
from django.utils.safestring import SafeString, mark_safe
from django.utils.text import capfirst

from core import side_panel as sp

logger = logging.getLogger(__name__)

FIELDS, RELATED, HISTORY = "fields", "related", "history"
KEYS = (FIELDS, RELATED, HISTORY)

RELATED_SKIP_APPS = {"email_assistant", "rag_assistant", "calendar_app", "worktime", "core", "api", "admin", "auth",
                     "contenttypes", "sessions", "ai_assistant", "meetings"}
RELATED_SKIP_MODELS = {"crm.customertimeline"} | set(sp.NEVER_MODELS)
RELATED_MAX = 12                 # зв'язків із записами — не більше, найбільші першими
RELATED_FIRST = 3                # перших записів кожного зв'язку
COUNT_LIMIT = 1000               # більше — «1000+» (рахується не далі)
HISTORY_MAX = 10
FILTER_FROM = 20                 # рядків більше — поле «🔎 Знайти поле»
M2M_MAX = 10
JSON_MAX = 2000
# посилання на записи пошти, календаря, бази знань, ключів API — без назви (тема листа, подія — не для всіх)
PRIVATE_TARGET_APPS = {"email_assistant", "calendar_app", "rag_assistant", "ai_assistant", "api", "meetings"}

# назви ключів: tabele.admin.SECRET_NAME_RE (у кінці назви) + будь-де в назві
SECRET_ANY_RE = re.compile(r"(password|passwd|secret|token|api_?key|apikey|private|credential|_hash)", re.I)
JSON_SECRET_RE = re.compile(r"(authorization|cookie)", re.I)
_FALLBACK_SECRET_RE = re.compile(r"(password|secret|api_key|apikey|token|license_key|access_key|private_key|"
                                 r"assistant_key|secret_key)$", re.I)

SAVED_TEXT = "🔒 збережено"
UNSET_TEXT = "не задано"
YES_TEXT, NO_TEXT = "✓ так", "✗ ні"
DASH = "—"
EXTRA_TITLE = "Ще поля"
CARD_ONLY_TEXT = "Ще в картці:"
SYSTEM_USER = "система"
ACTIONS = {1: ("➕", "Додано"), 2: ("✏️", "Змінено"), 3: ("🗑", "Видалено")}   # LogEntry.action_flag

EMPTY_FIELDS = "Полів для показу немає."
EMPTY_RELATED = "Пов’язаних записів немає."
EMPTY_HISTORY = "Змін через картку ще не було."


# ── спільне ────────────────────────────────────────────────────────────────────

def _secret_name_re():
    try:
        from tabele.admin import SECRET_NAME_RE
        return SECRET_NAME_RE
    except Exception:  # noqa: BLE001 — без адмінки проєкту: той самий список тут
        return _FALLBACK_SECRET_RE


def is_secret_name(name) -> bool:
    """Назва поля / ключа JSON схожа на ключ чи пароль (stripe_secret_key, api_token, password_hash…)."""
    if not isinstance(name, str):
        return False
    return bool(_secret_name_re().search(name) or SECRET_ANY_RE.search(name))


def _plural(n, one, few, many) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _clean_text(value) -> str:
    return " ".join(strip_tags(str(value or "")).split())


def _admin(model):
    from django.contrib import admin
    return admin.site._registry.get(model)


def _change_url(obj) -> str:
    from django.contrib.admin.utils import quote
    from django.urls import NoReverseMatch, reverse
    o = obj._meta
    try:
        return reverse(f"admin:{o.app_label}_{o.model_name}_change", args=[quote(obj.pk)])
    except NoReverseMatch:
        return ""


def _panel_of(request, obj):
    """Панель, яку обслуговує цей запит (core/views.py), або зареєстрована для моделі запису."""
    key = sp.model_key(obj._meta)
    panel = sp.panel_for(request)
    if panel is not None and panel.model == key:
        return panel
    try:
        return sp.get(key)
    except Exception:  # noqa: BLE001
        return None


def _may_view(ma, request, obj) -> bool:
    try:
        return bool(ma.has_view_or_change_permission(request, obj))
    except Exception:  # noqa: BLE001 — не вдалося перевірити = закрито
        return False


# ── «🧾 Усі поля» ───────────────────────────────────────────────────────────────

def _flat(fields) -> list:
    out = []
    for name in fields or ():
        if isinstance(name, (list, tuple)):
            out.extend(name)
        else:
            out.append(name)
    return out


def _fieldsets(ma, request, obj) -> list:
    """Групи картки; не вдалося — одна група з полів моделі, які картка редагує (без виключених)."""
    from django.db import models
    if ma is not None:
        try:
            return [(title, dict(options or {})) for title, options in ma.get_fieldsets(request, obj)]
        except Exception as exc:  # noqa: BLE001
            logger.info("side panel fields: %s get_fieldsets failed: %s", obj._meta.label_lower, exc)
    exclude = set()
    if ma is not None:
        try:
            exclude = set(ma.get_exclude(request, obj) or ())
        except Exception:  # noqa: BLE001
            exclude = set()
    names = [f.name for f in obj._meta.concrete_fields
             if f.editable and not f.auto_created and not isinstance(f, models.AutoField) and f.name not in exclude]
    return [(None, {"fields": names})]


def _form_fields(ma, request, obj, names) -> dict:
    """Поля класу форми картки (`base_fields`) — підписи й поля-паролі. Екземпляр форми не створюється."""
    if ma is None:
        return {}
    try:
        form_class = ma.get_form(request, obj, fields=[n for n in names if isinstance(n, str)])
    except Exception as exc:  # noqa: BLE001 — без форми ключі все одно впізнаються за назвою
        logger.info("side panel fields: %s get_form failed: %s", obj._meta.label_lower, exc)
        return {}
    return dict(getattr(form_class, "base_fields", None) or {})


def model_field(opts, name):
    """Поле моделі, яке «Усі поля» показують (звичайне, FK, O2O, M2M), або None (метод, зворотний зв'язок…)."""
    if not isinstance(name, str):
        return None
    try:
        f = opts.get_field(name)
    except Exception:  # noqa: BLE001
        return None
    concrete = getattr(f, "concrete", False)
    if getattr(f, "auto_created", False) and not concrete:
        return None                                   # зворотний зв'язок
    if concrete or (getattr(f, "many_to_many", False) and not getattr(f, "auto_created", False)):
        return f
    return None


def _label(f, form_field=None) -> str:
    label = getattr(form_field, "label", None) if form_field is not None else None
    return _clean_text(label or capfirst(f.verbose_name)) or f.name


def _card_label(name, model, ma, form_fields) -> str:
    """Назва того, що є лише в картці (метод адмінки / моделі, поле лише форми) — без виконання."""
    from django.contrib.admin.utils import label_for_field
    from django.forms.utils import pretty_name
    if isinstance(name, str) and name in form_fields:
        label = getattr(form_fields[name], "label", None) or pretty_name(name)
    else:
        try:
            label = label_for_field(name, model, model_admin=ma)
        except Exception:  # noqa: BLE001
            label = pretty_name(name) if isinstance(name, str) else ""
    text = _clean_text(label)
    return "" if text in ("", "--") else text


def _blank(text=DASH):
    return format_html('<span class="mvp-muted">{}</span>', text), True


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return not value
    if hasattr(value, "name") and hasattr(value, "storage"):   # FieldFile без файлу
        return not value
    return False


def number(value, places=None) -> str:
    """Число як у панелі: «1 234,50». До 2 знаків поля — рівно стільки; більше — без зайвих нулів."""
    try:
        if isinstance(value, Decimal):
            d = value
        elif isinstance(value, float):
            d = Decimal(repr(value))
        else:
            d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return str(value)
    if not d.is_finite():
        return str(value)
    if places is not None and 0 <= places <= 2:
        digits = int(places)
    elif d == d.to_integral_value():
        digits = 0
    else:
        digits = max(2, -d.normalize().as_tuple().exponent)
    whole, _, frac = f"{abs(d):.{digits}f}".partition(".")
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    text = " ".join(groups) or "0"
    if frac:
        text += "," + frac
    return f"-{text}" if d < 0 else text


def _choice_label(choices, value):
    try:
        return dict(choices).get(value)
    except TypeError:
        from django.utils.hashable import make_hashable
        return dict(make_hashable(choices)).get(make_hashable(value))


def _rel_html(request, rel) -> SafeString:
    if rel._meta.app_label in PRIVATE_TARGET_APPS:
        return format_html('<span class="mvp-muted">{}</span>', "🔒 є запис")
    return sp.admin_link(request, rel)


def _new_tab_link(href, text) -> SafeString:
    return format_html('<a class="mvp-link" href="{}" target="_blank" rel="noopener" translate="no">{}</a>',
                       href, text)


def _file_html(value) -> SafeString:
    try:
        url = str(value.url or "")
    except Exception:  # noqa: BLE001 — сховище не дає адреси
        url = ""
    name = os.path.basename(str(getattr(value, "name", "") or "")) or str(value)
    if url.startswith("/") or url.lower().startswith(("http://", "https://")):
        href = sp.safe_href(url)
        if href:
            return _new_tab_link(href, name)        # /media/… — з перевіркою доступу (tabele/media.py)
    return sp.data(name)


def _url_html(value) -> SafeString:
    text = str(value)
    try:
        scheme = urlsplit(text).scheme.lower()
    except ValueError:
        scheme = ""
    href = sp.safe_href(text) if scheme in ("http", "https") else ""
    return _new_tab_link(href, text) if href else sp.data(text)


def _text_html(value) -> SafeString:
    lines = str(value).splitlines() or [str(value)]
    body = format_html_join(mark_safe("<br>"), "{}", ((line,) for line in lines))
    return format_html('<div class="mvp-clamp" data-mvp-clamp translate="no">{}</div>', body)


def _json_key_secret(key) -> bool:
    return is_secret_name(str(key)) or bool(JSON_SECRET_RE.search(str(key)))


def mask_json(value, depth=0):
    """JSON без значень ключів з «секретними» назвами (на будь-якій глибині)."""
    if depth > 30:
        return "…"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _json_key_secret(k) and not _is_blank(v):
                out[k] = SAVED_TEXT
            else:
                out[k] = mask_json(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [mask_json(v, depth + 1) for v in value]
    return value


def _json_html(value) -> SafeString:
    masked = mask_json(value)
    if isinstance(masked, dict):
        summary = f"{{…}} {len(masked)} {_plural(len(masked), 'ключ', 'ключі', 'ключів')}"
    elif isinstance(masked, list):
        summary = f"[…] {len(masked)} {_plural(len(masked), 'елемент', 'елементи', 'елементів')}"
    else:
        return sp.data(json.dumps(masked, ensure_ascii=False, default=str)[:200])
    text = json.dumps(masked, ensure_ascii=False, indent=1, default=str)
    if len(text) > JSON_MAX:
        text = text[:JSON_MAX - 1] + "…"
    return format_html('<details class="mvp-det"><summary>{}</summary><pre class="mvp-json" translate="no">{}</pre>'
                       '</details>', summary, text)


def _display(request, obj, f, secret: bool):
    """(HTML значення, порожнє?) для одного поля моделі. Нічого з адмінки не виконується."""
    from django.contrib.admin.utils import display_for_field
    from django.core.exceptions import ObjectDoesNotExist
    from django.db import models
    if secret:
        try:
            has = (getattr(obj, f.name).exists() if f.many_to_many
                   else not _is_blank(getattr(obj, f.attname, None)))
        except Exception:  # noqa: BLE001
            has = False
        return (format_html("{}", SAVED_TEXT), False) if has else _blank(UNSET_TEXT)
    if f.many_to_many:
        manager = getattr(obj, f.name)
        items = list(manager.all()[:M2M_MAX])
        if not items:
            return _blank()
        more = manager.count() - len(items) if len(items) >= M2M_MAX else 0
        html = format_html_join(", ", "{}", ((_rel_html(request, item),) for item in items))
        if more > 0:
            html = format_html('{} <span class="mvp-muted">+{}</span>', html, more)
        return html, False
    if f.many_to_one or f.one_to_one:
        try:
            rel = getattr(obj, f.name)
        except ObjectDoesNotExist:
            rel = None
        return (_rel_html(request, rel), False) if rel is not None else _blank()
    value = f.value_from_object(obj)
    if isinstance(f, models.BooleanField):
        if value is None:
            return _blank()
        return format_html("{}", YES_TEXT if value else NO_TEXT), False
    if _is_blank(value):
        return _blank()
    choices = getattr(f, "flatchoices", None)
    if choices:
        label = _choice_label(choices, value)
        if label not in (None, ""):
            return format_html("{}", label), False   # підпис з коду — перекладається разом з інтерфейсом
        return sp.data(str(value)), False
    if isinstance(f, models.DateTimeField):
        return sp.data(sp.dt(value)), False
    if isinstance(f, models.DateField):
        return sp.data(sp.date(value)), False
    if isinstance(f, models.TimeField):
        return sp.data(value.strftime("%H:%M")), False
    if isinstance(f, models.DecimalField):
        return sp.data(number(value, f.decimal_places)), False
    if isinstance(f, models.FloatField):
        return sp.data(number(value)), False
    if isinstance(f, models.IntegerField):
        return sp.data(str(value)), False
    if isinstance(f, models.FileField):
        return _file_html(value), False
    if isinstance(f, models.URLField):
        return _url_html(value), False
    if isinstance(f, models.JSONField):
        return _json_html(value), False
    if isinstance(f, models.TextField):
        return _text_html(value), False
    if isinstance(f, models.BinaryField):
        return format_html('<span class="mvp-muted">{}</span>', "двійкові дані"), False
    return sp.data(str(display_for_field(value, f, DASH))), False


def _row(request, obj, f, form_field=None, secret_widget=False) -> dict:
    secret = bool(secret_widget) or is_secret_name(f.name)
    try:
        html, empty = _display(request, obj, f, secret)
    except Exception as exc:  # noqa: BLE001 — одне поле не ламає розділ
        logger.info("side panel fields: %s.%s failed: %s", obj._meta.label_lower, f.name, exc)
        html, empty = _blank()
    return {"name": f.name, "label": _label(f, form_field), "html": html, "empty": empty, "secret": secret}


def field_groups(ma, request, obj, extra_fields=None) -> list:
    """Групи полів картки для «Усі поля»: [{index, title, rows: [{name, label, html, empty, secret}], card_only}].

    `index` — номер групи в `ma.get_fieldsets` (якір картки `#fieldset-0-<index>-heading`), «Ще поля»
    (`extra_fields`, без них — `Panel.extra_fields` панелі моделі) — `index` None. У рядках лише готовий HTML
    значення; ключів і паролів у ньому немає. Доступ до запису перевіряє той, хто викликає."""
    from django import forms
    model, opts = type(obj), obj._meta
    fieldsets = _fieldsets(ma, request, obj)
    names = [n for _title, options in fieldsets for n in _flat(options.get("fields"))]
    form_fields = _form_fields(ma, request, obj, names)
    passwords = {n for n, ff in form_fields.items() if isinstance(getattr(ff, "widget", None), forms.PasswordInput)}
    seen = set()
    groups = []
    for index, (title, options) in enumerate(fieldsets):
        rows, card_only = [], []
        for name in _flat(options.get("fields")):
            f = model_field(opts, name)
            if f is None:
                label = _card_label(name, model, ma, form_fields)
                if label and label not in card_only:
                    card_only.append(label)
                continue
            if f.name in seen:
                continue
            seen.add(f.name)
            rows.append(_row(request, obj, f, form_fields.get(f.name), f.name in passwords))
        groups.append({"index": index, "title": _clean_text(title) if title else "", "rows": rows,
                       "card_only": card_only})
    if extra_fields is None:
        panel = _panel_of(request, obj)
        extra_fields = getattr(panel, "extra_fields", ()) if panel is not None else ()
    rows = []
    for name in extra_fields or ():
        f = model_field(opts, name)
        if f is None or f.name in seen:
            continue
        seen.add(f.name)
        rows.append(_row(request, obj, f, form_fields.get(f.name), f.name in passwords))
    if rows:
        groups.append({"index": None, "title": EXTRA_TITLE, "rows": rows, "card_only": []})
    return groups


def _anchor(change_url, group) -> str:
    """Якір групи в картці: Django дає id лише групі з назвою."""
    if not change_url or group.get("index") is None or not group.get("title"):
        return ""
    return f"{change_url}#fieldset-0-{int(group['index'])}-heading"


def _row_html(row) -> SafeString:
    return format_html('<div class="mvp-kv-r" data-mvp-row><dt>{}</dt><dd>{}</dd></div>', row["label"], row["html"])


def _rows_html(rows) -> SafeString:
    return mark_safe("".join(str(_row_html(r)) for r in rows))


def _card_link(change_url) -> SafeString:
    return sp.link(change_url, "↗ у картці") if change_url else mark_safe("")


def _card_only_note(items, tail="") -> SafeString:
    """«Ще в картці: А · Б» — кожна назва окремим вузлом (перекладач інтерфейсу знаходить її в словнику)."""
    if not items:
        return mark_safe("")
    names = format_html_join(" · ", "<span>{}</span>", ((item,) for item in items))
    return format_html('<p class="mvp-note"><span>{}</span> {}{}</p>', CARD_ONLY_TEXT, names,
                       format_html(" {}", tail) if tail else "")


def _group_html(group, change_url) -> SafeString:
    filled = [r for r in group["rows"] if not r["empty"]]
    blank = [r for r in group["rows"] if r["empty"]]
    head = ""
    if group["title"]:
        anchor = _anchor(change_url, group)
        card = format_html('<a class="mvp-icon mvp-fg-card" href="{}" aria-label="{}" title="{}">↗</a>', anchor,
                           f"Відкрити в картці: {group['title']}", "Відкрити в картці") if anchor else ""
        head = format_html('<h4 class="mvp-fg-h"><span>{}</span>{}</h4>', group["title"], card)
    rows = format_html('<dl class="mvp-kv">{}</dl>', _rows_html(filled)) if filled else ""
    folded = format_html('<details class="mvp-det"><summary>{}</summary><dl class="mvp-kv">{}</dl></details>',
                         f"Порожні поля ({len(blank)})", _rows_html(blank)) if blank else ""
    # у групи без назви немає «↗» у заголовку — посилання на картку в самому рядку
    tail = _card_link(change_url) if group.get("card_only") and not group["title"] and group["index"] is not None \
        else ""
    more = _card_only_note(group.get("card_only"), tail)
    return format_html('<div class="mvp-fg">{}{}{}{}</div>', head, rows, folded, more)


def fields_html(groups, change_url="") -> SafeString:
    """HTML розділу «Усі поля» з `field_groups`: групи `.mvp-fg` з «↗» до групи в картці, порожні поля —
    у «Порожні поля (N)», понад 20 рядків — «🔎 Знайти поле»; групи картки без полів моделі — «Ще в картці»."""
    total = sum(len(g["rows"]) for g in groups)
    out = []
    if total > FILTER_FROM:
        out.append(format_html('<input type="search" class="mvp-filter" data-mvp-filter placeholder="{}" '
                               'aria-label="{}">', "🔎 Знайти поле", "Знайти поле"))
    links, plain = [], False
    for group in groups:
        if group["rows"]:
            out.append(_group_html(group, change_url))
        elif group.get("title"):                      # група картки без полів моделі — посилання на неї
            anchor = _anchor(change_url, group)
            links.append(sp.link(anchor, group["title"]) if anchor else group["title"])
        elif group.get("card_only"):
            links += list(group["card_only"])
            plain = True
    if links:
        out.append(_card_only_note(links, _card_link(change_url) if plain else ""))
    if not any(g["rows"] for g in groups) and not links:
        return mark_safe("")
    return mark_safe("".join(str(x) for x in out))


def _render_fields(obj, request):
    ma = sp.admin_for(request, obj)
    if ma is not None and not _may_view(ma, request, obj):
        return ""
    return fields_html(field_groups(ma, request, obj), _change_url(obj))


# ── «🔗 Пов’язане» ─────────────────────────────────────────────────────────────

def relations_of(model) -> list:
    """Зворотні зв'язки моделі (FK / O2O / M2M інших моделей на неї), які розділ може показати — без запитів
    і без прав людини: модель зареєстрована в адмінці, не пошта / календар / база знань / службове / секрети."""
    out = []
    for rel in model._meta.related_objects:
        rm, field = getattr(rel, "related_model", None), getattr(rel, "field", None)
        if rm is None or field is None:
            continue
        o = rm._meta
        if o.app_label in RELATED_SKIP_APPS or o.app_label in sp.NEVER_APPS:
            continue
        if o.label_lower in RELATED_SKIP_MODELS or o.label_lower in sp.NEVER_MODELS:
            continue
        if _admin(rm) is None:
            continue
        out.append(rel)
    return out


def has_relations(obj) -> bool:
    return bool(relations_of(type(obj)))


def _can_see(request, model) -> bool:
    o = model._meta
    rma = _admin(model)
    if rma is None:
        return False
    try:
        return bool(request.user.has_perm(f"{o.app_label}.view_{o.model_name}")
                    and rma.has_view_or_change_permission(request))
    except Exception:  # noqa: BLE001
        return False


def _rel_labels(rels) -> list:
    """Назва кожного зв'язку; два зв'язки з тією самою моделлю — ще й назва поля («📝 Angebote (Замовлення…)»)."""
    seen = {}
    for rel in rels:
        seen[rel.related_model] = seen.get(rel.related_model, 0) + 1
    out = []
    for rel in rels:
        base = _clean_text(capfirst(rel.related_model._meta.verbose_name_plural))
        if seen[rel.related_model] > 1:
            base = f"{base} ({_clean_text(rel.field.verbose_name)})"
        out.append(base)
    return out


def _changelist_url(rel, obj) -> str:
    from django.urls import NoReverseMatch, reverse
    o = rel.related_model._meta
    try:
        base = reverse(f"admin:{o.app_label}_{o.model_name}_changelist")
    except NoReverseMatch:
        return ""
    return f"{base}?{urlencode({f'{rel.field.name}__{obj._meta.pk.name}__exact': obj.pk})}"


def _count_text(n) -> str:
    return f"{COUNT_LIMIT}+" if n > COUNT_LIMIT else str(n)


def _relation_html(request, obj, rel, label, n, qs) -> SafeString:
    count = _count_text(n)
    if rel.one_to_one:
        item = qs.first()
        return format_html('<div class="mvp-line" data-mvp-row><div class="mvp-row"><span>{}</span><span>{}</span>'
                           '</div></div>', label, sp.admin_link(request, item) if item is not None else DASH)
    items = list(qs[:RELATED_FIRST])
    links = format_html_join(" · ", "{}", ((sp.admin_link(request, item),) for item in items))
    url = _changelist_url(rel, obj)
    more = sp.more(url, f"Усі {count} →") if url else ""
    return format_html('<div class="mvp-line" data-mvp-row><div class="mvp-row"><span>{}</span>'
                       '<span class="mvp-note">{}</span></div><div>{}</div>{}</div>', label, count, links, more)


def related_html(request, obj) -> SafeString:
    """Розділ «Пов’язане»: зв'язки з записами (найбільші першими, ≤ 12, по 3 перших + «Усі N →»), порожні —
    у «Порожні зв’язки (N)». Записи — з queryset адмінки пов'язаної моделі (її обмеження для цієї людини)."""
    rels = [rel for rel in relations_of(type(obj)) if _can_see(request, rel.related_model)]
    labels = _rel_labels(rels)
    full, empty = [], []
    for i, rel in enumerate(rels):
        try:
            qs = _admin(rel.related_model).get_queryset(request).filter(**{rel.field.name: obj})
            n = qs.values("pk")[:COUNT_LIMIT + 1].count()      # лише ключі: без анотацій списку адмінки
        except Exception as exc:  # noqa: BLE001 — один зв'язок не ламає розділ
            logger.info("side panel related %s → %s failed: %s", obj._meta.label_lower,
                        rel.related_model._meta.label_lower, exc)
            continue
        (full if n else empty).append((i, rel, n, qs))
    if not full:
        return mark_safe("")
    full.sort(key=lambda item: -item[2])
    out = []
    for i, rel, n, qs in full[:RELATED_MAX]:
        try:
            out.append(_relation_html(request, obj, rel, labels[i], n, qs))
        except Exception as exc:  # noqa: BLE001
            logger.info("side panel related %s → %s rows failed: %s", obj._meta.label_lower,
                        rel.related_model._meta.label_lower, exc)
    rest = len(full) - RELATED_MAX
    if rest > 0:
        out.append(format_html('<p class="mvp-note">{}</p>', f"Ще зв’язків: {rest}"))
    if empty:
        names = " · ".join(labels[i] for i, _rel, _n, _qs in empty)
        out.append(format_html('<details class="mvp-det"><summary>{}</summary><p class="mvp-note">{}</p></details>',
                               f"Порожні зв’язки ({len(empty)})", names))
    return mark_safe("".join(str(x) for x in out))


def _render_related(obj, request):
    return related_html(request, obj)


# ── «🕘 Історія» ────────────────────────────────────────────────────────────────

def history_entries(obj) -> list:
    """Останні записи Django LogEntry запису (≤ 10, новіші першими) — один запит на запит панелі."""
    def load():
        from django.contrib.admin.models import LogEntry
        from django.contrib.admin.options import get_content_type_for_model
        ct = get_content_type_for_model(obj)
        return list(LogEntry.objects.filter(content_type_id=ct.pk, object_id=str(obj.pk))
                    .select_related("user").order_by("-action_time", "-pk")[:HISTORY_MAX])
    return sp.memo(obj, "generic:history", load)


def _who(entry):
    """(ім'я, це дані запису?) — повне ім'я або логін; без людини — «система»."""
    user = getattr(entry, "user", None)
    if user is None:
        return SYSTEM_USER, False
    name = (user.get_full_name() or "").strip() or user.get_username()
    return (name, True) if name else (SYSTEM_USER, False)


def _history_link(request, obj) -> SafeString:
    from django.contrib.admin.utils import quote
    from django.urls import NoReverseMatch, reverse
    panel = _panel_of(request, obj)
    if panel is not None and not panel.history_link:
        return mark_safe("")
    ma = sp.admin_for(request, obj)
    if ma is None or not _may_view(ma, request, obj):
        return mark_safe("")
    o = obj._meta
    try:
        url = reverse(f"admin:{o.app_label}_{o.model_name}_history", args=[quote(obj.pk)])
    except NoReverseMatch:
        return mark_safe("")
    return sp.more(url, "📜 Уся історія →")


def history_html(request, obj) -> SafeString:
    """Розділ «Історія»: хто (ім'я / логін), коли, ➕ / ✏️ / 🗑 і що змінено (повідомлення Django)."""
    entries = history_entries(obj)
    if not entries:
        return mark_safe("")
    rows = []
    for entry in entries:
        icon, what = ACTIONS.get(entry.action_flag, ("•", ""))
        name, is_data = _who(entry)
        try:
            message = entry.get_change_message()
        except Exception:  # noqa: BLE001
            message = ""
        rows.append(format_html(
            '<div class="mvp-line" data-mvp-row><div class="mvp-row"><span><span role="img" aria-label="{}" '
            'title="{}">{}</span> {}</span><span class="mvp-note">{}</span></div>{}</div>',
            what or "Дія", what or "Дія", icon, sp.data(name) if is_data else name, sp.dt(entry.action_time),
            format_html('<div class="mvp-note" translate="no">{}</div>', message) if message else ""))
    return mark_safe("".join(str(r) for r in rows) + str(_history_link(request, obj)))


def _history_summary(obj, request) -> str:
    entries = history_entries(obj)
    if not entries:
        return ""
    return f"{sp.dt(entries[0].action_time)} · {_who(entries[0])[0]}"


def _render_history(obj, request):
    return history_html(request, obj)


# ── розділи й загальна панель ──────────────────────────────────────────────────

def make(key, **overrides) -> sp.Block:
    """Загальний розділ «fields» / «related» / «history»; `overrides` — поля Block (lazy=False, default=True…)."""
    if key == FIELDS:
        block = sp.Block(FIELDS, "Усі поля", _render_fields, default=True, icon="🧾", lazy=True, collapsed=True,
                         empty=EMPTY_FIELDS)
    elif key == RELATED:
        block = sp.Block(RELATED, "Пов’язане", _render_related, default=False, icon="🔗", lazy=True, collapsed=True,
                         applies=has_relations, empty=EMPTY_RELATED)
    elif key == HISTORY:
        block = sp.Block(HISTORY, "Історія", _render_history, default=True, icon="🕘", lazy=True, collapsed=True,
                         summary=_history_summary, empty=EMPTY_HISTORY)
    else:
        raise ValueError(f"side panel: unknown generic block {key!r}")
    return replace(block, **overrides) if overrides else block


def _title(obj) -> SafeString:
    return format_html("<b>{}</b>", sp.data(str(obj)))


def _subtitle(obj, request) -> str:
    return _clean_text(capfirst(obj._meta.verbose_name))


def generic_panel(model: str):
    """Загальна панель списку моделі «app.model» (лише `side_panel.GENERIC`, викликає side_panel.get):
    за замовчуванням вимкнена; «Усі поля» — одразу, «Пов’язане» й «Історія» — на вимогу."""
    from django.apps import apps
    if not isinstance(model, str) or "." not in model:
        return None
    if model in sp.NEVER_MODELS or model.split(".", 1)[0] in sp.NEVER_APPS:
        return None
    try:
        apps.get_model(model)
    except (LookupError, ValueError):
        return None
    return sp.register(model, title=_title, subtitle=_subtitle,
                       blocks=[make(FIELDS, lazy=False, collapsed=False), make(RELATED, default=True), make(HISTORY)],
                       generic_blocks=(), generic=True)
