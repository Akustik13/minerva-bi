"""
email_assistant/layout.py — 🎨 вигляд пошти, лише для себе (EmailSettings.mail_layout).

Готові шаблони — найвдаліші розкладки відомих поштових програм (Outlook, Gmail, Thunderbird) плюс «як зараз» і
«як раніше» в Minerva; кожен перемикач можна змінити окремо — тоді шаблон «Свій». Сторінка лише міняє класи
`lay-<ключ>-<значення>` на #em-app (CSS у inbox.html), тож зміна видно одразу, без перезавантаження.
"""

OPTIONS = {
    'nav':     ('tabs', 'rail', 'stack'),       # ліва колонка: вкладки внизу / значки збоку / усе підряд
    'pane':    ('right', 'below', 'off'),       # лист: праворуч / під списком / замість списку
    'density': ('compact', 'normal', 'roomy'),  # щільність рядків списку
    'preview': ('0', '1', '2'),                 # рядків тексту листа в списку
    'avatars': ('on', 'off'),                   # кружок із логотипом / буквою
    'tags':    ('on', 'off'),                   # мітки рядка: своя категорія, клієнт, проєкт, запит…
    'chips':   ('off', 'on'),                   # назви вкладень у рядку списку
    'att':     ('bottom', 'top'),               # вкладення у відкритому листі: під текстом / над ним
    'keys':    ('on', 'off'),                   # гарячі клавіші (j / k, r, e, # …)
}

PRESETS = [
    ('minerva', '🏛 Minerva', 'Як зараз: вкладки внизу лівої колонки, лист праворуч',
     {'nav': 'tabs', 'pane': 'right', 'density': 'normal', 'preview': '1', 'avatars': 'on', 'tags': 'on',
      'chips': 'off', 'att': 'bottom', 'keys': 'on'}),
    ('classic', '🗂 Як раніше', 'Папки, категорії, проєкти й клієнти — підряд в одній колонці, розділи згортаються',
     {'nav': 'stack', 'pane': 'right', 'density': 'normal', 'preview': '1', 'avatars': 'on', 'tags': 'on',
      'chips': 'off', 'att': 'bottom', 'keys': 'on'}),
    ('outlook', '📘 Outlook', 'Значки розділів збоку, вкладення над текстом листа, назви файлів у списку',
     {'nav': 'rail', 'pane': 'right', 'density': 'normal', 'preview': '1', 'avatars': 'on', 'tags': 'on',
      'chips': 'on', 'att': 'top', 'keys': 'on'}),
    ('gmail', '✉️ Gmail', 'Широкий список на весь екран, лист відкривається замість нього (← назад), файли в рядку',
     {'nav': 'stack', 'pane': 'off', 'density': 'normal', 'preview': '1', 'avatars': 'off', 'tags': 'on',
      'chips': 'on', 'att': 'bottom', 'keys': 'on'}),
    ('thunderbird', '🦅 Thunderbird', 'Класичний: список угорі, лист під ним, щільні рядки без тексту',
     {'nav': 'stack', 'pane': 'below', 'density': 'compact', 'preview': '0', 'avatars': 'off', 'tags': 'on',
      'chips': 'off', 'att': 'bottom', 'keys': 'on'}),
    ('compact', '💻 Ноутбук', 'Найбільше листів на маленькому екрані: значки збоку, щільно, без міток',
     {'nav': 'rail', 'pane': 'right', 'density': 'compact', 'preview': '0', 'avatars': 'off', 'tags': 'off',
      'chips': 'off', 'att': 'top', 'keys': 'on'}),
]
PRESET_VALUES = {key: values for key, _label, _hint, values in PRESETS}
DEFAULT = dict(PRESET_VALUES['minerva'])
CUSTOM = 'custom'


def get(es) -> dict:
    """Чинний вигляд користувача: збережене поверх стандартного, невідоме відкидається."""
    out = dict(DEFAULT)
    saved = es.mail_layout if isinstance(getattr(es, 'mail_layout', None), dict) else {}
    for key, value in saved.items():
        if key in OPTIONS and str(value) in OPTIONS[key]:
            out[key] = str(value)
    return out


def preset_of(values: dict) -> str:
    """Ключ шаблону, якщо всі перемикачі як у ньому, інакше 'custom'."""
    for key, preset in PRESET_VALUES.items():
        if all(values.get(k) == v for k, v in preset.items()):
            return key
    return CUSTOM


def save(es, data: dict) -> dict:
    """{preset: ключ} — шаблон цілком, {reset: true} — як за замовчуванням, інакше {перемикач: значення, …}
    поверх чинного. ValueError — невідомий шаблон / перемикач / значення."""
    if data.get('reset'):
        values = dict(DEFAULT)
    elif 'preset' in data:
        if data['preset'] not in PRESET_VALUES:
            raise ValueError('Невідомий шаблон')
        values = dict(PRESET_VALUES[data['preset']])
    else:
        values = get(es)
        for key, value in data.items():
            if key not in OPTIONS or str(value) not in OPTIONS[key]:
                raise ValueError(f'Невідомий перемикач: {key}')
            values[key] = str(value)
    es.mail_layout = values
    es.save(update_fields=['mail_layout'])
    return values


def classes(values: dict) -> str:
    """Класи #em-app: lay-nav-tabs lay-pane-right …"""
    return ' '.join(f'lay-{key}-{values[key]}' for key in OPTIONS)


def presets() -> list:
    return [{'key': key, 'label': label, 'hint': hint, 'values': values} for key, label, hint, values in PRESETS]
