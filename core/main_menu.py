"""🧭 Меню на ПК у новому вигляді (static/admin/js/mv_desktop.js): «Просте» / «Повне», «⭐ Мої», свої значки.

«Просте» — «⭐ Мої» (закріплене людиною) і кілька груп з переліку розділів `core.mobile_nav.ITEMS` (той самий, що
нижня панель телефона; людині — лише доступне, `mobile_nav.available`), «☰ Усі розділи» — «Повне» (бокове меню
з усіма групами, як було). У «Мої» можна закріпити й будь-яку сторінку з повного меню (`url:<адреса>`) — лише
справжню адресу Minerva, яку людина відкриє (`core.access_preview.check_url`). Значок кожного пункту — свій
(емодзі), діє й на телефоні. Вибір — `UserProfile.ui_prefs["menu"]` {mode, pinned, links, icons, filters},
збереження — POST /core/menu/ (правий клік у меню, перемикач «Просте / Повне»).
"""
import json
import re

from core import mobile_nav

MODES = ("simple", "full")
FILTERS = ("drawer", "side")          # фільтри списку: кнопкою збоку («висувна панель») / колонкою, як було
DEFAULT_PINNED = ["home", "orders", "stock", "mail", "calendar"]
MAX_PINNED = 12
LABEL_MAX = 60
HREF_MAX = 300

GROUPS = [
    ("Продажі", ["orders", "offers", "customers"]),
    ("Склад", ["stock", "purchases", "labels"]),
    ("Комунікації й час", ["mail", "calendar", "worktime", "projects"]),
    ("Ще", ["shipments", "dashboard", "signals", "settings", "guide"]),
]

# пошук «усюди» (Ctrl K): розділ → адреса списку з ?q=
SEARCH = [
    ("orders", "Замовлення", "/admin/sales/salesorder/?q="),
    ("stock", "Товари", "/admin/inventory/product/?q="),
    ("customers", "Клієнти", "/admin/crm/customer/?q="),
    ("mail", "Пошта", "/email/?q="),
    ("offers", "Angebote", "/admin/offers/offer/?q="),
    ("shipments", "Відправлення", "/admin/shipping/shipment/?q="),
]

ICON_CHOICES = [
    "🏠", "📦", "🗃", "✉️", "📅", "👥", "📝", "🚚", "🧾", "🏷", "📁", "⏱", "🚨", "📊", "⚙️", "📚",
    "💲", "💶", "🛒", "🏪", "🔷", "📈", "📉", "🔔", "⭐", "❤️", "🔥", "✅", "📌", "🔎", "🧭", "🗂",
    "📋", "📄", "📎", "🖨️", "🏭", "🔧", "🧪", "💡", "🎯", "🤝", "📞", "💬", "🌐", "🔑", "🛡", "🏛️",
]

_HREF_RE = re.compile(r"^/(?!/)[^\s<>\"'\\]*$")


def _profile(user):
    try:
        return user.profile
    except Exception:  # noqa: BLE001
        return None


def _saved(user) -> dict:
    p = _profile(user)
    data = (getattr(p, "ui_prefs", None) or {}).get("menu") if p is not None else None
    return data if isinstance(data, dict) else {}


def clean_icon(value):
    """Значок — короткий символ / емодзі (без літер і цифр ASCII, без розмітки), інакше None."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v or len(v) > 8 or re.search(r"[A-Za-z0-9<>&\"'\\\s]", v):
        return None
    return v


def clean_link(user, href, label):
    """Сторінка з повного меню для «Мої»: (ключ, {href, label}) лише для справжньої адреси, яку людина відкриє."""
    if not isinstance(href, str) or not isinstance(label, str):
        return None
    href = href.strip()
    label = " ".join(label.split())[:LABEL_MAX]
    if not label or len(href) > HREF_MAX or not _HREF_RE.match(href):
        return None
    try:
        from core.access_preview import check_url
        if not check_url(user, href).get("ok"):
            return None
    except Exception:  # noqa: BLE001 — не вдалося перевірити = не закріплювати
        return None
    return "url:" + href, {"href": href, "label": label}


def icons_of(user) -> dict:
    out = {}
    for k, v in (_saved(user).get("icons") or {}).items():
        icon = clean_icon(v)
        if isinstance(k, str) and icon:
            out[k] = icon
    return out


def prefs(user, allowed=None) -> dict:
    """Вибір людини, очищений: лише доступні розділи й перевірені сторінки."""
    allowed = mobile_nav.available(user) if allowed is None else allowed
    saved = _saved(user)
    links = {}
    for k, v in (saved.get("links") or {}).items():
        if isinstance(k, str) and k.startswith("url:") and isinstance(v, dict) and isinstance(v.get("href"), str) \
                and k == "url:" + v["href"] and _HREF_RE.match(v["href"]) and isinstance(v.get("label"), str):
            links[k] = {"href": v["href"], "label": v["label"][:LABEL_MAX]}
    raw = saved.get("pinned")
    pinned = []
    for k in raw if isinstance(raw, list) else DEFAULT_PINNED:
        if isinstance(k, str) and k not in pinned and (k in allowed or k in links):
            pinned.append(k)
    return {
        "mode": saved.get("mode") if saved.get("mode") in MODES else "simple",
        "pinned": pinned[:MAX_PINNED],
        "links": {k: v for k, v in links.items() if k in pinned},
        "icons": icons_of(user),
        "filters": saved.get("filters") if saved.get("filters") in FILTERS else "drawer",
    }


def save(user, data) -> dict:
    """Записати зміну: {mode}, {pinned: [...]}, {pin_link: {href, label}}, {icons: {ключ: значок | null}},
    {filters}, {reset: true} — як було. Повертає очищений вибір."""
    allowed = mobile_nav.available(user)
    p = _profile(user)
    if p is None or not isinstance(data, dict):
        return prefs(user, allowed)
    cur = prefs(user, allowed)
    if data.get("reset"):
        new = None
    else:
        new = dict(cur)
        if data.get("mode") in MODES:
            new["mode"] = data["mode"]
        if data.get("filters") in FILTERS:
            new["filters"] = data["filters"]
        if isinstance(data.get("pinned"), list):
            new["pinned"] = [k for k in dict.fromkeys(data["pinned"])
                             if isinstance(k, str) and (k in allowed or k in cur["links"])][:MAX_PINNED]
        if isinstance(data.get("pin_link"), dict):
            got = clean_link(user, data["pin_link"].get("href"), data["pin_link"].get("label"))
            if got and len(new["pinned"]) < MAX_PINNED:
                key, link = got
                new["links"] = {**new["links"], key: link}
                if key not in new["pinned"]:
                    new["pinned"] = new["pinned"] + [key]
        if isinstance(data.get("icons"), dict):
            icons = dict(new["icons"])
            for k, v in data["icons"].items():
                if not isinstance(k, str) or len(k) > HREF_MAX + 4:
                    continue
                icon = clean_icon(v)
                if icon:
                    icons[k] = icon
                else:
                    icons.pop(k, None)
            new["icons"] = icons
        new["links"] = {k: v for k, v in new["links"].items() if k in new["pinned"]}
    ui = dict(p.ui_prefs or {})
    if new is None:
        ui.pop("menu", None)
    else:
        ui["menu"] = new
    p.ui_prefs = ui
    p.save(update_fields=["ui_prefs"])
    return prefs(user, allowed)


def page_config(user, allowed=None) -> str:
    """JSON для сторінки: розділи людини (стандартні значки), групи «Простого» меню, її вибір, пошук усюди.
    Меню малює браузер — після правого кліку воно перемальовується без перезавантаження."""
    allowed = mobile_nav.available(user) if allowed is None else allowed
    if not allowed:
        return ""
    return json.dumps({
        "items": [{"key": k, "icon": mobile_nav.ITEMS[k][0], "label": mobile_nav.ITEMS[k][1],
                   "href": mobile_nav.ITEMS[k][2]} for k in allowed],
        "groups": [{"title": title, "keys": [k for k in keys if k in allowed]} for title, keys in GROUPS
                   if any(k in allowed for k in keys)],
        "prefs": prefs(user, allowed),
        "search": [{"key": k, "label": label, "href": href} for k, label, href in SEARCH if k in allowed],
        "iconChoices": ICON_CHOICES, "max": MAX_PINNED, "saveUrl": "/core/menu/",
    }, ensure_ascii=False).replace("<", "\\u003c")
