"""Тема й доступність інтерфейсу — особисто для кожного (`UserProfile.ui_prefs`), однаково на всіх пристроях.

Що можна вибрати — лише тут: `THEMES` і `OPTIONS` (значення → атрибут на <html>, CSS — у
`templates/admin/base_site.html`, блок «♿ Доступність»). `clean` пропускає лише відомі значення, тож у сторінку
(скрипт у <head>) потрапляють тільки вони. Збереження — «Мої налаштування» і перемикач тем у меню
(`core.views.my_settings_view`); `save` — під `select_for_update` рядка профілю.
`sidebar` (`data-mv-sidebar`: auto / wide / rail) — бокове меню нового вигляду на ПК лише значками
(static/admin/js/mv_desktop.js, html.mvd-rail ще до першого малювання — скрипт у <head>).
"""
import json

THEMES = {
    "dark": "🌙 Темна",
    "light": "☀️ Світла",
    "minerva": "⚜️ Minerva",
    "hc-dark": "♿ Контраст — темна",
    "hc-light": "♿ Контраст — світла",
    "custom": "🎨 Власна",
    "auto": "🔁 Як у системі",
}

# ключ → (атрибут <html>, значення за замовчуванням, допустимі значення з підписами)
OPTIONS = {
    "design": ("data-mv-design", "new", {
        "new": "Новий — меню «Просте / Повне», пошук усюди (Ctrl K), списки й фільтри по-новому",
        "classic": "Класичний — як було до 11.2026",
    }),
    # бокове меню в новому вигляді на ПК (static/admin/js/mv_desktop.js → html.mvd-rail; ⇤ / ⇥ унизу меню)
    "sidebar": ("data-mv-sidebar", "auto", {
        "auto": "Само звужується до значків, коли відкрита панель праворуч і списку тісно або вікно вужче за 1200 px",
        "wide": "Завжди повне — з назвами",
        "rail": "Завжди лише значки — підменю при наведенні або кліку",
    }),
    "fix_contrast": ("data-mv-fix-contrast", "1", {
        "1": "Так — підправляти колір тексту, що зливається з фоном",
        "0": "Ні — показувати кольори як є",
    }),
    "font": ("data-mv-font", "default", {
        "default": "Звичайний",
        "readable": "Легкий для читання (Atkinson Hyperlegible / Verdana, ширші інтервали)",
    }),
    "text_size": ("data-mv-size", "100", {
        "100": "100 %", "112": "112 %", "125": "125 %", "150": "150 %",
    }),
    "focus": ("data-mv-focus", "keyboard", {
        "keyboard": "Рамка фокусу при роботі з клавіатури",
        "always": "Рамка фокусу завжди, і після кліку мишею",
    }),
    "links": ("data-mv-links", "auto", {
        "auto": "Як на сторінці",
        "underline": "Підкреслювати всі посилання",
    }),
    "motion": ("data-mv-motion", "auto", {
        "auto": "Як у системі (Windows / macOS: «менше анімацій»)",
        "reduce": "Без анімацій і плавних переходів",
    }),
}


TITLES = {
    "design": ("🎨 Вигляд Minerva на ПК", "Повернутись до старого вигляду можна будь-коли — тут або в меню «🎨» унизу "
                                         "бокового меню. На телефоні вигляд свій, від цього не залежить."),
    "sidebar": ("📐 Бокове меню на ПК", "Діє в новому вигляді. ⇤ / ⇥ унизу меню — швидко перемкнути."),
    "fix_contrast": ("🩹 Підправляти нечитабельний текст",
                     "Світлий текст на світлому фоні (і навпаки) стає темнішим чи світлішим — відтінок той самий."),
    "font": ("🔤 Шрифт", "Atkinson Hyperlegible — безкоштовний шрифт для людей зі слабким зором; встановіть його на ПК, "
                        "інакше Minerva візьме Verdana. Курсив — звичайним напівжирним."),
    "text_size": ("🔍 Розмір тексту", "Збільшує всю сторінку, як Ctrl + «+», але лише в Minerva й на всіх пристроях."),
    "focus": ("⌨ Рамка фокусу", "Помітна рамка навколо поля чи кнопки, де зараз курсор."),
    "links": ("🔗 Посилання", "Підкреслені посилання видно й без розрізнення кольорів."),
    "motion": ("🎞 Анімації", ""),
}


def options_for(prefs: dict) -> list:
    """Для «Моїх налаштувань»: кожен параметр — назва, підказка, варіанти, поточне значення."""
    return [{"key": key, "attr": attr, "title": TITLES[key][0], "hint": TITLES[key][1],
             "values": list(values.items()), "current": prefs.get(key, default)}
            for key, (attr, default, values) in OPTIONS.items()]


def clean(data) -> dict:
    """Лише відомі ключі й значення; решта відкидається."""
    out = {}
    if not isinstance(data, dict):
        return out
    theme = data.get("theme")
    if theme in THEMES:
        out["theme"] = theme
    for key, (_attr, _default, values) in OPTIONS.items():
        val = data.get(key)
        if val is not None and str(val) in values:
            out[key] = str(val)
    return out


def prefs_of(profile) -> dict:
    """Налаштування людини: збережене + значення за замовчуванням. Тема — з `ui_prefs`, інакше старе поле `theme`."""
    saved = clean(getattr(profile, "ui_prefs", None) or {}) if profile is not None else {}
    out = {key: default for key, (_attr, default, _values) in OPTIONS.items()}
    out.update({k: v for k, v in saved.items() if k != "theme"})
    out["theme"] = saved.get("theme") or ""
    out["legacy_theme"] = (getattr(profile, "theme", "") or "") if profile is not None else ""
    return out


def page_config(profile) -> str:
    """JSON для скрипта в <head>: тема (порожньо — як у браузері) і атрибути <html>. Лише значення з `clean`."""
    p = prefs_of(profile)
    attrs = {attr: p[key] for key, (attr, _default, _values) in OPTIONS.items()}
    return json.dumps({"theme": p["theme"], "legacy": p["legacy_theme"], "attrs": attrs}).replace("<", "\\u003c")


def save(profile, data) -> list:
    """Записати вибране; повертає змінені поля профілю. Інше в `ui_prefs` (панелі, меню, нижня панель телефона)
    лишається: рядок профілю перечитується під `select_for_update` і зливається з ним, тож паралельний запис
    (панель праворуч, меню на ПК) не губиться. Об'єкт `profile` після запису — такий, як у базі."""
    from django.db import transaction

    cleaned = clean(data)
    fields = []
    with transaction.atomic():
        row = None
        if getattr(profile, "pk", None) is not None:
            row = type(profile)._default_manager.select_for_update().filter(pk=profile.pk).first()
        target = row if row is not None else profile
        cur = target.ui_prefs if isinstance(target.ui_prefs, dict) else {}
        new = dict(cur)
        new.update(cleaned)
        if new != cur:
            target.ui_prefs = new
            fields.append("ui_prefs")
        theme = new.get("theme")
        if theme and theme != target.theme and theme in dict(profile._meta.get_field("theme").choices):
            target.theme = theme
            fields.append("theme")
        if fields:
            target.save(update_fields=fields)
    if target is not profile:
        profile.ui_prefs = target.ui_prefs
        profile.theme = target.theme
    return fields
