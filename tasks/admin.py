from django.contrib import admin
from django.utils.html import format_html
from django.utils import timezone
from .models import Task


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = (
        'title_col', 'priority_badge', 'status_badge', 'task_type_col',
        'due_date_col', 'customer', 'order_link', 'notify_email', 'assigned_to',
    )
    list_filter  = (
        'status', 'priority', 'task_type', 'notify_email',
        ('due_date', admin.DateFieldListFilter),
    )
    search_fields = ('title', 'description', 'customer__name', 'order__order_number')
    date_hierarchy = 'due_date'
    autocomplete_fields = ['customer', 'order', 'product']

    fieldsets = (
        ('📋 Задача', {
            'fields': ('title', 'description', 'task_type', 'priority', 'status', 'due_date'),
        }),
        ("🔗 Зв'язки", {
            'fields': ('order', 'customer', 'product', 'note', 'assigned_to'),
            'classes': ('collapse',),
        }),
        ('📧 Нотифікація', {
            'fields': ('notify_email', 'notified_at'),
        }),
        ('ℹ️ Системне', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )
    readonly_fields = ('notified_at', 'created_at', 'updated_at')
    actions = ['mark_done', 'mark_cancelled', 'mark_in_progress', 'enable_email_notify']

    # ── Колонки ─────────────────────────────────────────────────────────────

    def title_col(self, obj):
        color = {
            'critical': '#ff5252',
            'high':     '#ff9800',
            'medium':   '#c9d8e4',
            'low':      '#607d8b',
        }.get(obj.priority, '#c9d8e4')
        return format_html('<span style="color:{}">{}</span>', color, obj.title)
    title_col.short_description = 'Задача'

    def priority_badge(self, obj):
        bg = {
            'critical': '#c62828',
            'high':     '#e65100',
            'medium':   '#f9a825',
            'low':      '#455a64',
        }.get(obj.priority, '#455a64')
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:10px;font-size:11px">{}</span>',
            bg, obj.get_priority_display(),
        )
    priority_badge.short_description = 'Пріоритет'

    def status_badge(self, obj):
        bg = {
            'pending':     '#455a64',
            'in_progress': '#1565c0',
            'done':        '#2e7d32',
            'cancelled':   '#212121',
        }.get(obj.status, '#455a64')
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:10px;font-size:11px">{}</span>',
            bg, obj.get_status_display(),
        )
    status_badge.short_description = 'Статус'

    def task_type_col(self, obj):
        return obj.get_task_type_display()
    task_type_col.short_description = 'Тип'

    def due_date_col(self, obj):
        if not obj.due_date:
            return '—'
        today = timezone.now().date()
        if obj.status in ('done', 'cancelled'):
            return format_html('<span style="color:#607d8b">{}</span>', obj.due_date)
        if obj.due_date < today:
            return format_html(
                '<span style="color:#ff5252;font-weight:bold">⚠️ {}</span>', obj.due_date,
            )
        if obj.due_date == today:
            return format_html(
                '<span style="color:#ff9800;font-weight:bold">🔔 {}</span>', obj.due_date,
            )
        return format_html('<span style="color:#c9d8e4">{}</span>', obj.due_date)
    due_date_col.short_description = 'Дедлайн'
    due_date_col.admin_order_field = 'due_date'

    def order_link(self, obj):
        if not obj.order:
            return '—'
        url = f'/admin/sales/salesorder/{obj.order.pk}/change/'
        return format_html('<a href="{}">{}</a>', url, obj.order.order_number)
    order_link.short_description = 'Замовлення'

    # ── Actions ──────────────────────────────────────────────────────────────

    @admin.action(description='✅ Позначити виконаними')
    def mark_done(self, request, queryset):
        n = queryset.update(status=Task.Status.DONE)
        self.message_user(request, f'Виконано: {n} задач.')

    @admin.action(description='🔄 В роботі')
    def mark_in_progress(self, request, queryset):
        n = queryset.update(status=Task.Status.IN_PROGRESS)
        self.message_user(request, f'В роботі: {n} задач.')

    @admin.action(description='❌ Скасувати')
    def mark_cancelled(self, request, queryset):
        n = queryset.update(status=Task.Status.CANCELLED)
        self.message_user(request, f'Скасовано: {n} задач.')

    @admin.action(description='📧 Увімкнути email нагадування')
    def enable_email_notify(self, request, queryset):
        n = queryset.update(notify_email=True)
        self.message_user(request, f'Email нагадування увімкнено для {n} задач.')
