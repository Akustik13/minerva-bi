import types
from django.contrib import admin
from django.http import HttpResponseRedirect


ALLOWED_HOSTS = ['*']

CSRF_TRUSTED_ORIGINS = [
    'https://akustik.synology.me',
    'https://akustik.synology.me:81',
    'http://192.168.2.123:8000',
]

def _get_app_list(self, request, app_label=None):
    """Кастомний порядок секцій та моделей у сайдбарі Django Admin."""
    app_dict = self._build_app_dict(request, app_label)

    app_order = [
        'core',       # 🔐 Ядро системи
        'crm',        # 👥 CRM
        'strategy',   # 🎯 Стратегії CRM
        'sales',      # 🛒 Продажі
        'accounting', # 💰 Бухгалтерія
        'shipping',   # 🚚 Доставка
        'inventory',  # 📦 Управління складом
        'tasks',      # 📋 Задачі та нагадування
        'autoimport', # 🔄 Авто-імпорт
        'bots',       # 🤖 Боти та AI
        'api',        # 🔑 API Ключі
        'config',     # ⚙️ Конфігурація
        'auth',       # 🔐 Аутентифікація та авторизація
        'authtoken',  # 🔑 API Токени
        'faq',        # ❓ FAQ та підтримка
        'backup',     # 💾 Резервне копіювання
    ]

    # Явний порядок моделей всередині аппів (object_name.lower())
    model_order = {
        'core':     ['auditlog', 'userprofile', 'moduleregistry'],
        'strategy': ['strategytemplate', 'customerstrategy', 'customerstep', 'steplog'],
        'sales': ['salesorder', 'salesorderline', 'salessource', 'salescategory'],
        'inventory': ['product', 'productcategory', 'productalias', 'location',
                      'inventorytransaction', 'reorderproxy', 'supplier',
                      'purchaseorder', 'purchaseorderline'],
        'shipping': ['carrier', 'shippingsettings', 'packagingmaterial', 'shipment', 'orderpackaging'],
        'config':   ['systemsettings', 'documentsettings', 'notificationsettings', 'themesettings'],
        'api':      ['apikey'],
        'bots':     ['digikeyconfig', 'bot', 'botlog'],
    }

    app_list = []
    for app_name in app_order:
        if app_name in app_dict:
            app_list.append(app_dict[app_name])

    # Решта додатків (dashboard, labels_app тощо) — в кінці
    for app_name, app in app_dict.items():
        if app_name not in app_order:
            app_list.append(app)

    # Сортуємо моделі всередині аппів де задано порядок
    for app in app_list:
        label = app['app_label']
        if label in model_order:
            order = model_order[label]
            app['models'].sort(
                key=lambda m: (
                    order.index(m['object_name'].lower())
                    if m['object_name'].lower() in order
                    else len(order)
                )
            )

    # Фільтрація по ModuleRegistry (core app)
    try:
        from core.models import ModuleRegistry
        always_show = {"auth", "config", "core"}
        app_list = [
            a for a in app_list
            if a["app_label"] in always_show
            or ModuleRegistry.check_active(a["app_label"])
        ]
    except Exception:
        pass  # БД не мігрована або помилка — показати все

    return app_list


def _admin_index(self, request, extra_context=None):
    """Redirect admin homepage → dashboard."""
    return HttpResponseRedirect('/dashboard/')


def _each_context(self, request):
    """Inject theme_custom (ThemeSettings CSS vars) into every admin template context."""
    from django.contrib.admin import AdminSite
    ctx = AdminSite.each_context(self, request)
    try:
        from config.models import ThemeSettings
        ts = ThemeSettings.get()
        ctx['theme_custom'] = ts.as_css_dict()
    except Exception:
        ctx['theme_custom'] = {}
    return ctx


admin.site.get_app_list = types.MethodType(_get_app_list, admin.site)
admin.site.index = types.MethodType(_admin_index, admin.site)
admin.site.each_context = types.MethodType(_each_context, admin.site)
admin.site.site_header = '🏛️ Minerva Business Intelligence'
admin.site.site_title = 'Minerva Admin'
admin.site.index_title = 'Панель управління'
