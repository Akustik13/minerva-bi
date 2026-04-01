from django import forms
from django.contrib import admin
from django.forms import widgets
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from config.models import (
    ALL_MODULES, SystemSettings, DocumentSettings, NotificationSettings, ThemeSettings,
)


# ── SystemSettings form — checkboxes for enabled_modules ─────────────────────

class SystemSettingsForm(forms.ModelForm):

    enabled_modules_choice = forms.MultipleChoiceField(
        choices=[],   # populated in __init__ from ModuleRegistry or ALL_MODULES
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label='Активні модулі',
        help_text='Відмічені модулі відображаються у бічному меню та доступні для роботи',
    )

    class Meta:
        model = SystemSettings
        exclude = ['enabled_modules']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Build choices from ModuleRegistry (non-core), fallback to ALL_MODULES
        try:
            from core.models import ModuleRegistry
            qs = ModuleRegistry.objects.exclude(tier='core').order_by('order', 'name')
            choices = [(m.app_label, m.name) for m in qs]
        except Exception:
            choices = [(m, m) for m in ALL_MODULES]

        self.fields['enabled_modules_choice'].choices = choices

        # Pre-select currently enabled modules
        current = []
        if self.instance and self.instance.pk:
            current = self.instance.enabled_modules or []
        self.fields['enabled_modules_choice'].initial = current

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.enabled_modules = list(self.cleaned_data.get('enabled_modules_choice', []))
        if commit:
            instance.save()
        return instance


class ColorPickerWidget(widgets.TextInput):
    """Color picker: native color swatch + hex text input, synced via JS."""

    def render(self, name, value, attrs=None, renderer=None):
        safe_val = value or '#000000'
        # Ensure 6-digit hex for type="color"
        if safe_val and len(safe_val) == 4:  # #rgb → #rrggbb
            safe_val = '#' + safe_val[1]*2 + safe_val[2]*2 + safe_val[3]*2
        uid = attrs.get('id', f'id_{name}') if attrs else f'id_{name}'
        return format_html(
            '<div class="mv-cp-wrap">'
            '<input type="color" class="mv-cp-swatch" id="{uid}_sw" value="{sv}" '
            '       oninput="mvCpSync(this,\'{uid}\')">'
            '<input type="text"  class="mv-cp-hex"   id="{uid}" name="{name}" '
            '       value="{v}" placeholder="#rrggbb" maxlength="7" '
            '       oninput="mvCpSyncRev(this,\'{uid}_sw\')">'
            '</div>',
            uid=uid, uid_sw=uid + '_sw', name=name, v=value or '', sv=safe_val,
        )

    class Media:
        css = {'all': ()}
        js = ()

    # Extra inline CSS/JS injected once per page via ThemeSettingsAdmin.Media
    pass


@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    form = SystemSettingsForm

    def changelist_view(self, request, extra_context=None):
        obj, _ = SystemSettings.objects.get_or_create(pk=1, defaults={
            "company_name": "Моя компанія",
        })
        return redirect(reverse("admin:config_systemsettings_change", args=[obj.pk]))

    fieldsets = [
        ("🏢 Компанія", {
            "fields": ("company_name", "logo", "default_currency", "timezone"),
        }),
        ("📦 Активні модулі", {
            "fields": ("enabled_modules_choice", "accounting_level"),
            "description": (
                "Оберіть модулі які активні у системі. "
                "Базові модулі (Ядро, Налаштування, Авторизація) завжди увімкнені."
            ),
        }),
        ("💰 Фінанси", {
            "fields": ("default_vat_rate",),
            "description": "Стандартна ставка ПДВ — підставляється автоматично у нові рахунки-фактури.",
        }),
        ("📐 Одиниці виміру та формати", {
            "fields": ("weight_unit", "dimension_unit", "country_code_format"),
            "description": (
                "Одиниці відображення у відправленнях та пакувальних матеріалах. "
                "Формат країни — ISO-2 (DE) або ISO-3 (DEU)."
            ),
        }),
        ("⚙️ Система", {
            "fields": ("is_onboarding_complete",),
            "description": "Скидайте is_onboarding_complete для повторного запуску wizard.",
        }),
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DocumentSettings)
class DocumentSettingsAdmin(admin.ModelAdmin):
    # Singleton: redirect changelist → change view for pk=1
    def changelist_view(self, request, extra_context=None):
        obj = DocumentSettings.get()
        return redirect(reverse("admin:config_documentsettings_change", args=[obj.pk]))

    fieldsets = [
        ("🌐 Загальне", {
            "fields": ("doc_language",),
            "description": "Мова заголовків і службового тексту у PDF-документах.",
        }),
        ("📦 Пакувальний лист", {
            "fields": ("packing_list_show_prices", "packing_list_footer_note"),
            "description": "Налаштування для Packing List — супровідний список позицій.",
        }),
        ("📄 Proforma Invoice", {
            "fields": ("proforma_payment_terms", "proforma_notes"),
            "description": "Умови оплати та додаткові примітки для proforma.",
        }),
        ("🛃 Митна декларація CN23", {
            "fields": ("customs_default_type", "customs_reason"),
            "description": (
                "Тип декларації підставляється коли у замовленні не вказано тип документа. "
                "Опис використовується як fallback якщо товар не має назви для документів."
            ),
        }),
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(NotificationSettings)
class NotificationSettingsAdmin(admin.ModelAdmin):
    # Singleton: redirect changelist → change view for pk=1
    def changelist_view(self, request, extra_context=None):
        obj = NotificationSettings.get()
        return redirect(reverse("admin:config_notificationsettings_change", args=[obj.pk]))

    readonly_fields = ("alert_actions", "last_alert_sent")

    def get_form(self, request, obj=None, **kwargs):
        from django.forms import PasswordInput
        form = super().get_form(request, obj, **kwargs)
        for field_name in ('email_host_password', 'telegram_bot_token'):
            if field_name in form.base_fields:
                form.base_fields[field_name].widget = PasswordInput(render_value=True)
        return form

    fieldsets = [
        ("📧 Email (SMTP)", {
            "fields": (
                "email_enabled",
                ("email_host", "email_port"),
                ("email_use_tls", "email_use_ssl"),
                "email_host_user", "email_host_password",
                "email_from", "email_to",
            ),
            "description": (
                "Gmail: host=smtp.gmail.com, port=587, TLS=✓. "
                "Потрібен App Password: Google Account → Security → "
                "2-Step Verification → App Passwords."
            ),
        }),
        ("📬 Нові замовлення", {
            "fields": (
                ("new_order_email", "new_order_telegram"),
            ),
            "description": (
                "Миттєве сповіщення при надходженні нового замовлення. "
                "Надсилається через email та/або Telegram."
            ),
        }),
        ("⚙️ Оновлення системи (синхронізації)", {
            "fields": (
                ("sync_result_email", "sync_result_telegram"),
                "sync_skip_if_no_changes",
            ),
            "description": (
                "Надсилати підсумок після автоматичних синхронізацій — "
                "DigiKey (нові замовлення) та авто-трекінг (зміни статусу відправлень). "
                "Сповіщення надходить лише якщо були реальні зміни."
            ),
        }),
        ("🔄 Зміна статусу замовлення", {
            "fields": (
                ("status_change_email", "status_change_telegram"),
                ("notify_on_processing", "notify_on_shipped",
                 "notify_on_delivered", "notify_on_cancelled"),
            ),
            "description": (
                "Сповіщення при зміні статусу. "
                "Оберіть канал (Email / Telegram) та які саме зміни статусу надсилати."
            ),
        }),
        ("🔔 Планові сповіщення (cron)", {
            "fields": (
                "stock_alerts_enabled",
                "deadline_alerts_enabled",
                "deadline_overdue_days",
            ),
            "description": "Запускаються автоматично через cron (send_alerts).",
        }),
        ("⏱️ Розклад", {
            "fields": ("alert_min_interval_hours", "last_alert_sent"),
            "description": (
                "Для автоматичного надсилання додайте cron: "
                "<code>0 */12 * * * docker-compose exec -T web python manage.py send_alerts</code>"
            ),
        }),
        ("📱 Telegram", {
            "fields": ("telegram_enabled", "telegram_bot_token", "telegram_chat_id"),
            "classes": ("collapse",),
            "description": (
                "1. @BotFather → /newbot → скопіювати токен. "
                "2. Додати бота в чат/канал (для каналу — зробити адміністратором). "
                "3. Chat ID: для каналу @username або числовий ID (дізнатись через @userinfobot)."
            ),
        }),
        ("🚀 Дії", {
            "fields": ("alert_actions",),
            "description": (
                "Тест перевіряє SMTP без реальних даних. "
                "'Надіслати зараз' — ігнорує інтервал і перевіряє реальні алерти."
            ),
        }),
    ]

    def alert_actions(self, obj):
        if not obj or not obj.pk:
            return "—"
        return format_html(
            '<a href="../test-email/" style="'
            'display:inline-block;padding:8px 18px;margin-right:10px;'
            'background:#1976d2;color:#fff;border-radius:6px;'
            'text-decoration:none;font-weight:600;font-size:13px">'
            '📧 Надіслати тест</a>'
            '<a href="../send-now/" style="'
            'display:inline-block;padding:8px 18px;margin-right:10px;'
            'background:#ff9800;color:#fff;border-radius:6px;'
            'text-decoration:none;font-weight:600;font-size:13px">'
            '🔔 Перевірити та надіслати зараз</a>'
            '<a href="../test-telegram/" style="'
            'display:inline-block;padding:8px 18px;'
            'background:#0088cc;color:#fff;border-radius:6px;'
            'text-decoration:none;font-weight:600;font-size:13px">'
            '📱 Тест Telegram</a>'
        )
    alert_actions.short_description = "Дії"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "",
                self.admin_site.admin_view(self._redirect_singleton),
                name="config_notificationsettings_changelist",
            ),
            path(
                "<int:pk>/test-email/",
                self.admin_site.admin_view(self._test_email),
                name="config_notificationsettings_test_email",
            ),
            path(
                "<int:pk>/send-now/",
                self.admin_site.admin_view(self._send_now),
                name="config_notificationsettings_send_now",
            ),
            path(
                "<int:pk>/test-telegram/",
                self.admin_site.admin_view(self._test_telegram),
                name="config_notificationsettings_test_telegram",
            ),
        ]
        return custom + urls

    def _redirect_singleton(self, request):
        obj = NotificationSettings.get()
        return redirect(reverse("admin:config_notificationsettings_change", args=[obj.pk]))

    def _test_email(self, request, pk):
        from django.contrib import messages
        from dashboard.notifications import run_alerts
        result = run_alerts(is_test=True, test_channel='email')
        if result.get('sent'):
            messages.success(request, "✅ Тестовий email надіслано! Перевірте поштову скриньку.")
        else:
            err = (
                result.get('email', {}).get('error')
                or result.get('error')
                or result.get('reason')
                or '?'
            )
            messages.error(request, f"❌ Email помилка: {err}")
        return redirect(reverse("admin:config_notificationsettings_change", args=[1]))

    def _send_now(self, request, pk):
        from django.contrib import messages
        from dashboard.notifications import run_alerts
        result = run_alerts(force=True)
        if result.get('sent'):
            messages.success(request, (
                f"✅ Надіслано: {result.get('critical', 0)} critical stock, "
                f"{result.get('overdue', 0)} overdue orders"
            ))
        elif result.get('ok'):
            messages.info(request, f"ℹ️ {result.get('reason', 'Немає алертів для надсилання')}")
        else:
            messages.error(request, f"❌ Помилка: {result.get('error') or result.get('reason', '?')}")
        return redirect(reverse("admin:config_notificationsettings_change", args=[1]))

    def _test_telegram(self, request, pk):
        from django.contrib import messages
        from dashboard.notifications import run_alerts
        result = run_alerts(is_test=True, test_channel='telegram')
        tg = result.get('telegram', {})
        if tg.get('sent'):
            messages.success(request, "✅ Тестове Telegram-повідомлення надіслано! Перевірте чат.")
        elif tg.get('error'):
            messages.error(request, f"❌ Telegram помилка: {tg['error']}")
        else:
            messages.warning(request, "⚠️ Telegram вимкнено або не налаштований (Bot Token / Chat ID).")
        return redirect(reverse("admin:config_notificationsettings_change", args=[1]))

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ThemeSettings)
class ThemeSettingsAdmin(admin.ModelAdmin):
    """Singleton: custom color theme — palette swatch + hex input side by side."""

    def changelist_view(self, request, extra_context=None):
        obj = ThemeSettings.get()
        return redirect(reverse("admin:config_themesettings_change", args=[obj.pk]))

    fieldsets = [
        ("🖼️ Фони", {
            "fields": (("bg_app", "bg_card"), ("bg_card_2", "bg_input"), ("bg_hover",)),
            "description": (
                "Основні фони сторінки і карток. "
                "Порожнє поле = значення активної пресетної теми."
            ),
        }),
        ("✏️ Текст", {
            "fields": (("text_primary", "text_muted", "text_dim"),),
        }),
        ("🎨 Акценти", {
            "fields": (("accent", "gold", "gold_l"),),
            "description": "accent — основний акцентний колір. gold — бренд Minerva.",
        }),
        ("🔴 Статуси", {
            "fields": (("ok", "warn", "err"),),
            "description": "ok=зелений, warn=помаранчевий, err=червоний.",
        }),
        ("🔝 Верхня панель", {
            "fields": (("header_bg", "header_color"),),
            "description": "Фон і колір тексту/посилань верхньої панелі (#header + breadcrumbs).",
        }),
        ("📌 Сайдбар", {
            "fields": (("sb_bg", "sb_head_bg"), ("sb_border", "sb_border_accent"),),
            "description": (
                "sb_bg — фон панелі. "
                "sb_head_bg — фон кнопок-заголовків груп (--mg-header). "
                "sb_border — лінії між секціями. "
                "sb_border_accent — кольорова вертикальна смужка зліва від заголовка групи."
            ),
        }),
        ("📐 Бордюри", {
            "fields": (("border_color",),),
            "description": "Колір рамок таблиць, карток, розділювальних ліній (--border-strong).",
        }),
        ("🖱️ Кнопки", {
            "fields": (("btn_primary", "btn_danger"),),
            "description": "btn_primary = кнопка «Зберегти», btn_danger = кнопка «Видалити».",
        }),
    ]

    class Media:
        css = {'all': ()}
        js = ()

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        for field_name, field in form.base_fields.items():
            field.widget = ColorPickerWidget()
            field.required = False
        return form

    def change_view(self, request, object_id, form_url='', extra_context=None):
        extra_context = extra_context or {}
        extra_context['cp_inline'] = True  # flag for template (not used, but signals intent)
        return super().change_view(request, object_id, form_url, extra_context)

    def render_change_form(self, request, context, *args, **kwargs):
        # Inject color picker CSS+JS via extra_context media
        context['cp_css_js'] = True
        return super().render_change_form(request, context, *args, **kwargs)

    def get_urls(self):
        from django.urls import path as url_path
        urls = super().get_urls()
        custom = [
            url_path(
                'server-profiles/',
                self.admin_site.admin_view(self._server_profiles_list),
                name='config_themesettings_server_profiles',
            ),
            url_path(
                'server-profiles/<str:name>/',
                self.admin_site.admin_view(self._server_profiles_load),
                name='config_themesettings_server_profiles_load',
            ),
        ]
        return custom + urls

    def _server_profiles_list(self, request):
        import os
        from django.conf import settings
        from django.http import JsonResponse
        folder = os.path.join(settings.BASE_DIR, 'theme_profiles')
        profiles = []
        if os.path.isdir(folder):
            profiles = sorted(
                os.path.splitext(f)[0]
                for f in os.listdir(folder)
                if f.endswith('.json')
            )
        return JsonResponse({'profiles': profiles})

    def _server_profiles_load(self, request, name):
        import json
        import os
        import re
        from django.conf import settings
        from django.http import JsonResponse
        if not re.match(r'^[\w-]+$', name):
            return JsonResponse({'error': 'Invalid name'}, status=400)
        folder = os.path.join(settings.BASE_DIR, 'theme_profiles')
        path = os.path.join(folder, name + '.json')
        if not os.path.isfile(path):
            return JsonResponse({'error': 'Not found'}, status=404)
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return JsonResponse(data)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ── Integrations Hub ──────────────────────────────────────────────────────────

def integrations_hub_view(request):
    """Centralised read-only overview of all configured integrations."""
    integrations = []

    # ── DigiKey ───────────────────────────────────────────────────────────────
    try:
        from bots.models import DigiKeyConfig
        dk = DigiKeyConfig.get()
        has_creds = bool(dk.client_id and dk.client_secret)
        has_marketplace = bool(dk.marketplace_access_token or dk.marketplace_refresh_token)
        integrations.append({
            "name":    "DigiKey Marketplace",
            "icon":    "🛒",
            "enabled": dk.sync_enabled and has_creds,
            "status":  (
                "✅ Активна" if (dk.sync_enabled and has_creds)
                else ("⚠️ Credentials відсутні" if not has_creds else "⏸ Вимкнено")
            ),
            "details": [
                ("Режим",             "Sandbox" if dk.use_sandbox else "Production"),
                ("Sync",              "Увімкнено" if dk.sync_enabled else "Вимкнено"),
                ("Marketplace OAuth", "✅ Є токен" if has_marketplace else "❌ Не авторизовано"),
                ("Остання синхр.",    dk.last_synced_at.strftime("%d.%m.%Y %H:%M") if dk.last_synced_at else "—"),
            ],
            "settings_url": "/admin/bots/digikeyconfig/1/change/",
            "extra_links": [
                ("📋 Marketplace замовлення", "/admin/bots/digikeyconfig/1/marketplace-orders/"),
                ("🔍 Звірка",                 "/admin/bots/digikeyconfig/1/reconcile/"),
            ],
        })
    except Exception as e:
        integrations.append({"name": "DigiKey", "icon": "🛒", "enabled": False,
                              "status": f"❌ Помилка: {e}", "details": [],
                              "settings_url": "/admin/bots/", "extra_links": []})

    # ── Carriers (Jumingo / DHL / UPS / FedEx) ────────────────────────────────
    try:
        from shipping.models import Carrier
        carriers = Carrier.objects.all().order_by("carrier_type", "name")
        type_icons = {"jumingo": "🚚", "dhl": "📦", "ups": "🟤", "fedex": "🟣", "other": "🔗"}
        for c in carriers:
            icon = type_icons.get(c.carrier_type, "🔗")
            has_key = bool(c.api_key)
            integrations.append({
                "name":    c.name,
                "icon":    icon,
                "enabled": c.is_active and has_key,
                "status":  (
                    "✅ Активний" if (c.is_active and has_key)
                    else ("⚠️ Немає API ключа" if not has_key else "⏸ Вимкнено")
                ),
                "details": [
                    ("Тип",        c.get_carrier_type_display()),
                    ("Режим",      c.api_url if c.api_url else "Production"),
                    ("За замовч.", "✅ Так" if c.is_default else "—"),
                    ("API ключ",   "✅ Є" if c.api_key else "❌ Відсутній"),
                ],
                "settings_url": f"/admin/shipping/carrier/{c.pk}/change/",
                "extra_links": [],
            })
        if not carriers.exists():
            integrations.append({
                "name": "Перевізники (Jumingo / DHL)", "icon": "🚚", "enabled": False,
                "status": "➕ Не налаштовано",
                "details": [("Підказка", "Додайте перевізника у модулі Доставка")],
                "settings_url": "/admin/shipping/carrier/add/",
                "extra_links": [],
            })
    except Exception as e:
        integrations.append({"name": "Перевізники", "icon": "🚚", "enabled": False,
                              "status": f"❌ Помилка: {e}", "details": [],
                              "settings_url": "/admin/shipping/", "extra_links": []})

    # ── Auto Tracking ─────────────────────────────────────────────────────────
    try:
        from shipping.models import ShippingSettings
        st = ShippingSettings.get()
        last_run = st.last_tracking_run.strftime("%d.%m.%Y %H:%M") if st.last_tracking_run else "—"
        integrations.append({
            "name":    "Авто-трекінг відправлень",
            "icon":    "🔄",
            "enabled": st.auto_tracking_enabled,
            "status":  "✅ Увімкнено" if st.auto_tracking_enabled else "⏸ Вимкнено",
            "details": [
                ("Інтервал",      f"{st.tracking_interval_minutes} хв"),
                ("Останній запуск", last_run),
            ],
            "settings_url": "/admin/shipping/shippingsettings/1/change/",
            "extra_links": [
                ("🔄 Запустити зараз", "/admin/shipping/shippingsettings/1/run-tracking/"),
            ],
        })
    except Exception as e:
        integrations.append({"name": "Авто-трекінг", "icon": "🔄", "enabled": False,
                              "status": f"❌ {e}", "details": [],
                              "settings_url": "/admin/shipping/shippingsettings/1/change/", "extra_links": []})

    # ── Email (SMTP) ──────────────────────────────────────────────────────────
    try:
        ns = NotificationSettings.get()
        integrations.append({
            "name":    "Email (SMTP)",
            "icon":    "📧",
            "enabled": ns.email_enabled,
            "status":  "✅ Увімкнено" if ns.email_enabled else "⏸ Вимкнено",
            "details": [
                ("Host",         ns.email_host or "—"),
                ("Port",         str(ns.email_port)),
                ("TLS/SSL",      "TLS" if ns.email_use_tls else ("SSL" if ns.email_use_ssl else "Немає")),
                ("Отримувачі",   ns.email_to or "—"),
            ],
            "settings_url": "/admin/config/notificationsettings/1/change/",
            "extra_links": [
                ("📧 Тест email", "/admin/config/notificationsettings/1/test-email/"),
            ],
        })
    except Exception as e:
        integrations.append({"name": "Email", "icon": "📧", "enabled": False,
                              "status": f"❌ {e}", "details": [],
                              "settings_url": "/admin/config/notificationsettings/1/change/", "extra_links": []})

    # ── Telegram ──────────────────────────────────────────────────────────────
    try:
        ns = NotificationSettings.get()
        has_tg = bool(ns.telegram_bot_token and ns.telegram_chat_id)
        integrations.append({
            "name":    "Telegram Bot",
            "icon":    "📱",
            "enabled": ns.telegram_enabled and has_tg,
            "status":  (
                "✅ Увімкнено" if (ns.telegram_enabled and has_tg)
                else ("⚠️ Токен або Chat ID відсутній" if not has_tg else "⏸ Вимкнено")
            ),
            "details": [
                ("Bot Token",      "✅ Є" if ns.telegram_bot_token else "❌ Відсутній"),
                ("Chat ID",        ns.telegram_chat_id or "❌ Відсутній"),
                ("Нові замовл.",   "✅" if ns.new_order_telegram else "—"),
                ("Зміна статусу",  "✅" if ns.status_change_telegram else "—"),
            ],
            "settings_url": "/admin/config/notificationsettings/1/change/",
            "extra_links": [
                ("📱 Тест Telegram", "/admin/config/notificationsettings/1/test-telegram/"),
            ],
        })
    except Exception as e:
        integrations.append({"name": "Telegram", "icon": "📱", "enabled": False,
                              "status": f"❌ {e}", "details": [],
                              "settings_url": "/admin/config/notificationsettings/1/change/", "extra_links": []})

    context = {
        "title": "Інтеграції",
        "integrations": integrations,
        **admin.site.each_context(request),
    }
    return render(request, "admin/config/integrations.html", context)
