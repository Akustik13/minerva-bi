import json
from django import forms
from django.contrib import admin
from django.contrib.auth.models import User
from django.utils.html import format_html, mark_safe

from .models import AuditLog, UserProfile, ModuleBundle, ModuleRegistry, TenantAccount


# ── UserProfile custom form ───────────────────────────────────────────────────

class UserProfileForm(forms.ModelForm):
    """Replace raw JSON allowed_modules with friendly checkboxes."""

    modules_override = forms.ModelMultipleChoiceField(
        queryset=ModuleRegistry.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label='Ручне перевизначення модулів',
        help_text=(
            'Заповнюйте лише якщо пакет не підходить. '
            'Порожньо = використовується пакет або роль.'
        ),
    )

    class Meta:
        model = UserProfile
        fields = '__all__'
        exclude = ['allowed_modules']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Set querysets at request time (not at class definition / import time)
        self.fields['bundle'].queryset = ModuleBundle.objects.all().order_by('name')
        self.fields['bundle'].empty_label = '— без пакету (використовувати роль) —'
        self.fields['modules_override'].queryset = ModuleRegistry.objects.order_by('order', 'name')
        if self.instance.pk and self.instance.allowed_modules:
            self.fields['modules_override'].initial = (
                ModuleRegistry.objects.filter(app_label__in=self.instance.allowed_modules)
            )

    def save(self, commit=True):
        instance = super().save(commit=False)
        selected = self.cleaned_data.get('modules_override', [])
        instance.allowed_modules = [m.app_label for m in selected]
        if commit:
            instance.save()
            self.save_m2m()
        return instance


# ── AuditLog ──────────────────────────────────────────────────────────────────

@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display    = ('timestamp', 'user', 'action_badge', 'object_repr', 'ip_address')
    list_filter     = ('action', 'user')
    search_fields   = ('user__username', 'object_repr', 'ip_address')
    readonly_fields = (
        'user', 'action', 'content_type', 'object_id', 'object_repr',
        'ip_address', 'extra', 'timestamp',
    )
    date_hierarchy  = 'timestamp'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    @admin.display(description='Дія')
    def action_badge(self, obj):
        colors = {
            'login':  '#3fb950', 'logout': '#9aafbe',
            'create': '#58a6ff', 'update': '#e3b341',
            'delete': '#f85149', 'view':   '#607d8b',
            'export': '#c9a84c', 'import': '#2196f3',
        }
        color = colors.get(obj.action, '#9aafbe')
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:4px;font-size:11px;font-weight:600">{}</span>',
            color, obj.get_action_display(),
        )


# ── ModuleBundle ──────────────────────────────────────────────────────────────

@admin.register(ModuleBundle)
class ModuleBundleAdmin(admin.ModelAdmin):
    list_display      = ('name_badge', 'modules_summary', 'is_system', 'description')
    list_filter       = ('is_system',)
    search_fields     = ('name', 'description')
    filter_horizontal = ('modules',)
    fieldsets = (
        (None, {
            'fields': ('name', 'color', 'description', 'is_system'),
        }),
        ('Модулі пакету', {
            'fields': ('modules',),
            'description': '🔒 Core-модулі (core, config, auth) завжди додаються автоматично',
        }),
    )

    def get_form(self, request, obj=None, **kwargs):
        from config.admin import ColorPickerWidget
        form = super().get_form(request, obj, **kwargs)
        if 'color' in form.base_fields:
            form.base_fields['color'].widget = ColorPickerWidget()
        return form

    def has_delete_permission(self, request, obj=None):
        if obj and obj.is_system:
            return False
        return super().has_delete_permission(request, obj)

    @admin.display(description='Назва')
    def name_badge(self, obj):
        return format_html(
            '<span style="background:{};color:#fff;padding:3px 12px;'
            'border-radius:4px;font-weight:600">{}</span>',
            obj.color or '#58a6ff', obj.name,
        )

    @admin.display(description='Склад пакету')
    def modules_summary(self, obj):
        mods = obj.modules.order_by('order').values_list('name', 'tier')
        tier_colors = {'core': '#f85149', 'standard': '#58a6ff', 'premium': '#c9a84c'}
        badges = mark_safe('')
        for name, tier in mods:
            badges += format_html(
                '<span style="background:{};color:#fff;padding:1px 6px;'
                'border-radius:3px;font-size:11px;margin:1px 2px;display:inline-block">{}</span>',
                tier_colors.get(tier, '#9aafbe'), name,
            )
        return badges or '—'


# ── UserProfile ───────────────────────────────────────────────────────────────

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    form          = UserProfileForm
    list_display  = ('user', 'role_badge', 'effective_access', 'ai_enabled')
    list_filter   = ('role', 'bundle', 'ai_enabled')
    search_fields = ('user__username', 'user__email', 'user__first_name', 'user__last_name')
    readonly_fields = ('effective_access_detail',)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'bundle':
            kwargs['queryset']    = ModuleBundle.objects.all().order_by('name')
            kwargs['empty_label'] = '— без пакету (використовувати роль) —'
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        from core.utils import ROLE_PERMISSIONS
        extra = extra_context or {}

        # app_label → pk (string) — needed because checkbox values are PKs
        module_pk_map = {
            m.app_label: str(m.pk)
            for m in ModuleRegistry.objects.all()
        }

        # role → [app_labels] or '__all__'
        role_modules = {
            role: (perms['modules'] if isinstance(perms.get('modules'), list) else '__all__')
            for role, perms in ROLE_PERMISSIONS.items()
        }

        # bundle pk → [app_labels]
        bundle_modules = {
            str(b.pk): b.get_module_labels()
            for b in ModuleBundle.objects.prefetch_related('modules')
        }

        extra['mv_module_pk_map']  = json.dumps(module_pk_map,  ensure_ascii=False)
        extra['mv_role_modules']   = json.dumps(role_modules,   ensure_ascii=False)
        extra['mv_bundle_modules'] = json.dumps(bundle_modules, ensure_ascii=False)
        extra['mv_changelist_url'] = '../'
        return super().changeform_view(request, object_id, form_url, extra)

    fieldsets = (
        ('👤 Користувач', {
            'fields': ('user', 'role', 'notes'),
        }),
        ('🧩 Доступ до модулів', {
            'fields': ('effective_access_detail', 'bundle', 'modules_override'),
            'description': (
                '<strong>Як визначається доступ (за пріоритетом):</strong><br>'
                '1️⃣ <strong>Ручне перевизначення</strong> — якщо відмічено нижче<br>'
                '2️⃣ <strong>Пакет</strong> — якщо вибрано пакет<br>'
                '3️⃣ <strong>Роль</strong> — автоматично за роллю користувача'
            ),
        }),
        ('🔒 Дозволи (перевизначення)', {
            'fields': ('can_delete', 'can_export', 'can_import', 'can_view_audit'),
            'classes': ('collapse',),
            'description': (
                'Порожньо = використовуються дефолти ролі. '
                'Явне Так/Ні — перевизначає роль.'
            ),
        }),
        ('🤖 AI-асистент', {
            'fields': ('ai_enabled', 'ai_model', 'ai_temperature', 'ai_system_prompt'),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Роль')
    def role_badge(self, obj):
        colors = {
            'superadmin': '#f85149', 'admin': '#c9a84c',
            'manager': '#58a6ff',    'warehouse': '#3fb950',
            'accountant': '#2196f3', 'ai': '#9c27b0',
        }
        color = colors.get(obj.role, '#9aafbe')
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:4px;font-size:11px;font-weight:600">{}</span>',
            color, obj.get_role_display(),
        )

    @admin.display(description='Активний доступ')
    def effective_access(self, obj):
        modules = obj.get_allowed_modules()
        if modules == '__all__':
            return format_html(
                '<span style="color:#3fb950;font-weight:600">✅ Всі модулі</span>'
            )
        source = '📋' if obj.allowed_modules else ('🧩' if obj.bundle_id else '👤')
        return format_html(
            '{} <span style="color:var(--text-muted,#9aafbe)">{} модулів</span>',
            source, len(modules),
        )

    @admin.display(description='Ефективний доступ (що бачить користувач)')
    def effective_access_detail(self, obj):
        if not obj.pk:
            return '— збережіть профіль щоб побачити —'

        modules = obj.get_allowed_modules()

        # Determine source
        if obj.allowed_modules:
            source_html = format_html(
                '<span style="background:#e3b341;color:#000;padding:2px 8px;'
                'border-radius:4px;font-size:11px">1️⃣ Ручне перевизначення</span>'
            )
        elif obj.bundle_id:
            source_html = format_html(
                '<span style="background:{};color:#fff;padding:2px 8px;'
                'border-radius:4px;font-size:11px">2️⃣ Пакет: {}</span>',
                obj.bundle.color or '#58a6ff', obj.bundle.name,
            )
        else:
            source_html = format_html(
                '<span style="background:#607d8b;color:#fff;padding:2px 8px;'
                'border-radius:4px;font-size:11px">3️⃣ Роль: {}</span>',
                obj.get_role_display(),
            )

        if modules == '__all__':
            return format_html(
                '{}&nbsp; <strong style="color:#3fb950">Доступ до всіх модулів</strong>',
                source_html,
            )

        tier_colors = {'core': '#f85149', 'standard': '#58a6ff', 'premium': '#c9a84c'}
        try:
            reg = {
                r.app_label: r
                for r in ModuleRegistry.objects.filter(app_label__in=modules)
            }
        except Exception:
            reg = {}

        badges = mark_safe('')
        for app_label in sorted(modules):
            r = reg.get(app_label)
            name  = r.name if r else app_label
            color = tier_colors.get(r.tier if r else '', '#9aafbe')
            badges += format_html(
                '<span style="background:{};color:#fff;padding:2px 8px;'
                'border-radius:3px;font-size:11px;margin:2px 3px;display:inline-block">{}</span>',
                color, name,
            )

        return format_html('{}&nbsp; {}', source_html, badges)


# ── ModuleRegistry ────────────────────────────────────────────────────────────

@admin.register(ModuleRegistry)
class ModuleRegistryAdmin(admin.ModelAdmin):
    list_display  = ('name', 'app_label', 'tier_badge', 'is_active', 'order')
    list_filter   = ('tier', 'is_active')
    list_editable = ('is_active', 'order')
    search_fields = ('app_label', 'name')
    ordering      = ('order', 'app_label')

    def has_delete_permission(self, request, obj=None):
        if obj and obj.tier == ModuleRegistry.Tier.CORE:
            return False
        return request.user.is_superuser

    @admin.display(description='Тир')
    def tier_badge(self, obj):
        colors = {
            'core':     '#f85149',
            'standard': '#58a6ff',
            'premium':  '#c9a84c',
        }
        color = colors.get(obj.tier, '#9aafbe')
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:4px;font-size:11px">{}</span>',
            color, obj.get_tier_display(),
        )


# ── TenantAccount ──────────────────────────────────────────────────────────────

@admin.register(TenantAccount)
class TenantAccountAdmin(admin.ModelAdmin):
    """Vendor dashboard — visible to superadmin only."""

    list_display  = (
        'company_name', 'owner_email', 'package_badge',
        'status_badge', 'days_badge', 'modules_count',
        'registered_at', 'actions_col',
    )
    list_filter   = ('status', 'package', 'company_country', 'email_verified')
    search_fields = ('company_name', 'owner_email', 'owner_name')
    readonly_fields = (
        'registered_at', 'activated_at', 'last_login_at',
        'verification_token', 'verification_sent_at',
        'active_modules', 'vendor_summary',
    )
    ordering = ['-registered_at']

    fieldsets = (
        ('🏢 Клієнт', {
            'fields': (
                'company_name', 'company_country', 'contact_phone',
                'owner_name', 'owner_email', 'owner_user',
            ),
        }),
        ('📦 Пакет і статус', {
            'fields': ('package', 'status', 'email_verified', 'trial_ends_at', 'paid_until'),
            'description': (
                'Після оплати: змінити status → active, '
                'встановити paid_until на дату закінчення.'
            ),
        }),
        ('🧩 Активні модулі', {
            'fields': ('active_modules',),
            'description': 'Оновлюється автоматично при зміні пакету.',
            'classes': ('collapse',),
        }),
        ('📊 Статистика', {
            'fields': ('vendor_summary',),
        }),
        ('🗒️ Нотатки', {
            'fields': ('notes',),
        }),
        ('📋 Службові', {
            'fields': ('registered_at', 'activated_at', 'last_login_at', 'verification_sent_at'),
            'classes': ('collapse',),
        }),
    )

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def save_model(self, request, obj, form, change):
        if change and 'package' in (form.changed_data or []):
            super().save_model(request, obj, form, change)
            obj.activate_modules()
        else:
            super().save_model(request, obj, form, change)
        if change and 'status' in (form.changed_data or []):
            AuditLog.log(
                action='settings',
                user=request.user,
                module='core',
                model_name='TenantAccount',
                object_id=obj.pk,
                object_repr=str(obj),
                extra={'event': 'status_changed', 'company': obj.company_name, 'new_status': obj.status},
                request=request,
            )

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        qs = TenantAccount.objects.all()
        extra_context.update({
            'active_count':  qs.filter(status='active').count(),
            'trial_count':   qs.filter(status='trial').count(),
            'expired_count': qs.filter(status='expired').count(),
            'pending_count': qs.filter(status='pending').count(),
            'total_count':   qs.count(),
        })
        return super().changelist_view(request, extra_context)

    # ── List display columns ───────────────────────────────

    @admin.display(description='Пакет')
    def package_badge(self, obj):
        COLORS = {
            'free':     '#6c757d', 'starter':  '#007bff',
            'business': '#fd7e14', 'custom':   '#6610f2',
        }
        c = COLORS.get(obj.package, '#333')
        label = obj.get_package_display().split(' —')[0]
        return format_html(
            '<span style="padding:2px 8px;border-radius:10px;'
            'font-size:11px;background:{};color:#fff">{}</span>',
            c, label)

    @admin.display(description='Статус')
    def status_badge(self, obj):
        COLORS = {
            'pending':   ('#ffc107', '#000'),
            'trial':     ('#17a2b8', '#fff'),
            'active':    ('#28a745', '#fff'),
            'suspended': ('#fd7e14', '#fff'),
            'expired':   ('#dc3545', '#fff'),
            'cancelled': ('#6c757d', '#fff'),
        }
        bg, fg = COLORS.get(obj.status, ('#333', '#fff'))
        return format_html(
            '<span style="padding:2px 8px;border-radius:10px;'
            'font-size:11px;background:{};color:{}">{}</span>',
            bg, fg, obj.get_status_display())

    @admin.display(description='Залишилось')
    def days_badge(self, obj):
        days = obj.days_until_expiry
        if days is None:
            return '—'
        if days < 0:
            return format_html('<span style="color:#dc3545">Прострочено {}д</span>', abs(days))
        if days <= 7:
            return format_html('<span style="color:#fd7e14">{}д</span>', days)
        return format_html('<span style="color:#28a745">{}д</span>', days)

    @admin.display(description='Модулі')
    def modules_count(self, obj):
        n = len(obj.active_modules or [])
        return format_html('<span style="color:#999;font-size:12px">{} модулів</span>', n)

    @admin.display(description='Дії')
    def actions_col(self, obj):
        if obj.status == 'pending':
            return format_html(
                '<a href="{}/change/" style="color:#ffc107;font-size:12px">Активувати →</a>',
                obj.pk)
        if obj.status in ('expired', 'trial'):
            return format_html(
                '<a href="{}/change/" style="color:#007bff;font-size:12px">Продовжити →</a>',
                obj.pk)
        return '—'

    @admin.display(description='Зведення')
    def vendor_summary(self, obj):
        if not obj.pk:
            return '—'
        lines = [
            f'Email підтверджено: {"✅ Так" if obj.email_verified else "❌ Ні"}',
            f'Модулів активних: {len(obj.active_modules or [])}',
            f'Реєстрація: {obj.registered_at.strftime("%d.%m.%Y %H:%M") if obj.registered_at else "—"}',
            f'Активація: {obj.activated_at.strftime("%d.%m.%Y %H:%M") if obj.activated_at else "—"}',
            f'Trial до: {obj.trial_ends_at.strftime("%d.%m.%Y") if obj.trial_ends_at else "—"}',
        ]
        return format_html(
            '<div style="font-size:13px;line-height:1.8">{}</div>',
            format_html('<br>'.join(lines)))
