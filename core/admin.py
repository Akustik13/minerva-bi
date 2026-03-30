from django.contrib import admin
from django.utils.html import format_html

from .models import AuditLog, UserProfile, ModuleRegistry


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


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display  = ('user', 'role', 'ai_enabled', 'ai_model')
    list_filter   = ('role', 'ai_enabled')
    search_fields = ('user__username', 'user__email', 'user__first_name', 'user__last_name')
    fieldsets = (
        ('Користувач', {
            'fields': ('user', 'role', 'notes'),
        }),
        ('AI-асистент', {
            'fields': ('ai_enabled', 'ai_model', 'ai_temperature', 'ai_system_prompt'),
            'classes': ('collapse',),
        }),
        ('Доступ до модулів', {
            'fields': ('allowed_modules',),
            'description': 'Залиште порожнім [], щоб використовувати дефолти ролі',
        }),
    )


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
            'core': '#f85149',
            'standard': '#58a6ff',
            'premium': '#c9a84c',
        }
        color = colors.get(obj.tier, '#9aafbe')
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:4px;font-size:11px">{}</span>',
            color, obj.get_tier_display(),
        )
