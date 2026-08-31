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
        'gerber_thumb', 'jlc_order_id_link', 'order_type', 'description_short',
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
            path('<int:pk>/wip-refresh/',
                 self.admin_site.admin_view(self.wip_refresh_view),
                 name='jlcpcb_jlcorder_wip_refresh'),
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
            # Gerber workflow
            path('gerber/',
                 self.admin_site.admin_view(self.gerber_page_view),
                 name='jlcpcb_gerber_page'),
            path('gerber/analyze/',
                 self.admin_site.admin_view(self.gerber_analyze_view),
                 name='jlcpcb_gerber_analyze'),
            path('gerber/create/',
                 self.admin_site.admin_view(self.gerber_create_view),
                 name='jlcpcb_gerber_create'),
            path('gerber/<int:pk>/reorder/',
                 self.admin_site.admin_view(self.gerber_reorder_view),
                 name='jlcpcb_gerber_reorder'),
            path('gerber/<int:pk>/raw-quote/',
                 self.admin_site.admin_view(self.gerber_raw_quote_view),
                 name='jlcpcb_gerber_raw_quote'),
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
            from .services.api import extract_pcb_item, _extract_gerber_images
            client = JLCAPIClient.from_config()
            raw    = client.get_pcb_order(batch)
            pcb    = extract_pcb_item(raw)
            # Preserve gerber preview images from previous Gerber-flow or extract from API
            prev_raw = order.raw_data if isinstance(order.raw_data, dict) else {}
            if prev_raw.get('gerber_top') and not raw.get('gerber_top'):
                raw['gerber_top'] = prev_raw['gerber_top']
            if prev_raw.get('gerber_bottom') and not raw.get('gerber_bottom'):
                raw['gerber_bottom'] = prev_raw['gerber_bottom']
            if prev_raw.get('pcb_param') and not raw.get('pcb_param'):
                raw['pcb_param'] = prev_raw['pcb_param']
            _extract_gerber_images(pcb, raw)
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

    def wip_refresh_view(self, request, pk):
        """Return WIP production progress via AJAX. Stores result in raw_data."""
        from django.http import JsonResponse
        order = get_object_or_404(JLCOrder, pk=pk)
        raw = order.raw_data if isinstance(order.raw_data, dict) else {}

        # Determine orderUUID:
        # - New API orders: jlc_order_id = orderId (UUID) which differs from jlc_order_number (batchNum)
        # - Synced orders:  jlc_order_id == jlc_order_number == batchNum — no UUID available
        order_uuid = (
            raw.get('order_uuid')
            or raw.get('orderId')
            or (order.jlc_order_id
                if order.jlc_order_id and order.jlc_order_id != order.jlc_order_number
                else None)
        )
        if not order_uuid:
            return JsonResponse({
                'ok': False,
                'no_uuid': True,
                'error': (
                    'orderUUID недоступний для цього замовлення. '
                    'WIP-прогрес доступний лише для замовлень, '
                    'створених через Gerber API (не синхронізованих).'
                ),
            })

        from .services.api import JLCAPIClient, JLCAPIError
        try:
            client = JLCAPIClient.from_config()
            wip_data = client.get_pcb_wip(order_uuid)
        except JLCAPIError as e:
            return JsonResponse({'ok': False, 'error': str(e)})
        except Exception as e:
            return JsonResponse({'ok': False, 'error': f'Помилка: {e}'})

        stages = wip_data if isinstance(wip_data, list) else []
        raw['production_steps'] = stages
        order.raw_data = raw
        order.save(update_fields=['raw_data'])
        return JsonResponse({'ok': True, 'stages': stages})

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

    # ── Gerber ordering workflow ──────────────────────────────────────────────

    def gerber_page_view(self, request):
        from django.template.response import TemplateResponse
        cfg = JLCConfig.get()
        return TemplateResponse(request, 'admin/jlcpcb/gerber_order.html', {
            **self.admin_site.each_context(request),
            'title':        'Нове PCB замовлення',
            'analyze_url':  reverse('admin:jlcpcb_gerber_analyze'),
            'create_url':   reverse('admin:jlcpcb_gerber_create'),
            'orders_url':   reverse('admin:jlcpcb_jlcorder_changelist'),
            'has_api_keys': bool(cfg.access_key and cfg.secret_key),
            'opts':         JLCOrder._meta,
        })

    def gerber_analyze_view(self, request):
        """AJAX: upload Gerber file → calculate quote. Returns price + gerber preview images."""
        import tempfile, os
        from .services.api import JLCAPIClient, JLCAPIError

        if request.method != 'POST':
            return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

        gerber_file = request.FILES.get('gerber_file')
        if not gerber_file:
            return JsonResponse({'ok': False, 'error': 'Файл не вибрано'})
        if not (gerber_file.name.lower().endswith('.zip')
                or gerber_file.name.lower().endswith('.rar')):
            return JsonResponse({'ok': False, 'error': 'Тільки .zip або .rar файли підтримуються'})

        try:
            layer = int(request.POST.get('layer', 2))
            # Core required params — always send
            pcb_param = {
                'layer':           layer,
                'width':           float(request.POST.get('width', 100)),
                'length':          float(request.POST.get('length', 100)),
                'qty':             int(request.POST.get('qty', 5)),
                'panelFlag':       int(request.POST.get('panel_flag', 0)),
                'thickness':       float(request.POST.get('thickness', 1.6)),
                'pcbColor':        int(request.POST.get('pcb_color', 0)),
                'surfaceFinish':   int(request.POST.get('surface_finish', 1)),
                'copperWeight':    int(request.POST.get('copper_weight', 1)),
                'flyingProbeTest': int(request.POST.get('flying_probe_test', 2)),
            }
            # Optional params — only include when explicitly non-default.
            # Sending default values (0) for optional fields triggers JLCPCB
            # validation errors (2603=impedance, 2108=gold_finger, etc.)
            def _opt_int(field, default=0, skip_val=None):
                v = int(request.POST.get(field, default))
                if v == default or (skip_val is not None and v == skip_val):
                    return None
                return v

            for api_key, field in [
                ('viaCovering',          'via_covering'),
                ('baseMaterial',         'base_material'),
                ('silkscreenColor',      'silkscreen_color'),
                ('goldFinger',           'gold_finger'),
                ('boardOutlineTolerance','board_outline_tolerance'),
                ('impedanceFlag',        'impedance_flag'),
                ('castellatedHoles',     'castellated_holes'),
            ]:
                v = _opt_int(field)
                if v is not None:
                    pcb_param[api_key] = v

            # differentDesign: default is 1, only send when > 1
            _dd = int(request.POST.get('different_design', 1))
            if _dd > 1:
                pcb_param['differentDesign'] = _dd
            # Inner copper only meaningful for multilayer
            if layer > 2:
                pcb_param['innerCopperWeight'] = int(request.POST.get('inner_copper_weight', 1))
            remarks = request.POST.get('remarks', '').strip()
            if remarks:
                pcb_param['remarks'] = remarks
            achieve_date = int(request.POST.get('achieve_date', 120))
            country      = request.POST.get('country', 'DE')
        except (ValueError, TypeError) as exc:
            return JsonResponse({'ok': False, 'error': f'Неправильні параметри: {exc}'})

        suffix   = '.zip' if gerber_file.name.lower().endswith('.zip') else '.rar'
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                for chunk in gerber_file.chunks():
                    tmp.write(chunk)
                tmp_path = tmp.name

            client   = JLCAPIClient.from_config()
            file_key = client.upload_gerber(tmp_path, file_name=gerber_file.name)
            quote    = client.calculate_quote(
                file_key=file_key,
                pcb_param=pcb_param,
                achieve_date=achieve_date,
                country=country,
            )
            # Try to find gerber preview images — search by known names AND by URL pattern
            _IMG_TOP_FIELDS    = ('gerberTop', 'gerberTopUrl', 'topImageUrl', 'topUrl',
                                  'pcbTopImage', 'renderTop', 'imageTop', 'topLayer')
            _IMG_BOTTOM_FIELDS = ('gerberBottom', 'gerberBottomUrl', 'bottomImageUrl', 'bottomUrl',
                                  'pcbBottomImage', 'renderBottom', 'imageBottom', 'bottomLayer')
            gerber_top    = next((quote.get(f) for f in _IMG_TOP_FIELDS    if quote.get(f)), '')
            gerber_bottom = next((quote.get(f) for f in _IMG_BOTTOM_FIELDS if quote.get(f)), '')
            # Fallback: scan all string fields for URL-like values containing image keywords
            if not gerber_top:
                for k, v in quote.items():
                    if isinstance(v, str) and ('http' in v) and any(
                        x in k.lower() for x in ('top', 'front', 'gerber', 'image', 'render', 'img')
                    ):
                        gerber_top = v
                        logger.info('JLC auto-detected gerber_top field=%s url=%s', k, v[:100])
                        break
            if not gerber_bottom:
                for k, v in quote.items():
                    if isinstance(v, str) and ('http' in v) and any(
                        x in k.lower() for x in ('bottom', 'back', 'gerber', 'image', 'render', 'img')
                    ) and v != gerber_top:
                        gerber_bottom = v
                        logger.info('JLC auto-detected gerber_bottom field=%s url=%s', k, v[:100])
                        break

            # Log FULL calculate response to find image URL field names
            import json as _json
            logger.info('JLC /calculate full response: %s',
                        _json.dumps(quote, ensure_ascii=False)[:3000])
            if gerber_top:
                logger.info('JLC gerber_top found: %s', gerber_top[:200])

            # Include raw quote keys in debug mode so browser console shows all fields
            _debug_keys = {k: (v[:120] if isinstance(v, str) else v)
                          for k, v in quote.items() if not isinstance(v, dict)}

            return JsonResponse({
                'ok':          True,
                'file_key':    file_key,
                'pcb_param':   pcb_param,
                'gerber_top':  gerber_top,
                'gerber_bottom': gerber_bottom,
                'price':       quote.get('priceWithoutFreight'),
                'weight':      quote.get('orderTotalWeight'),
                'pcb_cost':    quote.get('pcbCostInfo') or {},
                '_debug_quote': _debug_keys,
                'ship_list':   quote.get('shipList') or [],
                'achieve_list': quote.get('achieveDateList') or [],
                '_debug_quote_keys': list(quote.keys()),
            })
        except JLCAPIError as exc:
            return JsonResponse({'ok': False, 'error': str(exc)})
        except Exception as exc:
            return JsonResponse({'ok': False, 'error': f'Помилка: {exc}'})
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def gerber_create_view(self, request):
        """AJAX: place real PCB order using fileKey from previous analyze step."""
        import json as _json
        from datetime import date
        from .services.api import JLCAPIClient, JLCAPIError

        if request.method != 'POST':
            return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

        try:
            data = _json.loads(request.body)
        except Exception:
            return JsonResponse({'ok': False, 'error': 'Неправильний JSON'})

        file_key = (data.get('file_key') or '').strip()
        if not file_key:
            return JsonResponse({
                'ok': False,
                'error': 'fileKey відсутній — Gerber сесія закінчилась, завантажте файл знову',
            })

        shipping_method = (data.get('shipping_method') or '').strip()
        if not shipping_method:
            return JsonResponse({'ok': False, 'error': 'Оберіть метод доставки'})

        shipping_address = data.get('shipping_address') or {}
        if not shipping_address.get('country'):
            return JsonResponse({'ok': False, 'error': "Вкажіть країну в адресі доставки"})

        pcb_param    = data.get('pcb_param') or {}
        achieve_date = data.get('achieve_date')
        gerber_top    = (data.get('gerber_top') or '').strip()
        gerber_bottom = (data.get('gerber_bottom') or '').strip()

        # autoConfirmProductionFile is only relevant for create, not calculate
        if 'autoConfirmProductionFile' not in pcb_param:
            pcb_param['autoConfirmProductionFile'] = data.get('auto_confirm', 1)

        try:
            client = JLCAPIClient.from_config()
            result = client.create_pcb_order(
                file_key=file_key,
                pcb_param=pcb_param,
                shipping_address=shipping_address,
                shipping_method=shipping_method,
                achieve_date=achieve_date,
            )
            batch_num = result.get('batchNum', '')
            order_id  = result.get('orderId', batch_num)

            try:
                order_date = date.fromisoformat((result.get('orderDate') or '')[:10])
            except (ValueError, TypeError):
                order_date = date.today()

            # Augment result with gerber preview images and pcb_param for future use
            stored_raw = dict(result)
            stored_raw['pcb_param']    = pcb_param
            stored_raw['order_uuid']   = order_id  # orderId = orderUUID for WIP queries
            if gerber_top:
                stored_raw['gerber_top'] = gerber_top
            if gerber_bottom:
                stored_raw['gerber_bottom'] = gerber_bottom

            jlc_order = JLCOrder.objects.create(
                jlc_order_id=order_id,
                jlc_order_number=batch_num,
                order_type=JLCOrder.OrderType.PCB,
                quantity=pcb_param.get('qty', 1),
                local_status=JLCOrder.LocalStatus.ORDERED,
                order_date=order_date,
                raw_data=stored_raw,
            )
            return JsonResponse({
                'ok':        True,
                'batch_num': batch_num,
                'order_url': reverse('admin:jlcpcb_jlcorder_change', args=[jlc_order.pk]),
            })
        except JLCAPIError as exc:
            return JsonResponse({'ok': False, 'error': str(exc)})
        except Exception as exc:
            return JsonResponse({'ok': False, 'error': f'Помилка створення замовлення: {exc}'})

    def gerber_raw_quote_view(self, request, pk):
        """Debug: return raw_data of an order as pretty JSON — helps diagnose image URL issues."""
        order = get_object_or_404(JLCOrder, pk=pk)
        raw = order.raw_data if isinstance(order.raw_data, dict) else {}
        # Highlight image-relevant keys
        summary = {
            'gerber_top':    raw.get('gerber_top', '⚠️ not stored'),
            'gerber_bottom': raw.get('gerber_bottom', '⚠️ not stored'),
            'pcb_param':     raw.get('pcb_param', '⚠️ not stored'),
            'all_top_level_keys': list(raw.keys()),
        }
        from django.http import HttpResponse
        return HttpResponse(
            json.dumps(summary, ensure_ascii=False, indent=2),
            content_type='application/json; charset=utf-8',
        )

    def gerber_reorder_view(self, request, pk):
        """Open Gerber order form pre-filled with params from an existing order."""
        from django.template.response import TemplateResponse
        order = get_object_or_404(JLCOrder, pk=pk)
        cfg = JLCConfig.get()

        raw = order.raw_data if isinstance(order.raw_data, dict) else {}
        pcb_param = raw.get('pcb_param') or {}

        # If no stored pcb_param, try to reconstruct from pcbItem
        if not pcb_param:
            from .services.api import extract_pcb_item
            pcb = extract_pcb_item(raw)
            if pcb:
                _color_map = {'Green': 0, 'Red': 1, 'Yellow': 2, 'Blue': 3,
                              'White': 4, 'Black': 5, 'Purple': 6}
                _surface_map = {'HASL(with lead)': 0, 'LeadFree HASL': 1,
                                'HASL Lead-Free': 1, 'ENIG': 2, 'OSP': 3}
                pcb_param = {
                    'layer':         pcb.get('layer', 2),
                    'qty':           pcb.get('count', 5),
                    'width':         pcb.get('width', 100),
                    'length':        pcb.get('length', 100),
                    'thickness':     pcb.get('thickness', 1.6),
                    'pcbColor':      _color_map.get(pcb.get('pcbColor', 'Green'), 0),
                    'surfaceFinish': _surface_map.get(pcb.get('surfaceFinish', ''), 1),
                    'copperWeight':  pcb.get('copperWeight', 1),
                }

        return TemplateResponse(request, 'admin/jlcpcb/gerber_order.html', {
            **self.admin_site.each_context(request),
            'title':           'Повторне замовлення',
            'analyze_url':     reverse('admin:jlcpcb_gerber_analyze'),
            'create_url':      reverse('admin:jlcpcb_gerber_create'),
            'orders_url':      reverse('admin:jlcpcb_jlcorder_changelist'),
            'has_api_keys':    bool(cfg.access_key and cfg.secret_key),
            'opts':            JLCOrder._meta,
            'prefill_params':  json.dumps(pcb_param),
            'prefill_order_id': order.jlc_order_id,
        })

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

    @admin.display(description='PCB')
    def gerber_thumb(self, obj):
        raw = obj.raw_data if isinstance(obj.raw_data, dict) else {}
        top = raw.get('gerber_top', '')
        if top:
            return format_html(
                '<img src="{}" width="52" height="52" '
                'style="object-fit:contain;border-radius:4px;'
                'background:#0a1420;border:1px solid #243347;display:block" '
                'alt="PCB" onerror="this.style.display=\'none\'">',
                top,
            )
        # No render available — show PCB specs chip instead
        pcb_item = {}
        for item in raw.get('orderItem', []):
            pcb_item = item.get('pcbItem') or {}
            if pcb_item:
                break
        color_raw = (pcb_item.get('pcbColor') or '').lower()
        _COLOR_MAP = {
            'green': '#1a7a1a', 'blue': '#0d47a1', 'red': '#b71c1c',
            'black': '#1a1a1a', 'white': '#e0e0e0', 'yellow': '#f9a825',
            'purple': '#6a1b9a', 'matte black': '#1a1a1a',
        }
        bg_color = _COLOR_MAP.get(color_raw, '#1e3a2f')
        txt_color = '#fff' if color_raw not in ('white', 'yellow') else '#333'
        layers = pcb_item.get('layer', '')
        label  = f'{layers}L' if layers else 'PCB'
        size   = ''
        if pcb_item.get('width') and pcb_item.get('length'):
            size = f"{pcb_item['width']}×{pcb_item['length']}"
        return format_html(
            '<span style="display:flex;flex-direction:column;align-items:center;'
            'justify-content:center;width:52px;height:52px;border-radius:4px;'
            'background:{};color:{};font-size:11px;font-weight:700;gap:2px;'
            'text-align:center;line-height:1.2">'
            '<span>{}</span>'
            '<span style="font-size:9px;font-weight:400;opacity:.8">{}</span>'
            '</span>',
            bg_color, txt_color, label, size,
        )

    # ── changelist with toolbar ───────────────────────────────────────────────

    def changelist_view(self, request, extra_context=None):
        extra = extra_context or {}
        cfg   = JLCConfig.get()
        from datetime import timedelta, date as _date
        extra['jlc_sync_url']        = reverse('admin:jlcpcb_jlcorder_run_sync')
        extra['jlc_sync_period_url'] = reverse('admin:jlcpcb_jlcorder_run_sync_period')
        extra['jlc_match_url']       = reverse('admin:jlcpcb_jlcorder_run_match')
        extra['gerber_url']          = reverse('admin:jlcpcb_gerber_page')
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

            # Reorder button
            raw_check = obj.raw_data if isinstance(obj.raw_data, dict) else {}
            extra['reorder_url']  = reverse('admin:jlcpcb_gerber_reorder', args=[obj.pk])
            extra['has_pcb_param'] = bool(
                raw_check.get('pcb_param') or
                any(item.get('pcbItem') for item in raw_check.get('orderItem', []))
            )

            # Parse raw_data for rich display
            raw = obj.raw_data if isinstance(obj.raw_data, dict) else {}
            extra['jlc_raw']           = raw
            extra['gerber_top']    = raw.get('gerber_top', '')
            extra['gerber_bottom'] = raw.get('gerber_bottom', '')
            extra['jlc_shipping_method'] = raw.get('shippingMethod', '')
            extra['jlc_total_money']   = raw.get('totalDummyMoney')  # merchandise cost
            extra['jlc_paid_money']    = raw.get('totalMoney')       # actually charged (after credits)
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

            # Gerber file download URL (from pcbItem.orderFileUrl — may expire after ~24h)
            extra['gerber_file_url'] = next(
                (item.get('pcbItem', {}).get('orderFileUrl', '')
                 for item in raw.get('orderItem', [])
                 if item.get('pcbItem', {}).get('orderFileUrl')),
                raw.get('orderFileUrl', ''),
            )

            # DHL tracking events
            extra['dhl_events']      = raw.get('dhl_events') or []
            extra['dhl_status']      = raw.get('dhl_status', '')
            extra['dhl_status_desc'] = raw.get('dhl_status_desc', '')
            extra['dhl_origin']      = raw.get('dhl_origin', '')
            extra['dhl_dest']        = raw.get('dhl_dest', '')
            extra['dhl_updated_at']  = raw.get('dhl_updated_at', '')

            # Production progress (WIP)
            production_steps = raw.get('production_steps') or []
            extra['production_steps'] = production_steps
            _TOTAL_STEPS = 16
            extra['wip_percent'] = min(
                round(len(production_steps) / _TOTAL_STEPS * 100), 99
            ) if production_steps else 0

            # WIP refresh button availability
            _order_uuid = (
                raw.get('order_uuid')
                or raw.get('orderId')
                or (obj.jlc_order_id
                    if obj.jlc_order_id and obj.jlc_order_id != obj.jlc_order_number
                    else None)
            )
            extra['has_wip_uuid'] = bool(_order_uuid)
            extra['wip_url'] = reverse('admin:jlcpcb_jlcorder_wip_refresh', args=[obj.pk])

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
