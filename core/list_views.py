"""Швидкий вигляд списку (чипи над списком адмінки) — що людина вибрала востаннє.

`UserProfile.ui_prefs["list_views"]` {ключ списку: вигляд}, однаково на всіх пристроях. Що означає вигляд і які
значення можливі — вирішує сам список (напр. inventory/po_views.py для закупівель); тут лише читання й запис:
`saved` повертає збережене (або None), `remember` перечитує рядок профілю під `select_for_update` і змінює лише
свій ключ (тема, панелі, меню лишаються), записує лише коли значення справді змінилось.
"""
KEY = "list_views"


def _profile(user):
    try:
        return user.profile
    except Exception:  # noqa: BLE001 — немає профілю: вибір не запам'ятовується
        return None


def saved(user, list_key):
    """Збережений вигляд списку `list_key` або None."""
    p = _profile(user)
    data = (getattr(p, "ui_prefs", None) or {}).get(KEY) if p is not None else None
    value = data.get(list_key) if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def remember(user, list_key, value):
    """Запам'ятати вигляд `value` списку `list_key` (None — забути). True — записано."""
    from django.db import transaction

    p = _profile(user)
    if p is None or p.pk is None or saved(user, list_key) == value:
        return False
    with transaction.atomic():
        row = type(p)._default_manager.select_for_update().filter(pk=p.pk).first()
        if row is None:
            return False
        ui = dict(row.ui_prefs) if isinstance(row.ui_prefs, dict) else {}
        views = dict(ui.get(KEY)) if isinstance(ui.get(KEY), dict) else {}
        if views.get(list_key) == value:
            p.ui_prefs = ui
            return False
        if value is None:
            views.pop(list_key, None)
        else:
            views[list_key] = value
        ui[KEY] = views
        row.ui_prefs = ui
        row.save(update_fields=["ui_prefs"])
        p.ui_prefs = ui
    return True
