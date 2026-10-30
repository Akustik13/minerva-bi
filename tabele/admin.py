import types
from django.contrib import admin

ALLOWED_HOSTS = ['*']

CSRF_TRUSTED_ORIGINS = [
    'https://akustik.synology.me',
    'https://akustik.synology.me:81',
    'http://192.168.2.123:8000',
]

def _can_open(site, request, model_dict) -> bool:
    model_admin = site._registry.get(model_dict.get("model"))
    if model_admin is None:
        return True
    try:
        return bool(model_admin.has_view_permission(request) or model_admin.has_change_permission(request))
    except Exception:
        return True


def _get_app_list(self, request, app_label=None):
    """Кастомний порядок секцій та моделей у сайдбарі Django Admin."""
    # Bypass Django's model-permission check — Minerva uses its own module system.
    # Temporarily elevate to superuser so _build_app_dict includes all registered apps.
    _was_super = request.user.is_superuser
    if not _was_super:
        request.user.is_superuser = True
    try:
        app_dict = self._build_app_dict(request, app_label)
    finally:
        request.user.is_superuser = _was_super

    app_order = [
        'core',       # 🔐 Ядро системи
        'crm',        # 👥 CRM
        'strategy',   # 🎯 Стратегії CRM
        'sales',      # 🛒 Продажі
        'accounting', # 💰 Бухгалтерія
        'shipping',   # 🚚 Доставка
        'inventory',  # 📦 Управління складом
        'shop',       # 🏪 Інтернет-магазин
        'tasks',      # 📋 Задачі та нагадування
        'autoimport',    # 🔄 Авто-імпорт
        'scraper_hub',  # 📥 Scraper рахунків
        'ai_assistant',     # 🏛️ Minerva AI
        'rag_assistant',    # 📚 Помічник по продукції (RAG)
        'email_assistant',  # 📧 Email Асистент
        'projects',         # 📁 Проєкти з пошти
        'offers',           # 📝 Angebote
        'worktime',         # ⏱ Робочий час (askDANTE)
        'bots',             # 🤖 Боти та AI
        'jlcpcb',           # 🔬 JLCPCB виробництво
        'documents',        # 📄 Документи
        'api',        # 🔑 API Ключі
        'config',     # ⚙️ Конфігурація
        'auth',       # 🔐 Аутентифікація та авторизація
        'authtoken',  # 🔑 API Токени
        'faq',        # ❓ FAQ та підтримка
        'backup',     # 💾 Резервне копіювання
    ]

    # Явний порядок моделей всередині аппів (object_name.lower())
    model_order = {
        'core':     ['auditlog', 'userprofile', 'modulebundle', 'moduleregistry'],
        'strategy': ['aisettings', 'strategytemplate', 'customerstrategy', 'customerstep', 'steplog'],
        'ai_assistant':    ['aiconversation', 'aibudgetlog'],
        'rag_assistant':   ['ragsettings', 'ragconversation', 'knowledgesettings', 'knowledgesource', 'knowledgeitem', 'knowledgedoc'],
        'projects':        ['project', 'dismissedthread'],
        'offers':          ['offer', 'offersettings', 'offerrevision'],
        'worktime':        ['workday', 'danteproject', 'workreport', 'danteuser', 'booking', 'dantesettings'],
        'email_assistant': ['emailaccount', 'emailsettings', 'mailcategory', 'emailmessage', 'emailthread'],
        'sales': ['salesorder', 'salesorderline', 'salessource', 'salescategory', 'salessettings'],
        'inventory': ['product', 'productcategory', 'productalias', 'location',
                      'inventorytransaction', 'reorderproxy', 'supplier',
                      'purchaseorder', 'purchaseorderline'],
        'shipping': ['carrier', 'shippingsettings', 'packagingmaterial', 'shipment', 'orderpackaging'],
        'config':   ['systemsettings', 'documentsettings', 'notificationsettings', 'themesettings'],
        'api':          ['apikey', 'apirequestlog', 'webhook', 'webhookdelivery'],
        'shop':         ['shop', 'shoplisting', 'shopproduct', 'productmedia', 'shopcustomer', 'shoppayment',
                         'shopmailtemplate', 'shopsettings'],
        'scraper_hub':  ['scrapersiteconfig', 'scraperrun', 'scraperdocument'],
        'bots':      ['digikeyconfig', 'digikeylisting', 'digikeypricerun', 'digikeypricesettings', 'bot', 'botlog'],
        'jlcpcb':    ['jlcconfig', 'jlcorder', 'jlcproductmapping'],
        'documents': ['documenttemplate', 'generateddocument'],
    }

    # Моделі що реєструються в admin але не показуються в сайдбарі
    model_exclude = {
        'shipping': ['trackingrule', 'trackingattemptlog'],
    }

    app_list = []
    for app_name in app_order:
        if app_name in app_dict:
            app_list.append(app_dict[app_name])

    # Решта додатків (dashboard, labels_app тощо) — в кінці
    for app_name, app in app_dict.items():
        if app_name not in app_order:
            app_list.append(app)

    # Виключаємо моделі що не мають бути в сайдбарі
    for app in app_list:
        excluded = model_exclude.get(app['app_label'], [])
        if excluded:
            app['models'] = [
                m for m in app['models']
                if m['object_name'].lower() not in excluded
            ]

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

    # Фільтрація по ModuleRegistry + per-user allowed_modules
    try:
        from core.models import ModuleRegistry
        always_show = {"auth", "config", "core"}

        profile = getattr(getattr(request, 'user', None), 'profile', None)
        is_super = _was_super or (profile and profile.role == 'superadmin')

        if is_super:
            # Superuser/superadmin: show all active modules
            app_list = [
                a for a in app_list
                if a["app_label"] in always_show
                or ModuleRegistry.check_active(a["app_label"])
            ]
        else:
            from core.middleware import with_hooks
            allowed = with_hooks(request.user, profile.get_allowed_modules()) if profile else '__all__'
            denied_models = set(getattr(profile, 'denied_models', None) or [])

            app_list = [
                a for a in app_list
                if a["app_label"] in always_show
                or (
                    ModuleRegistry.check_active(a["app_label"])
                    and (allowed == '__all__' or a["app_label"] in allowed)
                )
            ]

            # Моделі, яких людина не відкриє (власні права адмінки — напр. бази знань), — не в меню:
            # список будувався «як суперкористувач», тож перевіряємо справжні права перегляду
            for app in app_list:
                app["models"] = [m for m in app["models"] if _can_open(self, request, m)]
            app_list = [a for a in app_list if a["models"]]

            # Filter denied sub-models within allowed apps
            if denied_models:
                for app in app_list:
                    prefix = app["app_label"] + ":"
                    denied_here = {
                        d[len(prefix):].lower()
                        for d in denied_models
                        if d.startswith(prefix)
                    }
                    if denied_here:
                        app["models"] = [
                            m for m in app["models"]
                            if m["object_name"].lower() not in denied_here
                        ]
    except Exception:
        pass  # БД не мігрована або помилка — показати все

    return app_list


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
admin.site.each_context = types.MethodType(_each_context, admin.site)
admin.site.site_header = '🏛️ Minerva Business Intelligence'
admin.site.site_title = 'Minerva Admin'
admin.site.index_title = 'Панель управління'

# ── Global ModelAdmin permission patch ────────────────────────────────────────
# Many admin classes extend admin.ModelAdmin without AuditableMixin/MinervaAdminMixin.
# Django's default has_view/change/add_permission check model-level auth permissions
# (e.g. "accounting.change_companysettings") which are never assigned to staff users.
# Minerva controls access via ModuleAccessMiddleware + UserProfile.allowed_modules,
# so we replace the base-class defaults to use Minerva's operations system.
# Subclasses that define their own overrides (AuditableMixin, AuditLogAdmin, etc.)
# are NOT affected — Python MRO uses the most-specific definition.

def _mv_has_module_permission(self, request):
    u = request.user
    return bool(u and u.is_active and u.is_staff)


def _mv_has_view_permission(self, request, obj=None):
    u = request.user
    if not (u and u.is_active and u.is_staff):
        return False
    try:
        denied = u.profile.denied_models or []
        if denied and f'{self.opts.app_label}:{self.opts.object_name}' in denied:
            return False
    except Exception:
        pass
    return True


def _mv_has_change_permission(self, request, obj=None):
    u = request.user
    if not (u and u.is_active and u.is_staff):
        return False
    try:
        from core.utils import user_has_operation
        return user_has_operation(u, self.opts.app_label, 'change')
    except Exception:
        return bool(u.is_superuser)


def _mv_has_add_permission(self, request):
    u = request.user
    if not (u and u.is_active and u.is_staff):
        return False
    try:
        from core.utils import user_has_operation
        return user_has_operation(u, self.opts.app_label, 'add')
    except Exception:
        return bool(u.is_superuser)


admin.ModelAdmin.has_module_permission = _mv_has_module_permission
admin.ModelAdmin.has_view_permission   = _mv_has_view_permission
admin.ModelAdmin.has_change_permission = _mv_has_change_permission
admin.ModelAdmin.has_add_permission    = _mv_has_add_permission


# ── Ключі й паролі у формах адмінки ──────────────────────────────────────────
# Поле-пароль (PasswordInput) ніколи не підставляє збережене значення в сторінку: замість нього підказка
# «збережено: sk-a••••••••wxyz — введіть новий, щоб замінити» (core/secrets.py). Старі форми з render_value=True
# покладались на те, що браузер надішле значення назад, — тепер порожнє поле лишає збережене. Людям лише
# з переглядом (поля «тільки читання») ключі й паролі показуються замаскованими.
import re as _re

from django import forms as _forms
from django.contrib.admin import helpers as _helpers

SECRET_NAME_RE = _re.compile(r"(password|secret|api_key|apikey|token|license_key|access_key|private_key|"
                             r"assistant_key)$", _re.I)


def _secret_fields(form, legacy_only=False):
    for name, field in form.fields.items():
        widget = field.widget
        if isinstance(widget, _forms.PasswordInput) and (widget.render_value or not legacy_only):
            yield name, field


def _hide_secrets(form):
    from core.secrets import placeholder
    instance = getattr(form, "instance", None)
    for name, field in _secret_fields(form):
        stored = getattr(instance, name, "") if instance is not None and instance.pk else ""
        legacy = field.widget.render_value
        field.widget.render_value = False
        if legacy and isinstance(stored, str):
            field.widget.attrs["placeholder"] = placeholder(stored, empty="")
            field.widget.attrs.setdefault("autocomplete", "new-password")
            if stored and not field.help_text:
                field.help_text = "Порожньо — залишити збережене."


_mv_orig_render_change_form = admin.ModelAdmin.render_change_form
_mv_orig_save_form = admin.ModelAdmin.save_form


def _mv_render_change_form(self, request, context, *args, **kwargs):
    form = getattr(context.get("adminform"), "form", None)
    if form is not None:
        _hide_secrets(form)
    return _mv_orig_render_change_form(self, request, context, *args, **kwargs)


def _mv_save_form(self, request, form, change):
    if change:                                   # поле не показувало збережене — порожнє означає «не змінювати»
        for name, _field in _secret_fields(form, legacy_only=True):
            old = form.initial.get(name)
            if old and not form.cleaned_data.get(name):
                form.cleaned_data[name] = old
                setattr(form.instance, name, old)
    return _mv_orig_save_form(self, request, form, change)


_mv_orig_readonly_contents = _helpers.AdminReadonlyField.contents


def _mv_readonly_contents(self):
    name = self.field.get("field")
    if isinstance(name, str) and SECRET_NAME_RE.search(name):
        value = getattr(getattr(self.form, "instance", None), name, None)
        if isinstance(value, str):
            from core.secrets import mask
            return mask(value) or self.empty_value_display
    return _mv_orig_readonly_contents(self)


admin.ModelAdmin.render_change_form = _mv_render_change_form
admin.ModelAdmin.save_form = _mv_save_form
_helpers.AdminReadonlyField.contents = _mv_readonly_contents
