from __future__ import annotations
from decimal import Decimal
from django.contrib import admin, messages
from django.db.models import Sum, Count, Max
from django.shortcuts import redirect
from django.urls import path
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from .models import Customer, CustomerNote
from sales.models import SalesOrder, SalesOrderLine


class CustomerNoteInline(admin.TabularInline):
    model = CustomerNote
    extra = 1
    fields = ("note_type", "subject", "body", "created_at", "created_by")
    readonly_fields = ("created_at",)


# ── Фільтр: постійні клієнти ──────────────────────────────────────────────────
class RepeatCustomerFilter(admin.SimpleListFilter):
    title = "Тип клієнта"
    parameter_name = "repeat"

    def lookups(self, request, model_admin):
        return [("yes", "✅ Постійні"), ("no", "🔸 Нові")]

    def queryset(self, request, queryset):
        qs = queryset.annotate(order_count=Count("id"))
        if self.value() == "yes":
            return qs.filter(order_count__gt=1)
        if self.value() == "no":
            return qs.filter(order_count=1)
        return queryset


# ── Фільтр: RFM сегмент ──────────────────────────────────────────────────────
class RFMSegmentFilter(admin.SimpleListFilter):
    title = "RFM Сегмент"
    parameter_name = "rfm_segment"

    def lookups(self, request, model_admin):
        return [
            ("champions",   "🏆 Champions"),
            ("loyal",       "💎 Loyal"),
            ("potential",   "⭐ Potential"),
            ("regular",     "🔄 Regular"),
            ("at_risk",     "😴 At Risk"),
            ("hibernating", "💤 Hibernating"),
            ("new",         "🆕 New"),
        ]

    def queryset(self, request, queryset):
        """
        Фільтр по RFM — використовує точну логіку з моделі Customer.rfm_score()
        ПОВІЛЬНО для великої кількості клієнтів, але точно!
        """
        if not self.value():
            return queryset
        
        # Отримуємо всіх клієнтів і фільтруємо по rfm_segment
        filtered_ids = []
        target_segment = self.value()
        
        # Мапінг значень фільтра до емодзі сегментів
        segment_map = {
            'champions':   '🏆 Champions',
            'loyal':       '💎 Loyal',
            'potential':   '⭐ Potential',
            'regular':     '🔄 Regular',
            'at_risk':     '😴 At Risk',
            'hibernating': '💤 Hibernating',
            'new':         '🆕 New',
        }
        
        target_emoji = segment_map.get(target_segment)
        if not target_emoji:
            return queryset
        
        for customer in queryset:
            try:
                segment = customer.rfm_score()['segment']
                if segment == target_emoji:
                    filtered_ids.append(customer.pk)
            except Exception:
                continue
        
        return queryset.filter(pk__in=filtered_ids)


    def choices(self, changelist):
        """
        Перевизначаємо choices щоб показати правильні лічильники.
        Рахуємо кількість клієнтів у кожному сегменті.
        """
        # Отримуємо базовий queryset
        from django.utils.encoding import force_str
        
        # Рахуємо сегменти
        segment_counts = {}
        for customer in changelist.queryset:
            try:
                segment_key = customer.rfm_score()['segment']
                segment_counts[segment_key] = segment_counts.get(segment_key, 0) + 1
            except Exception:
                continue
        
        # Мапінг для відображення
        segment_map = {
            'champions':   '🏆 Champions',
            'loyal':       '💎 Loyal',
            'potential':   '⭐ Potential',
            'regular':     '🔄 Regular',
            'at_risk':     '😴 At Risk',
            'hibernating': '💤 Hibernating',
            'new':         '🆕 New',
        }
        
        yield {
            'selected': self.value() is None,
            'query_string': changelist.get_query_string(remove=[self.parameter_name]),
            'display': 'Всі',
        }
        
        for lookup, title in self.lookup_choices:
            emoji_segment = segment_map.get(lookup, title)
            count = segment_counts.get(emoji_segment, 0)
            yield {
                'selected': self.value() == force_str(lookup),
                'query_string': changelist.get_query_string({self.parameter_name: lookup}),
                'display': f'{title} ({count})',
            }

@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    change_list_template = "admin/crm/customer_changelist.html"

    actions = [
        # Статус
        "action_set_status_active",
        "action_set_status_inactive",
        "action_set_status_vip",
        "action_set_status_blocked",
        # Сегмент
        "action_set_segment_b2b",
        "action_set_segment_b2c",
        "action_set_segment_distributor",
        "action_set_segment_reseller",
        "action_set_segment_other",
    ]

    # ── Actions: Статус ───────────────────────────────────────────────────────

    def action_set_status_active(self, request, queryset):
        n = queryset.update(status=Customer.Status.ACTIVE)
        self.message_user(request, f"✅ Статус «Активний» встановлено для {n} клієнтів.", messages.SUCCESS)
    action_set_status_active.short_description = "🟢 Статус → Активний"

    def action_set_status_inactive(self, request, queryset):
        n = queryset.update(status=Customer.Status.INACTIVE)
        self.message_user(request, f"⚪ Статус «Неактивний» встановлено для {n} клієнтів.", messages.SUCCESS)
    action_set_status_inactive.short_description = "⚪ Статус → Неактивний"

    def action_set_status_vip(self, request, queryset):
        n = queryset.update(status=Customer.Status.VIP)
        self.message_user(request, f"⭐ Статус «VIP» встановлено для {n} клієнтів.", messages.SUCCESS)
    action_set_status_vip.short_description = "⭐ Статус → VIP"

    def action_set_status_blocked(self, request, queryset):
        n = queryset.update(status=Customer.Status.BLOCKED)
        self.message_user(request, f"🚫 Статус «Заблокований» встановлено для {n} клієнтів.", messages.WARNING)
    action_set_status_blocked.short_description = "🚫 Статус → Заблокований"

    # ── Actions: Сегмент ──────────────────────────────────────────────────────

    def action_set_segment_b2b(self, request, queryset):
        n = queryset.update(segment=Customer.Segment.B2B)
        self.message_user(request, f"🏢 Сегмент «B2B» встановлено для {n} клієнтів.", messages.SUCCESS)
    action_set_segment_b2b.short_description = "🏢 Сегмент → B2B"

    def action_set_segment_b2c(self, request, queryset):
        n = queryset.update(segment=Customer.Segment.B2C)
        self.message_user(request, f"👤 Сегмент «B2C» встановлено для {n} клієнтів.", messages.SUCCESS)
    action_set_segment_b2c.short_description = "👤 Сегмент → B2C"

    def action_set_segment_distributor(self, request, queryset):
        n = queryset.update(segment=Customer.Segment.DISTRIBUTOR)
        self.message_user(request, f"🔗 Сегмент «Дистриб'ютор» встановлено для {n} клієнтів.", messages.SUCCESS)
    action_set_segment_distributor.short_description = "🔗 Сегмент → Дистриб'ютор"

    def action_set_segment_reseller(self, request, queryset):
        n = queryset.update(segment=Customer.Segment.RESELLER)
        self.message_user(request, f"🛒 Сегмент «Реселер» встановлено для {n} клієнтів.", messages.SUCCESS)
    action_set_segment_reseller.short_description = "🛒 Сегмент → Реселер"

    def action_set_segment_other(self, request, queryset):
        n = queryset.update(segment=Customer.Segment.OTHER)
        self.message_user(request, f"📎 Сегмент «Інше» встановлено для {n} клієнтів.", messages.SUCCESS)
    action_set_segment_other.short_description = "📎 Сегмент → Інше"

    # EMAIL прибраний зі списку
    list_display = (
        "display_name", "country_flag", "segment_badge",
        "status_badge", "orders_count", "revenue_display",
        "avg_order_display", "last_order_display",
        "repeat_badge", "rfm_display",
    )
    list_filter = ("segment", "status", "country", RepeatCustomerFilter, RFMSegmentFilter)
    search_fields = ("name", "email", "company", "phone", "addr_city", "addr_street")
    # Зберігаємо фільтри між сесіями
    preserve_filters = True
    readonly_fields = (
        "created_at", "updated_at",
        "orders_count", "revenue_display", "avg_order_display",
        "last_order_display", "repeat_badge",
        "top_products_display", "order_history_display",
    )
    inlines = [CustomerNoteInline]

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "sync-crm/",
                self.admin_site.admin_view(self.sync_view),
                name="crm_customer_sync",
            ),
        ]
        return custom + urls

    def sync_view(self, request):
        """Запускає повну синхронізацію CRM з усіх замовлень."""
        stats = {
            "orders": 0,
            "customers_created": 0,
            "customers_updated": 0,
            "orders_linked": 0,
            "skipped": 0,
        }
        for order in SalesOrder.objects.all():
            stats["orders"] += 1
            if not order.email and not order.client:
                stats["skipped"] += 1
                continue
            key = Customer.generate_key(
                order.email or order.client,
                order.client or order.email,
            )
            contact = getattr(order, 'contact_name', '') or ''
            client  = order.client or ''
            if contact:
                cust_name    = contact
                cust_company = client
            else:
                cust_name    = client or (order.email.split("@")[0] if order.email else "Unknown")
                cust_company = ''

            customer, created = Customer.objects.get_or_create(
                external_key=key,
                defaults={
                    "name":    cust_name,
                    "company": cust_company,
                    "email":   order.email or "",
                    "phone":   order.phone or "",
                    "country":      (order.addr_country or order.shipping_region or "")[:2],
                    "addr_street":  order.addr_street or "",
                    "addr_city":    order.addr_city or "",
                    "addr_zip":     order.addr_zip or "",
                    "addr_state":   order.addr_state or "",
                    "shipping_address": order.shipping_address or "",
                    "source": order.source,
                },
            )
            if created:
                stats["customers_created"] += 1
            else:
                updated = False
                if not customer.addr_street and order.addr_street:
                    customer.addr_street = order.addr_street
                    customer.addr_city   = order.addr_city
                    customer.addr_zip    = order.addr_zip
                    customer.addr_state  = order.addr_state or ""
                    updated = True
                elif not customer.shipping_address and order.shipping_address:
                    customer.shipping_address = order.shipping_address
                    updated = True
                if not customer.phone and order.phone:
                    customer.phone = order.phone
                    updated = True
                # Виправити company/name якщо contact_name став відомий
                if contact and not customer.company:
                    customer.name    = contact
                    customer.company = client
                    updated = True
                if updated:
                    customer.save()
                    stats["customers_updated"] += 1
            if order.customer_key != key:
                order.customer_key = key
                order.save(update_fields=["customer_key"])
                stats["orders_linked"] += 1

        messages.success(
            request,
            f"✅ Синхронізацію завершено! "
            f"Замовлень: {stats['orders']} | "
            f"Створено клієнтів: {stats['customers_created']} | "
            f"Оновлено: {stats['customers_updated']} | "
            f"Прив'язано: {stats['orders_linked']} | "
            f"Пропущено: {stats['skipped']}",
        )
        return redirect("admin:crm_customer_changelist")

    def get_queryset(self, request):
        """
        Анотації прибрані — всі дані рахуються через методи моделі.
        Швидкість: повільніше, але точно.
        """
        return super().get_queryset(request)

    fieldsets = (
        ("📋 Контактна інформація", {
            "fields": ("company", "name", "email", "phone"),
            "description": "<b>Компанія</b> — юридична назва або торгова марка. "
                           "<b>Контактна особа</b> — ім'я людини (FABIEN SANTOS тощо).",
        }),
        ("📬 Адреса доставки", {
            "fields": (
                ("addr_street",),
                ("addr_city", "addr_zip", "addr_state", "country"),
            )
        }),
        ("📋 Legacy адреса (raw)", {
            "fields": ("shipping_address",),
            "classes": ("collapse",),
            "description": "Оригінальний текстовий формат — збережено для сумісності",
        }),
        ("🎯 Сегментація", {
            "fields": ("segment", "status", "source", "notes")
        }),
        ("📊 Аналітика", {
            "fields": (
                "orders_count", "revenue_display", "avg_order_display",
                "last_order_display", "repeat_badge",
            )
        }),
        ("🛒 Топ товари", {
            "fields": ("top_products_display",),
        }),
        ("📜 Історія замовлень", {
            "fields": ("order_history_display",),
            "classes": ("collapse",),
        }),
        ("ℹ️ Метадані", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    # ── Computed columns ──────────────────────────────────────────────────────

    def display_name(self, obj):
        if obj.company:
            return format_html(
                '<span style="font-weight:600;color:#e3f2fd">{}</span>'
                '<br><span style="font-size:11px;color:#607d8b">{}</span>',
                obj.company, obj.name,
            )
        return format_html('<span style="color:#c9d8e4">{}</span>', obj.name)
    display_name.short_description = "Компанія / Контакт"
    display_name.admin_order_field = "company"

    def country_flag(self, obj):
        from config.country_utils import country_flag_html
        return format_html(country_flag_html(obj.country))
    country_flag.short_description = "Країна"
    country_flag.admin_order_field = "country"

    def segment_badge(self, obj):
        colors = {
            "b2b": "#1976d2", "b2c": "#388e3c",
            "distributor": "#f57c00", "reseller": "#7b1fa2", "other": "#757575"
        }
        color = colors.get(obj.segment, "#757575")
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:10px;font-size:11px;white-space:nowrap">{}</span>',
            color, obj.get_segment_display()
        )
    segment_badge.short_description = "Сегмент"
    segment_badge.admin_order_field = "segment"

    def status_badge(self, obj):
        colors = {
            "active": "#4caf50", "inactive": "#9e9e9e",
            "vip": "#ffd700", "blocked": "#f44336"
        }
        color = colors.get(obj.status, "#9e9e9e")
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:10px;font-size:11px;white-space:nowrap">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = "Статус"
    status_badge.admin_order_field = "status"

    def orders_count(self, obj):
        return obj.total_orders()
    orders_count.short_description = "Замовлень"
    # admin_order_field removed - no annotation

    def revenue_display(self, obj):
        """Виручка з розбивкою по валютах."""
        from sales.models import SalesOrderLine
        from django.db.models import Sum
        from django.utils.html import format_html
        
        # Групуємо по валюті
        lines = (
            SalesOrderLine.objects
            .filter(order__customer_key=obj.external_key)
            .values('currency')
            .annotate(total=Sum('total_price'))
            .order_by('-total')
        )
        
        if not lines:
            return "—"
        
        # Символи валют
        symbols = {'USD': '$', 'EUR': '€', 'GBP': '£'}
        
        parts = []
        for line in lines:
            curr = line['currency'] or 'USD'
            symbol = symbols.get(curr, curr)
            amount = line['total'] or 0
            parts.append(f"{symbol}{amount:,.2f}")
        
        return format_html('<b>{}</b>', ' + '.join(parts))

    revenue_display.short_description = "Виручка"
    
    def avg_order_display(self, obj):
        """Середній чек з валютою основної."""
        from sales.models import SalesOrder, SalesOrderLine
        from django.db.models import Sum
        from django.utils.html import format_html
        
        orders = SalesOrder.objects.filter(customer_key=obj.external_key)
        
        if not orders.exists():
            return "—"
        
        # Беремо валюту першого замовлення
        first_currency = orders.first().currency or 'USD'
        
        # Рахуємо загальну суму з SalesOrderLine
        total_revenue = (
            SalesOrderLine.objects
            .filter(
                order__customer_key=obj.external_key,
                currency=first_currency
            )
            .aggregate(total=Sum('total_price'))
        )['total']
        
        if not total_revenue:
            return "—"
        
        # Кількість замовлень
        order_count = orders.count()
        avg = float(total_revenue) / order_count
        
        symbols = {'USD': '$', 'EUR': '€', 'GBP': '£'}
        symbol = symbols.get(first_currency, first_currency)
        
        # Форматуємо число ПЕРЕД format_html
        formatted_amount = f"{avg:.2f}"
        
        return format_html('<b>{}{}</b>', symbol, formatted_amount)

    avg_order_display.short_description = "Середній чек"

    def last_order_display(self, obj):
        last = obj.last_order_date()
        if not last:
            return "—"
        days = obj.days_since_last_order()
        color = "#4caf50" if days and days <= 30 else (
            "#ff9800" if days and days <= 90 else "#f44336"
        )
        d = last.date() if hasattr(last, "date") else last
        return format_html(
            '<span style="color:{};white-space:nowrap"><b>{}</b></span>'
            '<br><small style="color:#999">({} дн. тому)</small>',
            color, d.strftime("%d.%m.%Y"), days or "?"
        )
    last_order_display.short_description = "Останнє замовлення"
    last_order_display.admin_order_field = "_last_order_date"

    def repeat_badge(self, obj):
        cnt = getattr(obj, "_orders_count", obj.total_orders())
        if cnt and int(cnt) > 1:
            return format_html('<span style="color:#4caf50;font-weight:bold;white-space:nowrap">✅ Постійний</span>')
        return format_html('<span style="color:#ff9800;white-space:nowrap">🔸 Новий</span>')
    repeat_badge.short_description = "Тип"
    repeat_badge.admin_order_field = "_orders_count"

    def rfm_display(self, obj):
        rfm = obj.rfm_score()
        score = rfm["score"]
        segment = rfm["segment"]
        if score >= 12:
            color = "#4caf50"
        elif score >= 8:
            color = "#ff9800"
        else:
            color = "#f44336"
        return format_html(
            '<div style="text-align:center;min-width:90px">'
            '<div style="font-size:10px;color:#aaa;white-space:nowrap">R:{R} F:{F} M:{M}</div>'
            '<div style="background:{color};color:#fff;padding:2px 8px;border-radius:10px;'
            'font-size:12px;font-weight:bold;display:inline-block;margin:2px 0">{score}/15</div>'
            '<div style="font-size:11px;margin-top:2px;white-space:nowrap">{segment}</div>'
            '</div>',
            **rfm, color=color
        )
    rfm_display.short_description = "RFM Аналіз"

    def top_products_display(self, obj):
        from sales.models import SalesOrderLine
        from django.db.models import Q
        lines = (
            SalesOrderLine.objects
            .filter(
                Q(order__email=obj.email) | Q(order__client__iexact=obj.name)
            )
            .values("sku_raw")  # sku_raw завжди є!
            .annotate(
                total_qty=Sum("qty"),
                total_revenue=Sum("total_price")
            )
            .order_by("-total_revenue")[:5]
        )
        if not lines:
            return "Немає даних"
        rows = "".join(
            f"<tr style='border-bottom:1px solid rgba(128,128,128,0.15)'>"
            f"<td style='padding:8px;opacity:0.6;text-align:center'>{i+1}</td>"
            f"<td style='padding:8px;font-weight:bold;color:#64b5f6'>{l['sku_raw'] or '—'}</td>"
            f"<td style='padding:8px;text-align:right'>{int(l['total_qty'] or 0)} шт.</td>"
            f"<td style='padding:8px;text-align:right;color:#4caf50;font-weight:700'>${float(l['total_revenue'] or 0):.2f}</td>"
            f"</tr>"
            for i, l in enumerate(lines)
        )
        return mark_safe(
            '<table style="border-collapse:collapse;font-size:12px;width:100%;'
            'border-radius:8px;overflow:hidden;border:1px solid rgba(128,128,128,0.2)">'
            '<thead><tr style="background:rgba(21,101,192,0.8);color:#e3f2fd">'
            '<th style="padding:10px 8px;text-align:center;font-weight:600">#</th>'
            '<th style="padding:10px 8px;text-align:left;font-weight:600">SKU</th>'
            '<th style="padding:10px 8px;text-align:right;font-weight:600">К-сть</th>'
            '<th style="padding:10px 8px;text-align:right;font-weight:600">💰 Сума</th>'
            '</tr></thead><tbody>' + rows + '</tbody></table>'
        )
    top_products_display.short_description = "Топ-5 товарів"

    def order_history_display(self, obj):
        from django.db.models import Q
        orders = SalesOrder.objects.filter(
            Q(email=obj.email) | Q(client__iexact=obj.name)
        ).order_by("-order_date")[:20]
        if not orders:
            return "Немає замовлень"
        rows = "".join(
            f"<tr style='border-bottom:1px solid rgba(128,128,128,0.15)'>"
            f"<td style='padding:8px;white-space:nowrap;opacity:0.85'>{o.order_date.strftime('%d.%m.%Y') if o.order_date else '—'}</td>"
            f"<td style='padding:8px;font-weight:bold'>{o.order_number}</td>"
            f"<td style='padding:8px;opacity:0.75'>{o.source}</td>"
            f"<td style='padding:8px;opacity:0.75'>{o.tracking_number or '—'}</td>"
            f"</tr>"
            for o in orders
        )
        return mark_safe(
            '<table style="border-collapse:collapse;font-size:12px;width:100%;'
            'border-radius:8px;overflow:hidden;border:1px solid rgba(128,128,128,0.2)">'
            '<thead><tr style="background:rgba(46,125,50,0.8);color:#e8f5e9">'
            '<th style="padding:8px;text-align:left">Дата</th>'
            '<th style="padding:8px;text-align:left">№ замовлення</th>'
            '<th style="padding:8px;text-align:left">Джерело</th>'
            '<th style="padding:8px;text-align:left">Tracking</th>'
            '</tr></thead><tbody>' + rows + '</tbody></table>'
        )
    order_history_display.short_description = "Останні 20 замовлень"


@admin.register(CustomerNote)
class CustomerNoteAdmin(admin.ModelAdmin):
    list_display = ("customer", "note_type", "subject", "created_at", "created_by")
    list_filter = ("note_type",)
    search_fields = ("customer__name", "subject", "body")


# ── CRM app_index stats ────────────────────────────────────────────────────────

def _get_crm_stats():
    try:
        from datetime import date, timedelta
        month_ago = date.today() - timedelta(days=30)
        total      = Customer.objects.count()
        new_month  = Customer.objects.filter(created_at__date__gte=month_ago).count()
        vip        = Customer.objects.filter(status="vip").count()
        countries  = (Customer.objects.exclude(country="")
                      .values("country").distinct().count())
        return {"total": total, "new_month": new_month, "vip": vip, "countries": countries}
    except Exception:
        return {"total": "—", "new_month": "—", "vip": "—", "countries": "—"}


_orig_crm_app_index = admin.site.app_index


def _crm_app_index(request, app_label, extra_context=None):
    if app_label == "crm":
        extra_context = extra_context or {}
        extra_context["crm_stats"] = _get_crm_stats()
    return _orig_crm_app_index(request, app_label, extra_context)


admin.site.app_index = _crm_app_index