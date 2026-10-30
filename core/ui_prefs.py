"""Тема й доступність інтерфейсу — особисто для кожного (`UserProfile.ui_prefs`), однаково на всіх пристроях.

Що можна вибрати — лише тут: `THEMES` і `OPTIONS` (значення → атрибут на <html>, CSS — у
`templates/admin/base_site.html`, блок «♿ Доступність»). `clean` пропускає лише відомі значення, тож у сторінку
(скрипт у <head>) потрапляють тільки вони. Збереження — «Мої налаштування» і перемикач тем у меню
(`core.views.my_settings_view`).
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
    """Записати вибране; повертає змінені поля профілю."""
    new = dict(clean(getattr(profile, "ui_prefs", None) or {}))
    new.update(clean(data))
    fields = []
    if new != (profile.ui_prefs or {}):
        profile.ui_prefs = new
        fields.append("ui_prefs")
    theme = new.get("theme")
    if theme and theme != profile.theme and theme in dict(profile._meta.get_field("theme").choices):
        profile.theme = theme
        fields.append("theme")
    if fields:
        profile.save(update_fields=fields)
    return fields
