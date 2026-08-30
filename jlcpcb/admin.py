"""jlcpcb/admin.py — JLCPCB order tracking + product matching admin."""
import json
from django.contrib import admin
from django.urls import path, reverse
from django.shortcuts import redirect, get_object_or_404
from django.contrib import messages
from django.utils.html import format_html
from django.forms import PasswordInput
from django.http import JsonResponse
from django.utils import timezone

from .models import JLCConfig, JLCOrder, JLCProductMapping

# ── Badge helpers ─────────────────────────────────────────────────────────────

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


# ── JLCConfig admin ───────────────────────────────────────────────────────────

@admin.register(JLCConfig)
class JLCConfigAdmin(admin.ModelAdmin):
    change_form_template = 'admin/jlcpcb/jlcconfig/change_form.html'

    fieldsets = (
        ('🔑 JLCPCB API Credentials', {
            'fields': ('app_id', 'access_key', 'secret_key', 'use_sandbox'),
            'description': (
                'Ключі знаходяться на '
                '<a href="https://open.jlcpcb.com/console/setting" target="_blank">'
                'open.jlcpcb.com/console/setting</a>.<br>'
                '<b>App ID</b> — числовий ідентифікатор застосунку.<br>'
                '<b>Access Key</b> — публічний ключ доступу.<br>'
                '<b>Secret Key / Tokenization Key</b> — секретний ключ для підпису запитів.<br>'
                '<b>Увага:</b> перед тестом переконайтесь що у розділі '
                '<b>Requestable APIs</b> подано заявки та отримано підтвердження (Authorized APIs &gt; 0).'
            ),
        }),
        ('🔄 Синхронізація', {
            'fields': ('sync_enabled', 'sync_interval_hours', 'sync_days_back', 'last_synced_at'),
        }),
        ('📦 Склад', {
            'fields': ('auto_receive_on_delivered', 'default_location'),
        }),
        ('🔔 Сповіщення', {
            'fields': (
                'notify_on_shipped', 'notify_on_status_change', 'notify_on_delivered',
                'notify_telegram', 'telegram_personal_chat_id',
                'notify_email', 'notify_email_to',
            ),
            'description': (
                '💡 <b>Telegram Personal Chat ID</b>: відкрийте <b>@userinfobot</b> у Telegram '
                'і надішліть йому будь-яке повідомлення — він поверне ваш Chat ID. '
                'Якщо поле порожнє — сповіщення йдуть в канал із загальних налаштувань.'
            ),
        }),
        ('📋 Лог підключення', {
            'fields': ('connection_log',),
            'classes': ('collapse',),
        }),
    )
    readonly_fields = ('last_synced_at', 'connection_log')

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        for field in ('access_key', 'secret_key', 'app_id'):
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

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('test-connection/',
                 self.admin_site.admin_view(self.test_connection_view),
                 name='jlcpcb_config_test'),
            path('sync-orders/',
                 self.admin_site.admin_view(self.sync_orders_view),
                 name='jlcpcb_config_sync'),
            path('status-report/',
                 self.admin_site.admin_view(self.status_report_view),
                 name='jlcpcb_config_status_report'),
        ]
        return custom + urls

    def test_connection_view(self, request):
        from .services.api import JLCAPIClient
        cfg = JLCConfig.get()
        client = JLCAPIClient.from_config()
        result = client.test_connection()

        ts  = timezone.now().strftime('%d.%m.%Y %H:%M:%S')
        log = f'[{ts}] Тест з\'єднання\n{result["message"]}\n'
        if result.get('raw'):
            log += f'Відповідь: {json.dumps(result["raw"], ensure_ascii=False)[:400]}\n'

        cfg.connection_log = log + cfg.connection_log[:1000]
        cfg.save(update_fields=['connection_log'])

        if result['ok']:
            messages.success(request, result['message'])
        else:
            messages.error(request, result['message'])

        return redirect('admin:jlcpcb_jlcconfig_change', cfg.pk)

    def sync_orders_view(self, request):
        from django.core.management import call_command
        from io import StringIO
        buf = StringIO()
        try:
            call_command('sync_jlc_orders', '--force', stdout=buf)
            out = buf.getvalue() or 'Синхронізацію завершено.'
            messages.success(request, f'✅ {out[:400]}')

            cfg = JLCConfig.get()
            ts  = timezone.now().strftime('%d.%m.%Y %H:%M:%S')
            cfg.connection_log = f'[{ts}] Sync\n{out[:600]}\n' + cfg.connection_log[:800]
            cfg.save(update_fields=['connection_log'])
        except Exception as e:
            messages.error(request, f'❌ Помилка синхронізації: {e}')
        return redirect('admin:jlcpcb_jlcconfig_change', JLCConfig.get().pk)

    def status_report_view(self, request):
        from .notifications import notify_jlc_active_orders_summary
        result = notify_jlc_active_orders_summary()
        if result['count'] == 0:
            messages.info(request, 'ℹ️ Активних замовлень немає.')
        else:
            channels = [ch for ch, ok in [('Telegram', result['telegram']), ('Email', result['email'])] if ok]
            if channels:
                messages.success(request, f'✅ Звіт надіслано: {", ".join(channels)} ({result["count"]} замовлень)')
            else:
                messages.warning(request, f'⚠️ {result["count"]} активних замовлень — канали сповіщень не налаштовано.')
        return redirect('admin:jlcpcb_jlcconfig_change', JLCConfig.get().pk)

    def change_view(self, request, object_id, form_url='', extra_context=None):
        extra = extra_context or {}
        extra['test_url']           = reverse('admin:jlcpcb_config_test')
        extra['sync_url']           = reverse('admin:jlcpcb_config_sync')
        extra['status_report_url']  = reverse('admin:jlcpcb_config_status_report')
        extra['orders_url']         = reverse('admin:jlcpcb_jlcorder_changelist')
        extra['add_order_url']      = reverse('admin:jlcpcb_jlcorder_add')
        extra['orders_count']       = JLCOrder.objects.count()
        extra['active_orders']      = JLCOrder.objects.exclude(
            local_status__in=['delivered', 'cancelled']
        ).count()
        extra['unmatched_count']    = JLCOrder.objects.filter(
            mapping_status=JLCOrder.MappingStatus.UNMATCHED
        ).count()
        cfg = JLCConfig.get()
        extra['jlc_last_synced'] = cfg.last_synced_at
        return super().change_view(request, object_id, form_url, extra_context=extra)


# ── JLCOrder admin ────────────────────────────────────────────────────────────

@admin.register(JLCOrder)
class JLCOrderAdmin(admin.ModelAdmin):
    change_list_template = 'admin/jlcpcb/jlcorder/change_list.html'
    change_form_template = 'admin/jlcpcb/jlcorder/change_form.html'

    list_display = (
        'jlc_order_id_link', 'order_type', 'description_short',
        'quantity', 'status_badge', 'mapping_badge',
        'product_link', 'tracking_display',
        'order_date', 'shipped_date',
    )
    list_filter   = ('local_status', 'mapping_status', 'order_type')
    search_fields = ('jlc_order_id', 'jlc_order_number', 'description',
                     'tracking_number', 'product__sku')
    ordering      = ('-order_date', '-created_at')
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
            'description': (
                '⚠️ <b>Номер замовлення (jlc_order_number)</b> — це Batch Number з JLCPCB '
                '(напр. <code>W2025040800001</code>), видимий в розділі Order History на сайті JLCPCB. '
                'Без цього поля синхронізація статусу через API неможлива.'
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
        ("🏭 Прив'язка до складу", {
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

    # ── Custom URLs ───────────────────────────────────────────────────────────
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
            path('<int:pk>/refresh/',
                 self.admin_site.admin_view(self.refresh_order_view),
                 name='jlcpcb_jlcorder_refresh'),
            path('run-sync/',
                 self.admin_site.admin_view(self.run_sync_view),
                 name='jlcpcb_jlcorder_run_sync'),
            path('run-sync-period/',
                 self.admin_site.admin_view(self.run_sync_period_view),
                 name='jlcpcb_jlcorder_run_sync_period'),
            path('run-match/',
                 self.admin_site.admin_view(self.run_match_view),
                 name='jlcpcb_jlcorder_run_match'),
            path('<int:pk>/send-notification/',
                 self.admin_site.admin_view(self.send_notification_view),
                 name='jlcpcb_jlcorder_send_notification'),
            path('<int:pk>/mark-delivered/',
                 self.admin_site.admin_view(self.mark_delivered_view),
                 name='jlcpcb_jlcorder_mark_delivered'),
        ]
        return custom + urls

    def receive_view(self, request, pk):
        order = get_object_or_404(JLCOrder, pk=pk)
        if order.local_status != JLCOrder.LocalStatus.DELIVERED:
            messages.error(request, '❌ Прийом тільки для доставлених замовлень.')
            return redirect('admin:jlcpcb_jlcorder_change', pk)
        if not order.product_id:
            messages.error(request, "❌ Спочатку прив'яжіть товар до замовлення.")
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
            order.product          = product
            order.mapping_status   = JLCOrder.MappingStatus.MATCHED
            order.auto_matched_sku = product.sku
            order.save(update_fields=['product', 'mapping_status', 'auto_matched_sku', 'updated_at'])
            messages.success(request, f"✅ Прив'язано до {product.sku} ({match_type})")
        else:
            messages.warning(request, "⚠️ Товар не знайдено. Вкажіть вручну у полі «Товар на складі».")
        return redirect('admin:jlcpcb_jlcorder_change', pk)

    def set_track_only_view(self, request, pk):
        JLCOrder.objects.filter(pk=pk).update(mapping_status=JLCOrder.MappingStatus.TRACK_ONLY)
        messages.success(request, '👁 Встановлено: Тільки трекінг')
        return redirect('admin:jlcpcb_jlcorder_change', pk)

    def set_ignored_view(self, request, pk):
        JLCOrder.objects.filter(pk=pk).update(mapping_status=JLCOrder.MappingStatus.IGNORED)
        messages.success(request, '🚫 Замовлення позначено як ігнорувати')
        return redirect('admin:jlcpcb_jlcorder_change', pk)

    def refresh_order_view(self, request, pk):
        """Refresh a single order status from JLCPCB API by batch number."""
        order = get_object_or_404(JLCOrder, pk=pk)
        from .services.api import JLCAPIClient, JLCAPIError, map_jlc_status, status_can_advance
        from .notifications import notify_jlc_status_change
        cfg = JLCConfig.get()
        if not cfg.access_key:
            messages.warning(request, '⚠️ API ключі не налаштовано в JLCConfig.')
            return redirect('admin:jlcpcb_jlcorder_change', pk)
        batch = order.jlc_order_number or order.jlc_order_id
        if not batch:
            messages.error(request, '❌ Вкажіть номер замовлення (Batch Number) у полі «Номер замовлення».')
            return redirect('admin:jlcpcb_jlcorder_change', pk)
        try:
            from .services.api import extract_pcb_item
            client = JLCAPIClient.from_config()
            raw    = client.get_pcb_order(batch)
            pcb    = extract_pcb_item(raw)
            old_st = order.local_status

            status_int = pcb.get('orderStatus')
            new_st     = map_jlc_status(status_int)
            order.raw_data   = raw
            order.jlc_status = str(status_int) if status_int is not None else ''

            # Fill description from Gerber file name if blank
            if not order.description and pcb.get('fileName'):
                order.description = pcb['fileName']

            if status_can_advance(old_st, new_st):
                order.local_status = new_st
                if new_st == 'shipped' and not order.shipped_date:
                    order.shipped_date = timezone.now().date()
                if new_st == 'delivered' and not order.delivered_date:
                    order.delivered_date = timezone.now().date()

            order.save()
            messages.success(request, f'✅ Статус оновлено: {order.get_local_status_display()}')
            if old_st != order.local_status and order.local_status != order.last_notified_status:
                notify_jlc_status_change(order, old_st, order.local_status)
        except JLCAPIError as e:
            messages.error(request, f'❌ Помилка API: {e}')
        return redirect('admin:jlcpcb_jlcorder_change', pk)

    def run_sync_view(self, request):
        from django.core.management import call_command
        from io import StringIO
        buf = StringIO()
        try:
            call_command('sync_jlc_orders', '--force', stdout=buf)
            messages.success(request, f'✅ {buf.getvalue()[:300] or "Синхронізацію завершено."}')
        except Exception as e:
            messages.error(request, f'❌ {e}')
        return redirect('admin:jlcpcb_jlcorder_changelist')

    def run_sync_period_view(self, request):
        """Sync with specific period from GET params: ?days=30 or ?date_from=&date_to="""
        from django.core.management import call_command
        from io import StringIO
        buf  = StringIO()
        days = None
        date_from = request.GET.get('date_from', '').strip()
        date_to   = request.GET.get('date_to',   '').strip()
        try:
            if date_from and date_to:
                # Custom range — convert to days from today
                from datetime import date
                d_from = date.fromisoformat(date_from)
                d_to   = date.fromisoformat(date_to)
                days   = (date.today() - d_from).days + 1
                label  = f'{date_from} → {date_to}'
            else:
                days  = int(request.GET.get('days', 90))
                label = f'{days} днів'
            call_command('sync_jlc_orders', '--force', f'--days={days}', stdout=buf)
            out = buf.getvalue() or 'Синхронізацію завершено.'
            messages.success(request, f'✅ Синхронізація ({label}): {out[:300]}')
            cfg = JLCConfig.get()
            ts  = timezone.now().strftime('%d.%m.%Y %H:%M:%S')
            cfg.connection_log = f'[{ts}] Sync ({label})\n{out[:500]}\n' + cfg.connection_log[:700]
            cfg.save(update_fields=['connection_log'])
        except Exception as e:
            messages.error(request, f'❌ Помилка синхронізації: {e}')
        return redirect('admin:jlcpcb_jlcorder_changelist')

    def run_match_view(self, request):
        from django.core.management import call_command
        from io import StringIO
        buf = StringIO()
        try:
            call_command('sync_jlc_orders', '--match-only', stdout=buf)
            messages.success(request, f'✅ {buf.getvalue()[:300] or "Авто-матч виконано."}')
        except Exception as e:
            messages.error(request, f'❌ {e}')
        return redirect('admin:jlcpcb_jlcorder_changelist')

    def mark_delivered_view(self, request, pk):
        """Manually mark a shipped order as delivered."""
        order = get_object_or_404(JLCOrder, pk=pk)
        if order.local_status not in (JLCOrder.LocalStatus.SHIPPED, JLCOrder.LocalStatus.MANUFACTURED):
            messages.warning(request, '⚠️ Позначити доставленим можна лише для відправлених замовлень.')
            return redirect('admin:jlcpcb_jlcorder_change', pk)
        old_status = order.local_status
        order.local_status   = JLCOrder.LocalStatus.DELIVERED
        order.delivered_date = timezone.now().date()
        order.save(update_fields=['local_status', 'delivered_date', 'updated_at'])
        messages.success(request, f'✅ {order.jlc_order_id} позначено як доставлено.')
        from .notifications import notify_jlc_status_change
        notify_jlc_status_change(order, old_status, 'delivered')
        cfg = JLCConfig.get()
        if cfg.auto_receive_on_delivered and order.product_id and float(order.received_qty) < order.quantity:
            from .services.api import receive_into_inventory
            tx = receive_into_inventory(order, performed_by=request.user)
            if tx:
                messages.success(request, f'📦 Авто-прийом: {tx.qty} шт. {order.product.sku} додано на склад.')
        return redirect('admin:jlcpcb_jlcorder_change', pk)

    def send_notification_view(self, request, pk):
        """Send current order status to Telegram/email immediately (test/manual)."""
        order = get_object_or_404(JLCOrder, pk=pk)
        from .notifications import notify_jlc_status_change
        cfg = JLCConfig.get()
        if not cfg.notify_telegram and not cfg.notify_email:
            messages.warning(request, '⚠️ Сповіщення вимкнено в налаштуваннях JLCConfig.')
        else:
            notify_jlc_status_change(order, order.local_status, order.local_status, force=True)
            channels = []
            if cfg.notify_telegram:
                channels.append('Telegram')
            if cfg.notify_email:
                channels.append('Email')
            messages.success(request, f'🔔 Сповіщення надіслано: {", ".join(channels)} — {order.jlc_order_id}')
        return redirect('admin:jlcpcb_jlcorder_change', pk)

    # ── List display ──────────────────────────────────────────────────────────

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
        icon   = _STATUS_ICONS.get(obj.local_status, '')
        return _badge(f'{icon} {obj.get_local_status_display()}', bg, fg)

    @admin.display(description="Прив'язка", ordering='mapping_status')
    def mapping_badge(self, obj):
        bg, fg = _MAPPING_COLORS.get(obj.mapping_status, ('#607d8b', '#fff'))
        return _badge(obj.get_mapping_status_display(), bg, fg)

    @admin.display(description='Товар', ordering='product__sku')
    def product_link(self, obj):
        if obj.product_id:
            return format_html(
                '<a href="/admin/inventory/product/{}/change/" '
                'style="color:var(--link-fg);font-weight:600">{}</a>',
                obj.product_id, obj.product.sku,
            )
        if obj.mapping_status == JLCOrder.MappingStatus.UNMATCHED:
            return format_html('<span style="color:var(--err);font-size:11px">⚠️ не знайдено</span>')
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

    # ── changelist with toolbar ───────────────────────────────────────────────

    def changelist_view(self, request, extra_context=None):
        extra = extra_context or {}
        cfg   = JLCConfig.get()
        from datetime import timedelta, date as _date
        extra['jlc_sync_url']        = reverse('admin:jlcpcb_jlcorder_run_sync')
        extra['jlc_sync_period_url'] = reverse('admin:jlcpcb_jlcorder_run_sync_period')
        extra['jlc_match_url']       = reverse('admin:jlcpcb_jlcorder_run_match')
        extra['today']               = _date.today().isoformat()
        extra['today_minus_90']      = (_date.today() - timedelta(days=90)).isoformat()
        extra['jlc_config_url']     = reverse('admin:jlcpcb_jlcconfig_change', args=[1])
        extra['jlc_sync_enabled']   = cfg.sync_enabled
        extra['jlc_last_synced']    = cfg.last_synced_at
        extra['jlc_unmatched_count'] = JLCOrder.objects.filter(
            mapping_status=JLCOrder.MappingStatus.UNMATCHED
        ).count()
        # Status summary for toolbar
        from django.db.models import Count
        summary = {
            row['local_status']: row['cnt']
            for row in JLCOrder.objects.values('local_status').annotate(cnt=Count('id'))
        }
        extra['jlc_status_summary'] = summary
        return super().changelist_view(request, extra_context=extra)

    # ── change_view: inject action buttons ────────────────────────────────────

    def change_view(self, request, object_id, form_url='', extra_context=None):
        extra = extra_context or {}
        obj   = JLCOrder.objects.filter(pk=object_id).select_related('product').first()
        if obj:
            extra['jlc_order'] = obj
            extra['can_receive'] = (
                obj.local_status == JLCOrder.LocalStatus.DELIVERED
                and obj.product_id
                and float(obj.received_qty) < obj.quantity
            )
            extra['is_unmatched'] = obj.mapping_status == JLCOrder.MappingStatus.UNMATCHED
            extra['refresh_url']           = reverse('admin:jlcpcb_jlcorder_refresh', args=[obj.pk])
            extra['receive_url']           = reverse('admin:jlcpcb_jlcorder_receive', args=[obj.pk])
            extra['match_url']             = reverse('admin:jlcpcb_jlcorder_match', args=[obj.pk])
            extra['set_track_only_url']    = reverse('admin:jlcpcb_jlcorder_set_track_only', args=[obj.pk])
            extra['set_ignored_url']       = reverse('admin:jlcpcb_jlcorder_set_ignored', args=[obj.pk])
            extra['send_notification_url'] = reverse('admin:jlcpcb_jlcorder_send_notification', args=[obj.pk])
            extra['mark_delivered_url']    = reverse('admin:jlcpcb_jlcorder_mark_delivered', args=[obj.pk])
            cfg = JLCConfig.get()
            extra['has_api_keys']      = bool(cfg.access_key and cfg.secret_key)
            extra['notify_configured'] = bool(cfg.notify_telegram or cfg.notify_email)
            extra['is_overdue_shipped'] = (
                obj.local_status in (JLCOrder.LocalStatus.SHIPPED, JLCOrder.LocalStatus.MANUFACTURED)
                and obj.expected_date is not None
                and obj.expected_date < timezone.now().date()
            )

            # Parse raw_data for rich display
            raw = obj.raw_data if isinstance(obj.raw_data, dict) else {}
            extra['jlc_raw']           = raw
            extra['jlc_shipping_method'] = raw.get('shippingMethod', '')
            extra['jlc_total_money']   = raw.get('totalMoney')
            extra['jlc_carriage']      = raw.get('totalCarriageMoney')
            extra['jlc_payment']       = raw.get('paymentMethod', '')
            addr = raw.get('orderAddress') or {}
            extra['jlc_address'] = ', '.join(
                p for p in [addr.get('linkAddress'), addr.get('city'),
                             addr.get('province'), addr.get('country')] if p
            )

            from .services.api import map_jlc_status
            _STATUS_UK = {
                'ordered': 'Замовлено', 'reviewed': 'Перевірено',
                'in_production': 'У виробництві', 'manufactured': 'Виготовлено',
                'shipped': 'Відправлено', 'delivered': 'Доставлено', 'cancelled': 'Скасовано',
            }
            _STATUS_ICON = {
                'ordered': '📋', 'reviewed': '🔍', 'in_production': '🏭',
                'manufactured': '✅', 'shipped': '📦', 'delivered': '🎉', 'cancelled': '❌',
            }
            sub_orders = []
            for item in raw.get('orderItem', []):
                pcb = item.get('pcbItem') or {}
                if not pcb:
                    continue
                st_int = pcb.get('orderStatus')
                st_key = map_jlc_status(st_int)
                cancel = pcb.get('cancelReason') or ''
                # Strip HTML tags from cancelReason
                import re
                cancel = re.sub(r'<[^>]+>', '', cancel).strip()
                sub_orders.append({
                    'file_name':    pcb.get('fileName', '—'),
                    'produce_code': pcb.get('produceCode', ''),
                    'count':        pcb.get('count', 0),
                    'status_key':   st_key,
                    'status_label': _STATUS_UK.get(st_key, st_key),
                    'status_icon':  _STATUS_ICON.get(st_key, ''),
                    'order_date':   (pcb.get('orderDate') or '')[:10],
                    'delivery_time': (pcb.get('deliveryTime') or '')[:16].replace('T', ' '),
                    'price':        pcb.get('price'),
                    'size':         f"{pcb.get('width')}×{pcb.get('length')} мм" if pcb.get('width') else '',
                    'layers':       pcb.get('layer', ''),
                    'thickness':    pcb.get('thickness', ''),
                    'color':        pcb.get('pcbColor', ''),
                    'surface':      pcb.get('surfaceFinish', ''),
                    'material':     pcb.get('materialDetails', ''),
                    'copper':       pcb.get('copperWeight', ''),
                    'half_hole':    pcb.get('halfHole', ''),
                    'build_time':   pcb.get('buildTime', ''),
                    'cancel':       cancel,
                })
            extra['jlc_sub_orders'] = sub_orders
        return super().change_view(request, object_id, form_url, extra_context=extra)

    # ── Bulk actions ──────────────────────────────────────────────────────────
    actions = ['action_run_automatch', 'action_mark_track_only', 'action_mark_ignored']

    @admin.action(description="🔍 Авто-прив'язати до товарів на складі")
    def action_run_automatch(self, request, queryset):
        from .services.api import find_product_for_jlc_name
        matched = 0
        for order in queryset.filter(mapping_status=JLCOrder.MappingStatus.UNMATCHED):
            product, match_type = find_product_for_jlc_name(order.description or order.jlc_order_id)
            if product:
                order.product          = product
                order.mapping_status   = JLCOrder.MappingStatus.MATCHED
                order.auto_matched_sku = product.sku
                order.save(update_fields=['product', 'mapping_status', 'auto_matched_sku', 'updated_at'])
                matched += 1
        self.message_user(request, f"✅ Прив'язано {matched} замовлень")

    @admin.action(description='👁 Позначити як "Тільки трекінг"')
    def action_mark_track_only(self, request, queryset):
        n = queryset.update(mapping_status=JLCOrder.MappingStatus.TRACK_ONLY)
        self.message_user(request, f'👁 Оновлено {n} замовлень')

    @admin.action(description='🚫 Позначити як "Ігнорувати"')
    def action_mark_ignored(self, request, queryset):
        n = queryset.update(mapping_status=JLCOrder.MappingStatus.IGNORED)
        self.message_user(request, f'🚫 Оновлено {n} замовлень')

    # ── On save: status-change side effects ───────────────────────────────────

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
                if new == 'delivered':
                    cfg = JLCConfig.get()
                    if cfg.auto_receive_on_delivered and obj.product_id and float(obj.received_qty) < obj.quantity:
                        from .services.api import receive_into_inventory
                        tx = receive_into_inventory(obj, performed_by=request.user)
                        if tx:
                            messages.success(request, f'📦 Авто-прийом: {tx.qty} шт. {obj.product.sku} додано на склад')
        else:
            super().save_model(request, obj, form, change)
            # Auto-match on create
            if not obj.product_id and obj.description:
                from .services.api import find_product_for_jlc_name
                product, match_type = find_product_for_jlc_name(obj.description)
                if product:
                    obj.product          = product
                    obj.mapping_status   = JLCOrder.MappingStatus.MATCHED
                    obj.auto_matched_sku = product.sku
                    obj.save(update_fields=['product', 'mapping_status', 'auto_matched_sku', 'updated_at'])
                    messages.success(request, f"✅ Авто-прив'язано до {product.sku}")


# ── JLCProductMapping admin ────────────────────────────────────────────────────

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
