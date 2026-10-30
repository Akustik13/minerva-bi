"""📱 Нижня панель на телефоні (static/admin/js/mv_mobile.js): розділи, які людина бачить і може закріпити внизу.
Той самий перелік — «Просте» меню на ПК (core/main_menu.py).

Що можна закріпити — лише `ITEMS` (адреса, право). Людині показуються лише ті, до яких у неї є доступ
(`available`: право моделі за ролями Minerva або доступ до модуля — як у меню). Вибір — до `MAX_PINNED` розділів
(«☰ Ще» завжди п'ятий) у профілі (`UserProfile.ui_prefs["mobile_nav"]`), без вибору — `DEFAULT`. Збереження —
POST /core/mobile-nav/ (зірочки ☆ у меню «Ще»).
"""
import json

MAX_PINNED = 4

# ключ → (значок, назва, адреса, право: "app.view_model" або "module:app", None — усім)
ITEMS = {
    "home":      ("🏠", "Головна", "/admin/", None),
    "orders":    ("📦", "Замовлення", "/admin/sales/salesorder/", "sales.view_salesorder"),
    "stock":     ("🗃", "Склад", "/admin/inventory/product/", "inventory.view_product"),
    "mail":      ("✉️", "Пошта", "/email/", "module:email_assistant"),
    "calendar":  ("📅", "Календар", "/calendar/", "module:calendar_app"),
    "customers": ("👥", "Клієнти", "/admin/crm/customer/", "crm.view_customer"),
    "offers":    ("📝", "Angebote", "/offers/", "offers.view_offer"),
    "shipments": ("🚚", "Відправлення", "/admin/shipping/shipment/", "shipping.view_shipment"),
    "purchases": ("🧾", "Закупівлі", "/admin/inventory/purchaseorder/", "inventory.view_purchaseorder"),
    "labels":    ("🏷", "Етикетки", "/labels/", "module:labels_app"),
    "projects":  ("📁", "Проєкти", "/admin/projects/project/", "projects.view_project"),
    "worktime":  ("⏱", "Робочий час", "/admin/worktime/workday/", "worktime.view_workday"),
    "signals":   ("🚨", "Сигнали", "/dashboard/signals/", "module:dashboard"),
    "dashboard": ("📊", "Дашборд", "/dashboard/", "module:dashboard"),
    "settings":  ("⚙️", "Мої налаштування", "/core/my-settings/", None),
    "guide":     ("📚", "Гід по Minerva", "/guide/", None),
}

DEFAULT = ["home", "orders", "stock", "mail"]


def _allowed(user, perm) -> bool:
    if perm is None:
        return True
    try:
        if perm.startswith("module:"):
            return bool(user.has_module_perms(perm.split(":", 1)[1]))
        return bool(user.has_perm(perm))
    except Exception:  # noqa: BLE001 — помилка перевірки = розділу не показувати
        return False


def available(user) -> list:
    """Ключі розділів, які людина бачить, у порядку `ITEMS`."""
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_staff", False):
        return []
    return [k for k, (_i, _l, _h, perm) in ITEMS.items() if _allowed(user, perm)]


def clean(keys, allowed) -> list:
    """Лише відомі й доступні ключі, без повторів, до `MAX_PINNED`."""
    out = []
    for k in keys if isinstance(keys, (list, tuple)) else []:
        if isinstance(k, str) and k in allowed and k not in out:
            out.append(k)
    return out[:MAX_PINNED]


def _profile(user):
    try:
        return user.profile
    except Exception:  # noqa: BLE001
        return None


def pinned(user, allowed=None) -> list:
    """Закріплене людиною; нічого не вибрано (або вибране вже недоступне) — `DEFAULT`, доповнене доступними."""
    allowed = available(user) if allowed is None else allowed
    p = _profile(user)
    saved = (getattr(p, "ui_prefs", None) or {}).get("mobile_nav") if p is not None else None
    if isinstance(saved, list):
        mine = clean(saved, allowed)
        if mine or saved == []:
            return mine
    out = clean(DEFAULT, allowed)
    for k in allowed:
        if len(out) >= MAX_PINNED:
            break
        if k not in out:
            out.append(k)
    return out


def save(user, keys) -> list:
    """Записати вибір (None — як було, за замовчуванням); повертає закріплене."""
    allowed = available(user)
    p = _profile(user)
    if p is not None:
        ui = dict(p.ui_prefs or {})
        if keys is None:
            ui.pop("mobile_nav", None)
        else:
            ui["mobile_nav"] = clean(keys, allowed)
        p.ui_prefs = ui
        p.save(update_fields=["ui_prefs"])
    return pinned(user, allowed)


def page_config(user, allowed=None) -> str:
    """JSON для сторінки (json_script не потрібен: лише відомі значення з `ITEMS`)."""
    allowed = available(user) if allowed is None else allowed
    if not allowed:
        return ""
    from core import main_menu
    icons = main_menu.icons_of(user)                       # свої значки (правий клік у меню на ПК)
    items = [{"key": k, "icon": icons.get(k) or ITEMS[k][0], "label": ITEMS[k][1], "href": ITEMS[k][2]} for k in allowed]
    return json.dumps({"items": items, "pinned": pinned(user, allowed), "max": MAX_PINNED,
                       "saveUrl": "/core/mobile-nav/"}, ensure_ascii=False).replace("<", "\\u003c")
