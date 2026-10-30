"""
core/modules.py — перелік модулів Minerva для 🧩 Реєстру модулів (core.ModuleRegistry).

Одне джерело для `manage.py setup_modules` і для автоматичної реєстрації після кожного `migrate`
(сигнал post_migrate у core/apps.py → register_missing): новий застосунок з'являється в реєстрі,
у ручному списку модулів профілю й у пакетах «за рівнем», щойно на сервері пройде migrate.
Наявні записи не змінюються (вимкнений адміністратором модуль лишається вимкненим).
"""

MODULES = [
    {'app_label': 'core',            'name': 'Ядро системи',                'tier': 'core',     'order': 0},
    {'app_label': 'config',          'name': 'Налаштування',                'tier': 'core',     'order': 1},
    {'app_label': 'auth',            'name': 'Авторизація',                 'tier': 'core',     'order': 2},
    {'app_label': 'dashboard',       'name': 'Дашборд і аналітика',         'tier': 'core',     'order': 3},
    {'app_label': 'faq',             'name': 'FAQ та підтримка',            'tier': 'core',     'order': 4},
    {'app_label': 'guide',           'name': 'Гід по Minerva',              'tier': 'core',     'order': 5},
    {'app_label': 'crm',             'name': 'CRM',                         'tier': 'standard', 'order': 10},
    {'app_label': 'strategy',        'name': 'Стратегії CRM',               'tier': 'standard', 'order': 11},
    {'app_label': 'projects',        'name': 'Проєкти',                     'tier': 'standard', 'order': 12},
    {'app_label': 'sales',           'name': 'Продажі',                     'tier': 'standard', 'order': 20},
    {'app_label': 'accounting',      'name': 'Бухгалтерія',                 'tier': 'standard', 'order': 21},
    {'app_label': 'offers',          'name': 'Angebote (пропозиції)',       'tier': 'standard', 'order': 22},
    {'app_label': 'shipping',        'name': 'Доставка',                    'tier': 'standard', 'order': 30},
    {'app_label': 'inventory',       'name': 'Склад',                       'tier': 'standard', 'order': 40},
    {'app_label': 'shop',            'name': 'Інтернет-магазин',            'tier': 'premium',  'order': 41},
    {'app_label': 'labels_app',      'name': 'Етикетки DYMO',               'tier': 'standard', 'order': 42},
    {'app_label': 'tasks',           'name': 'Задачі',                      'tier': 'standard', 'order': 50},
    {'app_label': 'ai_assistant',    'name': 'Minerva AI',                  'tier': 'premium',  'order': 55},
    {'app_label': 'rag_assistant',   'name': 'Помічник по продукції (RAG)', 'tier': 'premium',  'order': 55},
    {'app_label': 'email_assistant', 'name': 'Email Асистент',              'tier': 'premium',  'order': 56},
    {'app_label': 'calendar_app',    'name': 'Календар',                    'tier': 'standard', 'order': 57},
    {'app_label': 'worktime',        'name': 'Робочий час (askDANTE)',      'tier': 'standard', 'order': 58},
    {'app_label': 'bots',            'name': 'AI та Боти',                  'tier': 'premium',  'order': 60},
    {'app_label': 'api',             'name': 'REST API',                    'tier': 'premium',  'order': 61},
    {'app_label': 'autoimport',      'name': 'Авто-імпорт',                 'tier': 'premium',  'order': 62},
    {'app_label': 'documents',       'name': 'Документи',                   'tier': 'standard', 'order': 63},
    {'app_label': 'jlcpcb',          'name': 'JLCPCB виробництво',          'tier': 'premium',  'order': 64},
    {'app_label': 'scraper_hub',     'name': 'Scraper рахунків',            'tier': 'premium',  'order': 65},
    {'app_label': 'backup',          'name': 'Резервне копіювання',         'tier': 'standard', 'order': 70},
]

# Застосунки без власного модуля в реєстрі (службові Django / бібліотеки)
NOT_MODULES = frozenset({
    'admin', 'contenttypes', 'sessions', 'messages', 'staticfiles', 'sites',
    'rest_framework', 'authtoken', 'django_filters',
})

DEFAULT_BUNDLES = [
    # ── Tier-based (автоматично підхоплюють нові модулі при setup_modules і реєстрації нового модуля) ──
    {
        'name': '⭐ Повний доступ',
        'color': '#c9a84c',
        'description': 'Всі модулі — для адміністраторів системи',
        'tiers': ('core', 'standard', 'premium'),
        'is_system': True,
    },
    {
        'name': '📦 Стандарт',
        'color': '#58a6ff',
        'description': 'CRM, Продажі, Склад, Доставка, Задачі — базовий пакет',
        'tiers': ('core', 'standard'),
        'is_system': True,
    },
    # ── Рольові пакети ──────────────────────────────────────────────────────
    {
        'name': '💼 Менеджер',
        'color': '#2196f3',
        'description': 'CRM, стратегії, продажі, доставка — для менеджерів з продажів',
        'app_labels': ['crm', 'strategy', 'sales', 'shipping'],
        'is_system': True,
    },
    {
        'name': '📦 Складник',
        'color': '#4caf50',
        'description': 'Склад та доставка — для складських працівників',
        'app_labels': ['inventory', 'shipping'],
        'is_system': True,
    },
    {
        'name': '💰 Бухгалтер',
        'color': '#ff9800',
        'description': 'Продажі, бухгалтерія, склад — для фінансового відділу',
        'app_labels': ['sales', 'accounting', 'inventory'],
        'is_system': True,
    },
    {
        'name': '🎯 CRM-фокус',
        'color': '#9c27b0',
        'description': 'Тільки CRM та стратегії — для аналітиків клієнтської бази',
        'app_labels': ['crm', 'strategy'],
        'is_system': True,
    },
    {
        'name': '🛒 Інтернет-магазин',
        'color': '#e91e63',
        'description': 'Продажі + CRM + Склад + Доставка — для e-commerce',
        'app_labels': ['crm', 'sales', 'inventory', 'shipping'],
        'is_system': True,
    },
    {
        'name': '🔒 Тільки ядро',
        'color': '#607d8b',
        'description': 'Мінімальний доступ — тільки базові системні модулі',
        'app_labels': [],
        'is_system': True,
    },
]


def register_missing(registry=None, bundles=None, using='default') -> list:
    """
    Додати в реєстр модулі з MODULES, яких там ще немає (увімкненими). Наявні записи не чіпає.
    Новий модуль також додається в наявні пакети «за рівнем» (⭐ Повний доступ, 📦 Стандарт), якщо його рівень
    туди входить, — так само, як їх збирає setup_modules. Повертає список доданих app_label.

    registry / bundles — моделі (у post_migrate — історичні з apps), за замовчуванням — core.models.
    """
    if registry is None or bundles is None:
        from core.models import ModuleBundle, ModuleRegistry
        registry = registry or ModuleRegistry
        bundles = bundles or ModuleBundle
    existing = set(registry.objects.using(using).values_list('app_label', flat=True))
    added = []
    for m in MODULES:
        if m['app_label'] in existing:
            continue
        obj = registry.objects.using(using).create(
            app_label=m['app_label'], name=m['name'], tier=m['tier'], order=m['order'], is_active=True)
        added.append(obj)
    if not added:
        return []
    for b in DEFAULT_BUNDLES:
        tiers = b.get('tiers')
        if not tiers:
            continue
        bundle = bundles.objects.using(using).filter(name=b['name']).first()
        if bundle is None:
            continue
        new = [obj for obj in added if obj.tier in tiers]
        if new:
            bundle.modules.add(*new)
    return [obj.app_label for obj in added]


def register_after_migrate(sender, app_config=None, using='default', apps=None, **kwargs):
    """post_migrate: зареєструвати нові модулі (для наявних установок — під час migrate при старті)."""
    try:
        if apps is not None:
            registry = apps.get_model('core', 'ModuleRegistry')
            bundles = apps.get_model('core', 'ModuleBundle')
        else:
            registry = bundles = None
        register_missing(registry, bundles, using=using)
    except Exception:   # noqa: BLE001 — migrate не має падати через реєстр (напр. таблиці ще немає)
        import logging
        logging.getLogger(__name__).warning('ModuleRegistry: не вдалося зареєструвати модулі', exc_info=True)
