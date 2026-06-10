from django import forms
from django.contrib import admin
from django.forms import widgets
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from config.models import (
    ALL_MODULES, SystemSettings, DocumentSettings,
    NotificationSettings, ThemeSettings, BriefingSettings,
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
        ("🌐 Публічна інформація", {
            "fields": (
                "company_tagline",
                "company_email",
                "company_phone",
                "company_telegram",
            ),
            "description": "Відображається на лендінгу і в листах клієнтам.",
        }),
        ("🔗 Домен системи", {
            "fields": ("site_protocol", "site_domain"),
            "description": (
                "Домен підставляється в посилання email листів (password reset). "
                "Вкажіть точну адресу за якою система доступна ззовні. "
                "Після збереження — перевірте password reset лист."
            ),
        }),
        ("🔑 Ліцензія", {
            "fields": ("license_package", "license_key", "license_expires_at"),
            "description": "Ліцензійний ключ отримується після придбання на minerva-bi.com",
            "classes": ("collapse",),
        }),
    ]

    def get_form(self, request, obj=None, **kwargs):
        from django.forms import PasswordInput
        form = super().get_form(request, obj, **kwargs)
        if 'license_key' in form.base_fields:
            form.base_fields['license_key'].widget = PasswordInput(render_value=True)
        return form

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        from core.utils import is_minerva_admin, user_has_operation
        if is_minerva_admin(request.user):
            return True
        return (request.user.is_active and request.user.is_staff
                and user_has_operation(request.user, 'config', 'change'))

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
        ("⚙️ Відображення", {
            "fields": ("show_auto_pdf_panel",),
            "description": (
                "Керуй видимістю секції «Автоматичні документи» в картці замовлення. "
                "Вимкни якщо використовуєш тільки Word шаблони (модуль Документи)."
            ),
        }),
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        from core.utils import is_minerva_admin, user_has_operation
        if is_minerva_admin(request.user):
            return True
        return (request.user.is_active and request.user.is_staff
                and user_has_operation(request.user, 'config', 'change'))

    def has_delete_permission(self, request, obj=None):
        return False


class SourcesComboWidget(widgets.TextInput):
    """Text input + datalist with SalesSource slugs from DB."""
    def render(self, name, value, attrs=None, renderer=None):
        if attrs is None:
            attrs = {}
        list_id = f'cfg-sources-dl-{name}'
        attrs['list'] = list_id
        html = super().render(name, value, attrs, renderer)
        try:
            from sales.models import SalesSource
            slugs = list(SalesSource.objects.values_list('slug', flat=True).order_by('order', 'name'))
        except Exception:
            slugs = []
        options = ''.join(f'<option value="{s}">' for s in slugs)
        html += f'<datalist id="{list_id}">{options}</datalist>'
        from django.utils.safestring import mark_safe
        return mark_safe(html)


@admin.register(NotificationSettings)
class NotificationSettingsAdmin(admin.ModelAdmin):
    # Singleton: redirect changelist → change view for pk=1
    def changelist_view(self, request, extra_context=None):
        obj = NotificationSettings.get()
        return redirect(reverse("admin:config_notificationsettings_change", args=[obj.pk]))

    readonly_fields = (
        "alert_actions", "last_alert_sent",
        "new_order_preview",
        "dk_confirm_preview",
        "digest_actions", "digest_last_sent",
        "imap_actions", "imap_last_fetched",
        "weekly_digest_actions", "weekly_digest_last_sent",
        "monthly_digest_actions", "monthly_digest_last_sent",
    )

    def get_form(self, request, obj=None, **kwargs):
        from django.forms import PasswordInput
        form = super().get_form(request, obj, **kwargs)
        for field_name in ('email_host_password', 'telegram_bot_token'):
            if field_name in form.base_fields:
                form.base_fields[field_name].widget = PasswordInput(render_value=True)
        if 'order_confirm_notify_sources' in form.base_fields:
            form.base_fields['order_confirm_notify_sources'].widget = SourcesComboWidget()
        return form

    fieldsets = [
        ("📧 Email (SMTP)", {
            "fields": (
                "email_enabled",
                ("email_host", "email_port"),
                ("email_use_tls", "email_use_ssl"),
                "email_host_user", "email_host_password",
                "email_from", "email_to",
                "email_signature_template",
            ),
            "description": (
                "Gmail: host=smtp.gmail.com, port=587, TLS=✓. "
                "Потрібен App Password: Google Account → Security → "
                "2-Step Verification → App Passwords."
            ),
        }),
        ("📥 IMAP — читання пошти", {
            "fields": (
                "imap_enabled",
                ("imap_host", "imap_port"),
                "imap_use_ssl",
                "imap_user", "imap_password",
                ("imap_inbox_folder", "imap_sent_folder"),
                "imap_lookback_days",
                "imap_last_fetched",
                "imap_actions",
            ),
            "classes": ("collapse",),
            "description": (
                "Мінерва підключається до вашої пошти і додає листи в хронологію клієнтів CRM. "
                "ionos: host=imap.ionos.de, port=993, SSL=✓, Sent=INBOX.Sent. "
                "Листи ідентифікуються по email-адресі клієнта з CRM."
            ),
        }),
        ("📬 Нові замовлення", {
            "fields": (
                ("new_order_email", "new_order_telegram"),
                "new_order_preview",
            ),
            "description": (
                "Миттєве сповіщення при надходженні нового замовлення (з будь-якого джерела). "
                "Для DigiKey авто-підтверджень є окрема секція нижче."
            ),
        }),
        ("🤖 DigiKey авто-підтвердження", {
            "fields": (
                ("dk_auto_confirm_email", "dk_auto_confirm_telegram"),
                "dk_confirm_preview",
            ),
            "description": (
                "Окремий прапор від «Нові замовлення» — щоб уникнути дублювання. "
                "Рекомендація: увімкніть одне з двох. "
                "Нижче — живий приклад на основі реального замовлення з БД."
            ),
        }),
        ("⚙️ Вміст сповіщень (Нові замовлення + DigiKey)", {
            "fields": (
                ("notify_include_total", "notify_include_crm_count"),
                ("notify_include_deadline", "notify_include_stock_info"),
                ("notify_include_datasheet", "notify_include_images"),
            ),
            "description": (
                "Що включати у Telegram/Email сповіщення для нових замовлень і DigiKey авто-підтвердження. "
                "Вимкніть зайві поля щоб зменшити обсяг повідомлення. "
                "Фото товарів: надсилаються окремо після тексту — 1 фото або альбом (до 10 фото)."
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
        ("📦 Статус відправлення (посилки)", {
            "fields": (
                ("shipment_email", "shipment_telegram"),
                ("shipment_on_submitted", "shipment_on_label_ready",
                 "shipment_on_in_transit", "shipment_on_delivered",
                 "shipment_on_error", "shipment_on_cancelled"),
            ),
            "description": (
                "Сповіщення при зміні статусу посилки (відправлення). "
                "Спрацьовує автоматично при збереженні відправлення з новим статусом — "
                "вручну або через автотрекінг DHL/UPS."
            ),
        }),
        ("📧 Повідомлення клієнту про відправку", {
            "fields": (
                ("customer_notify_enabled", "customer_notify_auto"),
                "customer_notify_subject",
                "customer_notify_cc",
                "customer_notify_body",
                "customer_notify_body_noneu",
            ),
            "description": (
                "Кнопка «📧 Надіслати клієнту» на сторінці замовлення. "
                "ЄС-шаблон — для країн EU; не-ЄС-шаблон — для решти (містить попередження про мито). "
                "Автовизначення за полем 'Країна' замовлення. "
                "Змінні: {order_number} {customer_name} {tracking_number} "
                "{carrier} {shipped_date} {items} {ship_address}"
            ),
        }),
        ("📥 Підтвердження отримання замовлення", {
            "fields": (
                ("order_confirm_notify_enabled", "order_confirm_notify_auto"),
                "order_confirm_notify_sources",
                "order_confirm_notify_subject",
                "order_confirm_notify_cc",
                "order_confirm_notify_body",
            ),
            "description": (
                "Кнопка «📥 Підтвердження замовлення» на сторінці замовлення та/або авто-відправка при імпорті. "
                "Фільтр джерел дозволяє надсилати лише для певних джерел (напр. digikey). "
                "Змінні: {order_number} {customer_name} {order_date} {items}"
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
        ("📅 Щоденний звіт", {
            "fields": (
                "digest_enabled",
                ("digest_email", "digest_telegram"),
                ("digest_frequency", "digest_send_time"),
                ("digest_skip_weekends", "digest_skip_holidays"),
                ("digest_holiday_country",),
                ("digest_include_pending", "digest_include_overdue"),
                ("digest_include_new_orders", "digest_include_delivered"),
                "digest_include_stock",
                "digest_last_sent",
                "digest_actions",
            ),
            "description": (
                "Зведений звіт за обраним розкладом. "
                "Cron: <code>0 8 * * * docker-compose exec -T web python manage.py send_digest</code>\n\n"
                "<b>Що включає (Telegram приклад):</b>\n"
                "📊 Minerva — Щоденний звіт · 10.06.2026 08:00\n\n"
                "📦 Очікують відправки (3):\n"
                "  • 99705503 | UCL | ⏰ 24.06 (14 дн.)\n"
                "  • 99001234 | ACME Ltd | ⏰ 12.06 (2 дн.) ⚠️\n\n"
                "⏰ Прострочено (1):\n"
                "  • 98900001 | Bosch GmbH | 🔴 +3 дн.\n\n"
                "🆕 Нові замовлення (2):\n"
                "  • 99705503 | UCL — 97.97 USD\n\n"
                "✅ Доставлено (5)\n\n"
                "🔥 Критичний залишок (2):\n"
                "  • SKU123 — Resistor 100Ω | 5 шт | 0.3 міс 🔴"
            ),
        }),
        ("📅 Щотижневий звіт", {
            "fields": (
                "weekly_digest_enabled",
                ("weekly_digest_day", "weekly_digest_time"),
                "weekly_digest_last_sent",
                "weekly_digest_actions",
            ),
            "description": (
                "Надсилається раз на тиждень у обраний день. "
                "Cron запускати щодня — система сама перевіряє чи сьогодні потрібний день. "
                "Cron: <code>0 8 * * * docker-compose exec -T web python manage.py send_digest</code>\n\n"
                "<b>Що включає (додатково до щоденного):</b>\n"
                "🚚 Відправлень за тиждень: 12\n\n"
                "📦 Топ товарів (Тижневий звіт):\n"
                "  1. AN220207-001 Resistor | 48 шт\n"
                "  2. BC547 Transistor | 30 шт\n"
                "  3. LM358 Op-Amp | 20 шт\n"
                "  ... (до 5 позицій)"
            ),
        }),
        ("📅 Місячний звіт", {
            "fields": (
                "monthly_digest_enabled",
                ("monthly_digest_day", "monthly_digest_time"),
                "monthly_digest_last_sent",
                "monthly_digest_actions",
            ),
            "description": (
                "Надсилається раз на місяць у обраний день. "
                "Cron запускати щодня — система перевіряє чи це потрібний день місяця. "
                "Cron: <code>0 8 * * * docker-compose exec -T web python manage.py send_digest</code>\n\n"
                "<b>Що включає (додатково):</b>\n"
                "💰 Виручка місяця: 4 820 ▲12.3%\n"
                "   Попередній місяць: 4 292 | Замовлень: 37\n\n"
                "🚚 Відправлень за місяць: 34\n\n"
                "📦 Топ товарів (Місячний звіт) — до 10 позицій"
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
            path(
                "<int:pk>/test-new-order/",
                self.admin_site.admin_view(self._test_new_order),
                name="config_notificationsettings_test_new_order",
            ),
            path(
                "<int:pk>/test-dk-confirm/",
                self.admin_site.admin_view(self._test_dk_confirm),
                name="config_notificationsettings_test_dk_confirm",
            ),
            path(
                "<int:pk>/send-digest/",
                self.admin_site.admin_view(self._send_digest),
                name="config_notificationsettings_send_digest",
            ),
            path(
                "<int:pk>/fetch-emails/",
                self.admin_site.admin_view(self._fetch_emails),
                name="config_notificationsettings_fetch_emails",
            ),
            path(
                "<int:pk>/send-weekly-digest/",
                self.admin_site.admin_view(self._send_weekly_digest),
                name="config_notificationsettings_send_weekly_digest",
            ),
            path(
                "<int:pk>/send-monthly-digest/",
                self.admin_site.admin_view(self._send_monthly_digest),
                name="config_notificationsettings_send_monthly_digest",
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

    def _test_new_order(self, request, pk):
        from django.contrib import messages
        from sales.models import SalesOrder
        from dashboard.notifications import notify_new_order
        order_ref = request.GET.get('order_ref', '').strip()
        if order_ref:
            order = SalesOrder.objects.filter(order_number=order_ref).first()
            if not order:
                messages.error(request, f"❌ Замовлення «{order_ref}» не знайдено.")
                return redirect(reverse("admin:config_notificationsettings_change", args=[1]))
        else:
            order = SalesOrder.objects.order_by('-order_date', '-pk').first()
        if not order:
            messages.warning(request, "⚠️ Немає замовлень для тесту.")
            return redirect(reverse("admin:config_notificationsettings_change", args=[1]))
        try:
            notify_new_order(order, is_test=True)
            messages.success(
                request,
                f"✅ Тестове сповіщення надіслано на основі замовлення {order.order_number}."
            )
        except Exception as e:
            messages.error(request, f"❌ Помилка: {e}")
        return redirect(reverse("admin:config_notificationsettings_change", args=[1]))

    # ── Telegram preview helpers ───────────────────────────────────────────────

    @staticmethod
    def _tg_preview_card(html_lines: list, order_number: str) -> str:
        """Render Telegram-style preview card from a list of HTML line strings."""
        from django.utils.safestring import mark_safe
        from django.utils.html import escape
        body = mark_safe('<br>'.join(html_lines))
        return (
            f'<div style="font-size:11px;color:var(--text-dim,#607d8b);margin-bottom:6px">'
            f'Приклад на основі замовлення <b>{escape(order_number)}</b></div>'
            f'<div style="background:#17212b;color:#e8f0f7;border-radius:12px;'
            f'padding:16px 20px;font-family:system-ui,sans-serif;font-size:13px;'
            f'line-height:1.8;max-width:520px;border:1px solid #2b5278;'
            f'margin-bottom:12px">{body}</div>'
        )

    def _build_order_preview_lines(self, order, mode='new_order', ns=None):
        """Build HTML lines for Telegram preview from a real order."""
        from django.utils import timezone
        from django.utils.html import escape
        from django.db.models import Sum

        _CNAMES = {
            'AT':'Австрія','BE':'Бельгія','BG':'Болгарія','CH':'Швейцарія','CY':'Кіпр',
            'CZ':'Чехія','DE':'Німеччина','DK':'Данія','EE':'Естонія','ES':'Іспанія',
            'FI':'Фінляндія','FR':'Франція','GB':'Велика Британія','GR':'Греція',
            'HR':'Хорватія','HU':'Угорщина','IE':'Ірландія','IT':'Італія','LT':'Литва',
            'LU':'Люксембург','LV':'Латвія','MT':'Мальта','NL':'Нідерланди','NO':'Норвегія',
            'PL':'Польща','PT':'Португалія','RO':'Румунія','SE':'Швеція','SI':'Словенія',
            'SK':'Словаччина','UA':'Україна','US':'США','CA':'Канада','AU':'Австралія',
            'JP':'Японія','CN':'Китай','TR':'Туреччина',
        }

        try:
            from config.models import SystemSettings
            cname = SystemSettings.get().company_name or 'Minerva'
        except Exception:
            cname = 'Minerva'

        _g = lambda f, d=True: getattr(ns, f, d) if ns else d
        _inc_crm      = _g('notify_include_crm_count')
        _inc_deadline = _g('notify_include_deadline')
        _inc_total    = _g('notify_include_total')
        _inc_stock    = _g('notify_include_stock_info')
        _inc_ds       = _g('notify_include_datasheet')
        _inc_img      = _g('notify_include_images')

        client      = escape(order.client or order.email or '—')
        cc          = (getattr(order, 'addr_country', '') or '').strip().upper()
        destination = escape(_CNAMES.get(cc, cc))
        now_str     = timezone.now().strftime('%d.%m.%Y %H:%M')

        # CRM
        crm_orders = None
        if _inc_crm:
            try:
                cust = order.crm_customer
                if cust:
                    crm_orders = cust.total_orders()
            except Exception:
                pass

        # Deadline
        deadline_str = days_left_str = ''
        days_left = None
        if _inc_deadline and order.shipping_deadline:
            deadline_str = order.shipping_deadline.strftime('%d.%m.%Y')
            days_left = (order.shipping_deadline - timezone.now().date()).days
            if days_left > 1:
                days_left_str = f'{days_left} дн.'
            elif days_left == 1:
                days_left_str = 'Завтра ⚠️'
            elif days_left == 0:
                days_left_str = 'Сьогодні! ⚠️'
            else:
                days_left_str = f'Прострочено ({-days_left} дн.) 🔴'

        # Total
        total_str = ''
        if _inc_total:
            try:
                t = order.lines.aggregate(s=Sum('total_price'))['s']
                if t:
                    total_str = f'{float(t):.2f} {order.currency or ""}'.strip()
            except Exception:
                pass

        L = []
        L.append(f'🏛️ <b style="color:#e8f0f7">{escape(cname)}</b>')
        if mode == 'dk_confirm':
            L.append(f'✅ <b>DigiKey: авто-підтверджено</b> · <i style="color:#8ab4d1">{now_str}</i>')
        else:
            L.append(f'🆕 <b>Нове замовлення</b> · <i style="color:#8ab4d1">{now_str}</i>')
        L.append('')
        L.append(
            f'📋 <code style="background:#1e3a5f;padding:1px 5px;border-radius:4px">'
            f'{escape(order.order_number)}</code> · {escape(order.source or "digikey")}'
        )
        L.append(f'👤 <b>{client}</b>')
        if _inc_crm and crm_orders is not None:
            L.append(f'&nbsp;&nbsp;&nbsp;📊 Замовлень всього: <b>{crm_orders}</b>')
        if destination:
            L.append(f'📍 {destination}')
        if deadline_str:
            dl_warn = ' ⚠️' if days_left is not None and days_left <= 2 else ''
            L.append(
                f'📦 Дедлайн: <b>{deadline_str}</b>'
                f' <span style="color:#8ab4d1">({escape(days_left_str)})</span>{dl_warn}'
            )
        if total_str:
            L.append(f'💰 <b style="color:#7dd47d">{escape(total_str)}</b>')
        if mode == 'dk_confirm':
            L.append(f'🤖 <i style="color:#8ab4d1">Підтверджено автоматично (always)</i>')

        # Products
        try:
            order_lines = list(order.lines.select_related('product').all())
        except Exception:
            order_lines = []

        if order_lines:
            L.append('')
            L.append('📦 <b>Товари:</b>')
            for ol in order_lines:
                p   = ol.product
                sku = escape((p.sku if p else getattr(ol, 'sku_raw', None)) or '—')
                qty = ol.qty or 0
                try:
                    qty_str = str(int(qty)) if float(qty) == int(float(qty)) else str(qty)
                except Exception:
                    qty_str = str(qty)
                curr = escape(ol.currency or getattr(order, 'currency', '') or '')

                # stock for new_order mode
                stock_icon = '•'
                if mode == 'new_order' and _inc_stock and p:
                    try:
                        from django.db.models import Sum as _S
                        from inventory.models import InventoryTransaction as _IT
                        stock = int(_IT.objects.filter(product=p).aggregate(t=_S('qty'))['t'] or 0)
                        stock_icon = '✅' if stock >= (ol.qty or 0) else '❌'
                    except Exception:
                        pass

                L.append(
                    f'{stock_icon} <code style="background:#1e3a5f;padding:1px 5px;'
                    f'border-radius:4px">{sku}</code>'
                )
                L.append(f'&nbsp;&nbsp;&nbsp;📦 × <b>{qty_str} шт</b>')
                if ol.unit_price:
                    L.append(
                        f'&nbsp;&nbsp;&nbsp;💵 <span style="color:#8ab4d1">'
                        f'{float(ol.unit_price):.2f} {curr}/шт</span>'
                    )
                if mode == 'new_order' and _inc_stock and p:
                    try:
                        from inventory.models import InventoryTransaction as _IT2
                        from django.db.models import Sum as _S2
                        stk = int(_IT2.objects.filter(product=p).aggregate(t=_S2('qty'))['t'] or 0)
                        icon2 = '✅' if stk >= (ol.qty or 0) else '❌'
                        L.append(f'&nbsp;&nbsp;&nbsp;🏪 склад: <b>{stk} шт</b> {icon2}')
                    except Exception:
                        pass
                if _inc_ds and p and getattr(p, 'datasheet_url', ''):
                    L.append(
                        f'&nbsp;&nbsp;&nbsp;📄 <a href="{escape(p.datasheet_url)}" '
                        f'style="color:#6ab4f5">Datasheet</a>'
                    )
        if _inc_img and order_lines:
            imgs = [
                p.image_url or ''
                for ol in order_lines
                for p in [ol.product]
                if p and getattr(p, 'image_url', '')
            ]
            if imgs:
                L.append(
                    f'<br><span style="color:#607d8b;font-size:11px">'
                    f'🖼️ {len(imgs)} фото надійде окремим альбомом</span>'
                )
        return L

    def _test_dk_confirm(self, request, pk):
        from django.contrib import messages
        from sales.models import SalesOrder
        from dashboard.notifications import notify_digikey_auto_confirmed
        order_ref = request.GET.get('order_ref', '').strip()
        if order_ref:
            order = SalesOrder.objects.filter(order_number=order_ref).first()
            if not order:
                messages.error(request, f"❌ Замовлення «{order_ref}» не знайдено.")
                return redirect(reverse("admin:config_notificationsettings_change", args=[1]))
        else:
            order = (
                SalesOrder.objects
                .filter(source__icontains='digikey')
                .order_by('-order_date', '-pk')
                .first()
            ) or SalesOrder.objects.order_by('-order_date', '-pk').first()
        if not order:
            messages.warning(request, "⚠️ Немає замовлень для тесту.")
            return redirect(reverse("admin:config_notificationsettings_change", args=[1]))
        try:
            notify_digikey_auto_confirmed(order, mode='always')
            messages.success(
                request,
                f"✅ Тест DigiKey авто-підтвердження надіслано · {order.order_number}."
            )
        except Exception as e:
            messages.error(request, f"❌ Помилка: {e}")
        return redirect(reverse("admin:config_notificationsettings_change", args=[1]))

    def _preview_widget(self, obj, mode, input_id, url_suffix, btn_label, btn_color='#0088cc'):
        """Render preview card + test button for a given mode."""
        from django.utils.safestring import mark_safe
        from django.utils.html import escape
        from sales.models import SalesOrder

        if mode == 'dk_confirm':
            order = (
                SalesOrder.objects
                .prefetch_related('lines__product')
                .filter(source__icontains='digikey')
                .order_by('-order_date', '-pk')
                .first()
            ) or SalesOrder.objects.prefetch_related('lines__product').order_by('-order_date', '-pk').first()
        else:
            order = (
                SalesOrder.objects
                .prefetch_related('lines__product')
                .order_by('-order_date', '-pk')
                .first()
            )

        if not order:
            no_data = '<span style="color:var(--text-dim)">Немає замовлень у БД для прикладу</span>'
            return mark_safe(no_data)

        lines = self._build_order_preview_lines(order, mode=mode, ns=obj)
        card  = self._tg_preview_card(lines, order.order_number)

        placeholder = (
            '№ DigiKey замовлення (або порожньо = останнє DigiKey)'
            if mode == 'dk_confirm'
            else '№ замовлення (або порожньо = останнє)'
        )

        controls = (
            f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
            f'<input type="text" id="{input_id}"'
            f' placeholder="{escape(placeholder)}"'
            f' style="padding:7px 10px;border:1px solid var(--border-strong,#2a3f52);'
            f'border-radius:6px;background:var(--bg-input,#141f2b);'
            f'color:var(--text,#c9d8e4);font-size:13px;width:320px">'
            f'<button type="button"'
            f' onclick="var v=document.getElementById(\'{input_id}\').value.trim();'
            f'window.location.href=\'../{url_suffix}/\'+(v?\'?order_ref=\'+encodeURIComponent(v):\'\');"'
            f' style="padding:8px 18px;background:{btn_color};color:#fff;border:none;'
            f'border-radius:6px;font-weight:600;font-size:13px;cursor:pointer">'
            f'{btn_label}</button>'
            f'</div>'
        )

        return mark_safe(card + controls)

    def new_order_preview(self, obj):
        if not obj or not obj.pk:
            return "—"
        return self._preview_widget(
            obj, mode='new_order',
            input_id='mv_no_order_ref',
            url_suffix='test-new-order',
            btn_label='📱 Надіслати тест',
            btn_color='#2e7d32',
        )
    new_order_preview.short_description = "Приклад повідомлення"

    def dk_confirm_preview(self, obj):
        if not obj or not obj.pk:
            return "—"
        return self._preview_widget(
            obj, mode='dk_confirm',
            input_id='mv_dk_order_ref',
            url_suffix='test-dk-confirm',
            btn_label='📱 Надіслати тест',
            btn_color='#0088cc',
        )
    dk_confirm_preview.short_description = "Приклад повідомлення"

    def _send_digest(self, request, pk):
        from django.contrib import messages
        from dashboard.digest import send_digest
        result = send_digest(force=True)
        if result.get("sent"):
            parts = []
            if result.get("email", {}).get("sent"):
                parts.append("Email ✓")
            if result.get("telegram", {}).get("sent"):
                parts.append("Telegram ✓")
            for ch, info in result.items():
                if isinstance(info, dict) and info.get("error"):
                    parts.append(f"{ch}: {info['error']}")
            messages.success(request, f"✅ Звіт надіслано: {', '.join(parts)}")
        else:
            err = result.get("reason") or result.get("error") or "?"
            messages.error(request, f"❌ Помилка: {err}")
        return redirect(reverse("admin:config_notificationsettings_change", args=[1]))

    def digest_actions(self, obj):
        if not obj or not obj.pk:
            return "—"
        last = ""
        if obj.digest_last_sent:
            from django.utils import timezone as _tz
            local_dt = _tz.localtime(obj.digest_last_sent)
            last = (
                f' <span style="color:var(--text-dim);font-size:11px;margin-left:12px">'
                f'Останній: {local_dt.strftime("%d.%m.%Y %H:%M")}</span>'
            )
        return format_html(
            '<a href="../send-digest/" style="display:inline-block;padding:8px 18px;'
            'background:#37474f;color:#fff;border-radius:6px;'
            'text-decoration:none;font-weight:600;font-size:13px">'
            '📊 Надіслати звіт зараз</a>{}',
            format_html(last),
        )
    digest_actions.short_description = "Тест щоденного звіту"

    def _fetch_emails(self, request, pk):
        from django.contrib import messages
        from django.core.management import call_command
        from django.utils import timezone
        from io import StringIO
        out = StringIO()
        try:
            call_command('fetch_emails', stdout=out)
            ns = NotificationSettings.get()
            ns.imap_last_fetched = timezone.now()
            ns.save(update_fields=['imap_last_fetched'])
            messages.success(request, f"✅ Пошту оновлено: {out.getvalue().strip() or 'готово'}")
        except Exception as e:
            messages.error(request, f"❌ Помилка: {e}")
        return redirect(reverse("admin:config_notificationsettings_change", args=[1]))

    def _send_weekly_digest(self, request, pk):
        from django.contrib import messages
        from dashboard.digest import send_digest
        result = send_digest(force=True, period='weekly')
        if result.get("sent"):
            messages.success(request, "✅ Тижневий звіт надіслано!")
        else:
            messages.error(request, f"❌ {result.get('reason') or result.get('error') or '?'}")
        return redirect(reverse("admin:config_notificationsettings_change", args=[1]))

    def _send_monthly_digest(self, request, pk):
        from django.contrib import messages
        from dashboard.digest import send_digest
        result = send_digest(force=True, period='monthly')
        if result.get("sent"):
            messages.success(request, "✅ Місячний звіт надіслано!")
        else:
            messages.error(request, f"❌ {result.get('reason') or result.get('error') or '?'}")
        return redirect(reverse("admin:config_notificationsettings_change", args=[1]))

    def imap_actions(self, obj):
        if not obj or not obj.pk:
            return "—"
        return format_html(
            '<a href="../fetch-emails/" style="display:inline-block;padding:8px 18px;'
            'background:#00796b;color:#fff;border-radius:6px;'
            'text-decoration:none;font-weight:600;font-size:13px">'
            '🔄 Оновити пошту зараз</a>'
        )
    imap_actions.short_description = "Дії"

    def weekly_digest_actions(self, obj):
        if not obj or not obj.pk:
            return "—"
        return format_html(
            '<a href="../send-weekly-digest/" style="display:inline-block;padding:8px 18px;'
            'background:#37474f;color:#fff;border-radius:6px;'
            'text-decoration:none;font-weight:600;font-size:13px">'
            '📊 Надіслати тижневий звіт зараз</a>'
        )
    weekly_digest_actions.short_description = "Тест тижневого звіту"

    def monthly_digest_actions(self, obj):
        if not obj or not obj.pk:
            return "—"
        return format_html(
            '<a href="../send-monthly-digest/" style="display:inline-block;padding:8px 18px;'
            'background:#37474f;color:#fff;border-radius:6px;'
            'text-decoration:none;font-weight:600;font-size:13px">'
            '📊 Надіслати місячний звіт зараз</a>'
        )
    monthly_digest_actions.short_description = "Тест місячного звіту"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        from core.utils import is_minerva_admin, user_has_operation
        if is_minerva_admin(request.user):
            return True
        return (request.user.is_active and request.user.is_staff
                and user_has_operation(request.user, 'config', 'change'))

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

    def has_change_permission(self, request, obj=None):
        from core.utils import is_minerva_admin, user_has_operation
        if is_minerva_admin(request.user):
            return True
        return (request.user.is_active and request.user.is_staff
                and user_has_operation(request.user, 'config', 'change'))

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


# ── BriefingSettingsAdmin ─────────────────────────────────────────────────────

@admin.register(BriefingSettings)
class BriefingSettingsAdmin(admin.ModelAdmin):
    readonly_fields = ("briefing_actions",)

    fieldsets = [
        ("⏰ Розклад", {
            "fields": ("enabled", "send_time"),
            "description": (
                "Налаштуйте cron на відповідний час: "
                "<code>0 8 * * * docker-compose exec -T web python manage.py morning_briefing</code>"
            ),
        }),
        ("📋 Що включати у брифінг", {
            "fields": (
                "include_orders", "include_revenue",
                "include_overdue", "include_reminders",
                "include_stock_alerts", "include_new_emails",
            ),
            "description": "AI може включати додаткову важливу інформацію на свій розсуд.",
        }),
        ("🤖 Інструкції для AI", {
            "fields": ("custom_instructions",),
            "description": (
                "Необов'язково. Задайте акценти або стиль: мова, тон, додаткові метрики. "
                "AI завжди може включити критично важливе навіть без вказівки."
            ),
        }),
        ("🚀 Дії", {
            "fields": ("briefing_actions",),
        }),
    ]

    def changelist_view(self, request, extra_context=None):
        obj = BriefingSettings.get()
        return redirect(reverse("admin:config_briefingsettings_change", args=[obj.pk]))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        from core.utils import is_minerva_admin, user_has_operation
        if is_minerva_admin(request.user):
            return True
        return (request.user.is_active and request.user.is_staff
                and user_has_operation(request.user, 'config', 'change'))

    def has_delete_permission(self, request, obj=None):
        return False

    def briefing_actions(self, obj):
        if not obj or not obj.pk:
            return "—"
        return format_html(
            '<a href="../send-briefing/" style="display:inline-block;padding:8px 18px;'
            'background:#1976d2;color:#fff;border-radius:6px;'
            'text-decoration:none;font-weight:600;font-size:13px">'
            '📤 Тестова відправка в Telegram</a>'
        )
    briefing_actions.short_description = "Дії"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:pk>/send-briefing/",
                self.admin_site.admin_view(self._send_briefing),
                name="config_briefingsettings_send_briefing",
            ),
        ]
        return custom + urls

    def _send_briefing(self, request, pk):
        from django.contrib import messages
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        try:
            call_command('morning_briefing', stdout=out)
            result = out.getvalue().strip()
            messages.success(request, f"✅ Брифінг надіслано. {result}")
        except Exception as e:
            messages.error(request, f"❌ Помилка: {e}")
        return redirect(reverse("admin:config_briefingsettings_change", args=[1]))
