"""jlcpcb/admin.py — JLCPCB order tracking + product matching admin."""
from django.contrib import admin
from django.urls import path
from django.shortcuts import redirect, get_object_or_404
from django.contrib import messages
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.forms import PasswordInput

from .models import JLCConfig, JLCOrder, JLCProductMapping

# ── Status badge colors ────────────────────────────────────────────────────────
_STATUS_COLORS = {
    'ordered':       ('#607d8b', '#fff'),
    'reviewed':      ('#1565c0', '#fff'),
    'in_production': ('#e65100', '#fff'),
    'manufactured':  ('#2e7d32', '#fff'),
    'shipped':       ('#6a1b9a', '#fff'),
    'delivered':     ('#1b5e20', '#fff'),
    'cancelled':     ('#555',    '#fff'),
}
_STATUS_ICONS = {
    'ordered':       '📋',
    'reviewed':      '🔍',
    'in_production': '🏭',
    'manufactured':  '✅',
    'shipped':       '📦',
    'delivered':     '🎉',
    'cancelled':     '❌',
}
_MAPPING_COLORS = {
    'unmatched':  ('#c62828', '#fff'),
    'matched':    ('#2e7d32', '#fff'),
    'track_only': ('#1565c0', '#fff'),
    'ignored':    ('#555',    '#fff'),
}


def _badge(text, bg, fg='#fff'):
    return format_html(
        '<span style="background:{};color:{};padding:2px 8px;border-radius:10px;'
        'font-size:11px;font-weight:600;white-space:nowrap">{}</span>',
        bg, fg, text,
    )


@admin.register(JLCConfig)
class JLCConfigAdmin(admin.ModelAdmin):
    change_list_template = None

    fieldsets = (
        ('🔑 JLCPCB API Credentials', {
            'fields': ('api_key', 'api_secret', 'use_sandbox'),
            'description': (
                'Отримати доступ: <a href="https://jlcpcb.com/api" target="_blank">'
                'https://jlcpcb.com/api</a> — подайте заявку на developer portal.'
            ),
        }),
        ('🔄 Синхронізація', {
            'fields': ('sync_enabled', 'sync_interval_hours', 'last_synced_at'),
        }),
        ('📦 Склад', {
            'fields': ('auto_receive_on_delivered', 'default_location'),
        }),
        ('🔔 Сповіщення', {
            'fields': (
                'notify_on_shipped', 'notify_on_status_change', 'notify_on_delivered',
                'notify_telegram', 'notify_email', 'notify_email_to',
            ),
        }),
    )
    readonly_fields = ('last_synced_at',)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        for field in ('api_key', 'api_secret'):
            if field in form.base_fields:
                form.base_fields[field].widget = PasswordInput(render_value=True)
        return form

    def has_add_permission(self, request):
        return not JLCConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj, _ = JLCConfig.objects.get_or_create(pk=1)
        return redirect('admin:jlcpcb_jlcconfig_change', obj.pk)


@admin.register(JLCOrder)
class JLCOrderAdmin(admin.ModelAdmin):
    list_display = (
        'jlc_order_id_link', 'order_type', 'description_short',
        'quantity', 'status_badge', 'mapping_badge',
        'product_link', 'tracking_display',
        'order_date', 'shipped_date',
    )
    list_filter  = ('local_status', 'mapping_status', 'order_type')
    search_fields = ('jlc_order_id', 'jlc_order_number', 'description',
                     'tracking_number', 'product__sku')
    ordering     = ('-order_date', '-created_at')
    date_hierarchy = 'order_date'
    readonly_fields = (
        'jlc_status', 'auto_matched_sku', 'last_notified_status',
        'received_qty', 'raw_data', 'created_at', 'updated_at',
    )

    fieldsets = (
        ('📋 Замовлення JLCPCB', {
            'fields': (
                'jlc_order_id', 'jlc_order_number', 'order_type',
                'description', 'quantity',
            ),
        }),
        ('🔄 Статус', {
            'fields': ('local_status', 'jlc_status'),
        }),
        ('📦 Доставка', {
            'fields': ('tracking_number', 'tracking_carrier', 'tracking_url'),
        }),
        ('📅 Дати', {
            'fields': ('order_date', 'shipped_date', 'delivered_date', 'expected_date'),
        }),
        ('💰 Вартість', {
            'fields': ('unit_price', 'total_price', 'currency'),
            'classes': ('collapse',),
        }),
        ('🏭 Прив\'язка до складу', {
            'fields': ('product', 'mapping_status', 'auto_matched_sku',
                       'purchase_order', 'received_qty'),
        }),
        ('🔔 Сповіщення', {
            'fields': ('last_notified_status',),
            'classes': ('collapse',),
        }),
        ('📝 Нотатки та дані API', {
            'fields': ('notes', 'raw_data', 'created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    # ── Custom URLs for action buttons ────────────────────────────────────────
    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('<int:pk>/receive/',
                 self.admin_site.admin_view(self.receive_view),
                 name='jlcpcb_jlcorder_receive'),
            path('<int:pk>/match/',
                 self.admin_site.admin_view(self.match_view),
                 name='jlcpcb_jlcorder_match'),
            path('<int:pk>/set-track-only/',
                 self.admin_site.admin_view(self.set_track_only_view),
                 name='jlcpcb_jlcorder_set_track_only'),
            path('<int:pk>/set-ignored/',
                 self.admin_site.admin_view(self.set_ignored_view),
                 name='jlcpcb_jlcorder_set_ignored'),
            path('run-sync/',
                 self.admin_site.admin_view(self.run_sync_view),
                 name='jlcpcb_jlcorder_run_sync'),
            path('run-match/',
                 self.admin_site.admin_view(self.run_match_view),
                 name='jlcpcb_jlcorder_run_match'),
        ]
        return custom + urls

    def receive_view(self, request, pk):
        order = get_object_or_404(JLCOrder, pk=pk)
        if order.local_status != JLCOrder.LocalStatus.DELIVERED:
            messages.error(request, '❌ Прийом доступний тільки для доставлених замовлень.')
            return redirect('admin:jlcpcb_jlcorder_change', pk)
        if not order.product_id:
            messages.error(request, '❌ Спочатку прив\'яжіть товар до замовлення.')
            return redirect('admin:jlcpcb_jlcorder_change', pk)
        from .services.api import receive_into_inventory
        tx = receive_into_inventory(order, performed_by=request.user)
        if tx:
            messages.success(request, f'✅ Додано {tx.qty} шт. {order.product.sku} на склад.')
        else:
            messages.warning(request, '⚠️ Прийом вже виконано або кількість = 0.')
        return redirect('admin:jlcpcb_jlcorder_change', pk)

    def match_view(self, request, pk):
        order = get_object_or_404(JLCOrder, pk=pk)
        from .services.api import find_product_for_jlc_name
        product, match_type = find_product_for_jlc_name(order.description or order.jlc_order_id)
        if product:
            order.product         = product
            order.mapping_status  = JLCOrder.MappingStatus.MATCHED
            order.auto_matched_sku = product.sku
            order.save(update_fields=['product', 'mapping_status', 'auto_matched_sku', 'updated_at'])
            messages.success(request, f'✅ Прив\'язано до {product.sku} ({match_type})')
        else:
            messages.warning(request, '⚠️ Товар не знайдено автоматично. Вкажіть вручну у полі "Товар на складі".')
        return redirect('admin:jlcpcb_jlcorder_change', pk)

    def set_track_only_view(self, request, pk):
        order = get_object_or_404(JLCOrder, pk=pk)
        order.mapping_status = JLCOrder.MappingStatus.TRACK_ONLY
        order.save(update_fields=['mapping_status', 'updated_at'])
        messages.success(request, '👁 Встановлено: Тільки трекінг')
        return redirect('admin:jlcpcb_jlcorder_change', pk)

    def set_ignored_view(self, request, pk):
        order = get_object_or_404(JLCOrder, pk=pk)
        order.mapping_status = JLCOrder.MappingStatus.IGNORED
        order.save(update_fields=['mapping_status', 'updated_at'])
        messages.success(request, '🚫 Замовлення позначено як ігнорувати')
        return redirect('admin:jlcpcb_jlcorder_change', pk)

    def run_sync_view(self, request):
        from django.core.management import call_command
        from io import StringIO
        buf = StringIO()
        try:
            call_command('sync_jlc_orders', '--force', stdout=buf)
            out = buf.getvalue()
            messages.success(request, f'✅ Синхронізацію запущено. {out[:200]}')
        except Exception as e:
            messages.error(request, f'❌ Помилка: {e}')
        return redirect('admin:jlcpcb_jlcorder_changelist')

    def run_match_view(self, request):
        from django.core.management import call_command
        from io import StringIO
        buf = StringIO()
        try:
            call_command('sync_jlc_orders', '--match-only', stdout=buf)
            out = buf.getvalue()
            messages.success(request, f'✅ Авто-матч виконано. {out[:300]}')
        except Exception as e:
            messages.error(request, f'❌ Помилка: {e}')
        return redirect('admin:jlcpcb_jlcorder_changelist')

    # ── List display helpers ──────────────────────────────────────────────────
    @admin.display(description='Замовлення', ordering='jlc_order_id')
    def jlc_order_id_link(self, obj):
        return format_html(
            '<a href="{}" style="font-family:monospace;font-weight:600">{}</a>',
            f'/admin/jlcpcb/jlcorder/{obj.pk}/change/',
            obj.jlc_order_id,
        )

    @admin.display(description='Опис')
    def description_short(self, obj):
        txt = obj.description or ''
        if len(txt) > 45:
            return format_html('<span title="{}">{}&hellip;</span>', txt, txt[:45])
        return txt or '—'

    @admin.display(description='Статус', ordering='local_status')
    def status_badge(self, obj):
        bg, fg = _STATUS_COLORS.get(obj.local_status, ('#607d8b', '#fff'))
        icon = _STATUS_ICONS.get(obj.local_status, '')
        label = obj.get_local_status_display()
        return _badge(f'{icon} {label}', bg, fg)

    @admin.display(description='Прив\'язка', ordering='mapping_status')
    def mapping_badge(self, obj):
        bg, fg = _MAPPING_COLORS.get(obj.mapping_status, ('#607d8b', '#fff'))
        label = obj.get_mapping_status_display()
        return _badge(label, bg, fg)

    @admin.display(description='Товар', ordering='product__sku')
    def product_link(self, obj):
        if obj.product_id:
            return format_html(
                '<a href="/admin/inventory/product/{}/change/" '
                'style="color:var(--link-fg);font-weight:600">{}</a>',
                obj.product_id, obj.product.sku,
            )
        if obj.mapping_status == JLCOrder.MappingStatus.UNMATCHED:
            return format_html(
                '<span style="color:var(--err);font-size:11px">⚠️ не знайдено</span>'
            )
        return '—'

    @admin.display(description='Трекінг')
    def tracking_display(self, obj):
        if not obj.tracking_number:
            return '—'
        carrier = f' {obj.tracking_carrier}' if obj.tracking_carrier else ''
        if obj.tracking_url:
            return format_html(
                '<a href="{}" target="_blank" style="font-family:monospace">{}</a>{}',
                obj.tracking_url, obj.tracking_number, carrier,
            )
        return format_html('<span style="font-family:monospace">{}</span>{}',
                           obj.tracking_number, carrier)

    # ── change_view: inject action buttons ────────────────────────────────────
    def change_view(self, request, object_id, form_url='', extra_context=None):
        extra = extra_context or {}
        obj = JLCOrder.objects.filter(pk=object_id).first()
        if obj:
            extra['jlc_order']            = obj
            extra['can_receive']          = (
                obj.local_status == JLCOrder.LocalStatus.DELIVERED
                and obj.product_id
                and float(obj.received_qty) < obj.quantity
            )
            extra['is_unmatched']         = obj.mapping_status == JLCOrder.MappingStatus.UNMATCHED
        return super().change_view(request, object_id, form_url, extra_context=extra)

    def changelist_view(self, request, extra_context=None):
        extra = extra_context or {}
        from .models import JLCConfig
        cfg = JLCConfig.get()
        extra['jlc_sync_enabled']   = cfg.sync_enabled
        extra['jlc_last_synced']    = cfg.last_synced_at
        extra['jlc_unmatched_count'] = JLCOrder.objects.filter(
            mapping_status=JLCOrder.MappingStatus.UNMATCHED
        ).count()
        return super().changelist_view(request, extra_context=extra)

    # ── Bulk actions ──────────────────────────────────────────────────────────
    actions = ['action_run_automatch', 'action_mark_track_only', 'action_mark_ignored']

    @admin.action(description='🔍 Авто-прив\'язати до товарів на складі')
    def action_run_automatch(self, request, queryset):
        from .services.api import find_product_for_jlc_name
        matched = 0
        for order in queryset.filter(mapping_status=JLCOrder.MappingStatus.UNMATCHED):
            product, match_type = find_product_for_jlc_name(order.description or order.jlc_order_id)
            if product:
                order.product         = product
                order.mapping_status  = JLCOrder.MappingStatus.MATCHED
                order.auto_matched_sku = product.sku
                order.save(update_fields=['product', 'mapping_status', 'auto_matched_sku', 'updated_at'])
                matched += 1
        self.message_user(request, f'✅ Прив\'язано {matched} замовлень')

    @admin.action(description='👁 Позначити як "Тільки трекінг"')
    def action_mark_track_only(self, request, queryset):
        n = queryset.update(mapping_status=JLCOrder.MappingStatus.TRACK_ONLY)
        self.message_user(request, f'👁 Оновлено {n} замовлень')

    @admin.action(description='🚫 Позначити як "Ігнорувати"')
    def action_mark_ignored(self, request, queryset):
        n = queryset.update(mapping_status=JLCOrder.MappingStatus.IGNORED)
        self.message_user(request, f'🚫 Оновлено {n} замовлень')

    # ── On save: handle status-change side effects ────────────────────────────
    def save_model(self, request, obj, form, change):
        if change:
            old = JLCOrder.objects.filter(pk=obj.pk).values_list('local_status', flat=True).first()
            new = obj.local_status
            super().save_model(request, obj, form, change)
            if old and new and old != new:
                from .notifications import notify_jlc_status_change
                from .services.api import status_can_advance
                if status_can_advance(old, new) and new != obj.last_notified_status:
                    notify_jlc_status_change(obj, old, new)
                # Auto-receive on delivered
                if new == 'delivered':
                    from .models import JLCConfig
                    cfg = JLCConfig.get()
                    if cfg.auto_receive_on_delivered and obj.product_id and float(obj.received_qty) < obj.quantity:
                        from .services.api import receive_into_inventory
                        tx = receive_into_inventory(obj, performed_by=request.user)
                        if tx:
                            messages.success(request, f'📦 Авто-прийом: {tx.qty} шт. {obj.product.sku} додано на склад')
        else:
            super().save_model(request, obj, form, change)
            # Try auto-match for new manual orders
            if not obj.product_id and obj.description:
                from .services.api import find_product_for_jlc_name
                product, match_type = find_product_for_jlc_name(obj.description)
                if product:
                    obj.product         = product
                    obj.mapping_status  = JLCOrder.MappingStatus.MATCHED
                    obj.auto_matched_sku = product.sku
                    obj.save(update_fields=['product', 'mapping_status', 'auto_matched_sku', 'updated_at'])
                    messages.success(request, f'✅ Авто-прив\'язано до {product.sku}')


@admin.register(JLCProductMapping)
class JLCProductMappingAdmin(admin.ModelAdmin):
    list_display  = ('jlc_reference', 'product_link', 'match_type', 'created_at')
    list_filter   = ('match_type',)
    search_fields = ('jlc_reference', 'product__sku')
    raw_id_fields = ('product',)

    @admin.display(description='Товар', ordering='product__sku')
    def product_link(self, obj):
        return format_html(
            '<a href="/admin/inventory/product/{}/change/" style="color:var(--link-fg)">{}</a>',
            obj.product_id, obj.product.sku,
        )
