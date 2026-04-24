from __future__ import annotations
from decimal import Decimal
from django.contrib import admin, messages
from django.db.models import Sum, Count, Max
from django.shortcuts import redirect, get_object_or_404
from django.urls import path
from django.utils.html import format_html
from django.utils.http import urlencode
from django.utils.safestring import mark_safe
from .models import Customer, CustomerNote
from sales.models import SalesOrder, SalesOrderLine
from core.mixins import AuditableMixin


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
class CustomerAdmin(AuditableMixin, admin.ModelAdmin):
    change_list_template = "admin/crm/customer_changelist.html"
    change_form_template = "admin/crm/customer/change_form.html"

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
        "display_name", "country_flag", "sources_display", "segment_badge",
        "status_badge", "orders_count", "revenue_display",
        "avg_order_display", "last_order_display",
        "repeat_badge", "rfm_display", "strategy_btn",
    )
    list_filter = ("segment", "status", "country", RepeatCustomerFilter, RFMSegmentFilter)
    search_fields = ("name", "email", "company", "phone", "addr_city", "addr_street", "external_key")
    # Зберігаємо фільтри між сесіями
    preserve_filters = True

    def get_search_results(self, request, queryset, search_term):
        """
        Розширений пошук: крім полів клієнта, шукає по:
        - № замовлення (order_number)
        - трекінг-номеру (tracking_number)
        - SKU товару в замовленні
        - коду клієнта (external_key, вже в search_fields)
        """
        qs, use_distinct = super().get_search_results(request, queryset, search_term)

        if not search_term or len(search_term) < 2:
            return qs, use_distinct

        term = search_term.strip()
        from django.db.models import Q

        # Пошук по полях замовлень → отримуємо customer_key
        order_keys = list(
            SalesOrder.objects
            .filter(
                Q(order_number__icontains=term) |
                Q(tracking_number__icontains=term)
            )
            .values_list("customer_key", flat=True)
            .distinct()
        )

        # Пошук по SKU в рядках замовлень
        sku_keys = list(
            SalesOrderLine.objects
            .filter(sku_raw__icontains=term)
            .values_list("order__customer_key", flat=True)
            .distinct()
        )

        all_keys = list({k for k in order_keys + sku_keys if k})
        if all_keys:
            extra = Customer.objects.filter(external_key__in=all_keys)
            qs = (qs | extra).distinct()
            use_distinct = True

        return qs, use_distinct
    readonly_fields = (
        "created_at", "updated_at",
        "orders_count", "revenue_display", "avg_order_display",
        "last_order_display", "repeat_badge",
        "top_products_display", "order_history_display",
        "strategy_btn", "upload_widget_customer",
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
            path(
                "<int:pk>/new-order/",
                self.admin_site.admin_view(self.new_order_view),
                name="crm_customer_new_order",
            ),
            path(
                "<int:pk>/assign-strategy/",
                self.admin_site.admin_view(self.assign_strategy_view),
                name="crm_customer_assign_strategy",
            ),
            path(
                "<int:pk>/assign-strategy/manual/",
                self.admin_site.admin_view(self.assign_strategy_manual_view),
                name="crm_customer_assign_strategy_manual",
            ),
            path(
                "<int:pk>/upload-docs/",
                self.admin_site.admin_view(self.upload_docs_view_customer),
                name="crm_customer_upload_docs",
            ),
            path(
                "<int:pk>/delete-doc/",
                self.admin_site.admin_view(self.delete_doc_view_customer),
                name="crm_customer_delete_doc",
            ),
        ]
        return custom + urls

    def assign_strategy_view(self, request, pk):
        """One-click: рекомендує шаблон → створює CustomerStrategy → відкриває canvas."""
        from strategy.models import StrategyTemplate, CustomerStrategy
        from strategy.services.engine import recommend_template_behavior, start_strategy

        customer = get_object_or_404(Customer, pk=pk)

        # Якщо вже є активна стратегія — редиректимо на неї
        existing = CustomerStrategy.objects.filter(
            customer=customer, status="active"
        ).first()
        if existing:
            messages.warning(
                request,
                f"⚠️ Клієнт вже має активну стратегію «{existing.template.name}». "
                f"Завершіть або призупиніть її перед призначенням нової.",
            )
            return redirect(f"/strategy/{existing.pk}/canvas/")

        behavior = recommend_template_behavior(customer)
        if not behavior:
            messages.error(request, "Не вдалося визначити рекомендовану стратегію.")
            return redirect("admin:crm_customer_change", pk)

        template = StrategyTemplate.objects.filter(
            behavior_type=behavior, is_active=True
        ).first()
        if not template:
            messages.error(
                request,
                f"Шаблон «{behavior}» не знайдено. "
                f"Запустіть: python manage.py create_strategy_templates",
            )
            return redirect("admin:crm_customer_change", pk)

        strategy = start_strategy(customer, template)
        messages.success(
            request,
            f"✅ Стратегія «{template.name}» призначена клієнту {customer.company or customer.name}.",
        )
        return redirect(f"/strategy/{strategy.pk}/canvas/")

    def assign_strategy_manual_view(self, request, pk):
        """Ручний вибір шаблону стратегії зі списку."""
        from strategy.models import StrategyTemplate, CustomerStrategy
        from strategy.services.engine import start_strategy

        customer = get_object_or_404(Customer, pk=pk)

        existing = CustomerStrategy.objects.filter(customer=customer, status="active").first()
        if existing:
            messages.warning(request,
                f"⚠️ Вже є активна стратегія «{existing.template.name}».")
            return redirect(f"/strategy/{existing.pk}/canvas/")

        if request.method == "POST":
            template_id = request.POST.get("template_id")
            template = get_object_or_404(StrategyTemplate, pk=template_id, is_active=True)
            strategy = start_strategy(customer, template)
            messages.success(request,
                f"✅ Стратегія «{template.name}» призначена клієнту {customer.company or customer.name}.")
            return redirect(f"/strategy/{strategy.pk}/canvas/")

        templates = StrategyTemplate.objects.filter(is_active=True).order_by("behavior_type", "name")
        ICONS = {"onboarding": "🚀", "reactivation": "🔄", "nurturing": "📈", "retention": "👑"}
        tmpl_html = "".join(
            f'<button type="submit" name="template_id" value="{t.pk}" '
            f'style="display:block;width:100%;text-align:left;background:var(--bg-card);'
            f'border:1px solid var(--border-strong);color:var(--text);padding:12px 16px;'
            f'border-radius:6px;cursor:pointer;font-size:14px;margin-bottom:8px">'
            f'{ICONS.get(t.behavior_type,"🎯")} <b>{t.name}</b> '
            f'<span style="opacity:.6;font-size:12px">({t.behavior_type})</span></button>'
            for t in templates
        ) or "<p>Немає активних шаблонів. Запустіть: manage.py create_strategy_templates</p>"

        html = (
            f'<div style="max-width:600px;padding:24px">'
            f'<h2 style="margin-bottom:4px">Вибір стратегії вручну</h2>'
            f'<p style="opacity:.7;margin-bottom:20px">Клієнт: <b>{customer.company or customer.name}</b></p>'
            f'<form method="post">'
            f'<input type="hidden" name="csrfmiddlewaretoken" value="{request.META.get("CSRF_COOKIE","")}">'
            f'{tmpl_html}'
            f'</form>'
            f'<a href="/admin/crm/customer/{pk}/change/" style="opacity:.6;font-size:12px">← Назад</a>'
            f'</div>'
        )
        from django.http import HttpResponse
        from django.middleware.csrf import get_token
        get_token(request)  # ensure CSRF cookie is set
        return HttpResponse(
            f'<!doctype html><html><head><meta charset="utf-8">'
            f'<link rel="stylesheet" href="/static/admin/css/base.css">'
            f'<title>Стратегія — {customer.company or customer.name}</title></head>'
            f'<body class="default">{html}</body></html>'
        )

    def new_order_view(self, request, pk):
        """Редирект на форму нового замовлення з pre-filled даними клієнта."""
        customer = get_object_or_404(Customer, pk=pk)
        params = {}
        if customer.company:
            params["_prefill_client"] = customer.company
            params["_prefill_contact_name"] = customer.name
        else:
            params["_prefill_client"] = customer.name
        if customer.email:
            params["_prefill_email"] = customer.email
        if customer.phone:
            params["_prefill_phone"] = customer.phone
        if customer.addr_street:
            params["_prefill_addr_street"] = customer.addr_street
        if customer.addr_city:
            params["_prefill_addr_city"] = customer.addr_city
        if customer.addr_zip:
            params["_prefill_addr_zip"] = customer.addr_zip
        if customer.addr_state:
            params["_prefill_addr_state"] = customer.addr_state
        if customer.country:
            params["_prefill_addr_country"] = customer.country
        if customer.shipping_address and not customer.addr_street:
            params["_prefill_shipping_address"] = customer.shipping_address
        add_url = "/admin/sales/salesorder/add/?" + urlencode(params)
        return redirect(add_url)

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
        ("🎯 Стратегія CRM", {
            "fields": ("strategy_btn",),
            "description": "Рекомендована стратегія на основі RFM-аналізу. "
                           "Один клік — і стратегія призначена.",
        }),
        ("📎 Документи", {
            "fields": ("upload_widget_customer",),
            "description": "Файли клієнта: договори, рахунки, листування. "
                           "Зберігаються у <code>media/customers/{pk}/</code>.",
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
        }),
        ("ℹ️ Метадані", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    # ── Computed columns ──────────────────────────────────────────────────────

    _SOURCE_COLORS = {
        'digikey':  '#c62828',  # DigiKey red
        'amazon':   '#e65100',  # Amazon deep orange
        'ebay':     '#1565c0',  # eBay blue
        'webshop':  '#2e7d32',  # Webshop green
        'nova_post':'#1565c0',  # Nova Post blue
        'manual':   '#455a64',  # Steel grey
    }

    def sources_display(self, obj):
        if not obj.external_key:
            src = obj.source or ''
            sources = [src] if src else []
        else:
            from sales.models import SalesOrder
            sources = list(
                SalesOrder.objects.filter(customer_key=obj.external_key)
                .values_list('source', flat=True).distinct()
            )
            if not sources:
                sources = [obj.source] if obj.source else []
        if not sources:
            return mark_safe('<span style="opacity:.4">—</span>')
        badges = []
        for src in sources:
            color = self._SOURCE_COLORS.get(src.lower(), '#455a64')
            badges.append(
                f'<span style="background:{color};color:#fff;padding:2px 8px;border-radius:10px;'
                f'font-size:11px;font-weight:700;white-space:nowrap;letter-spacing:.02em">'
                f'{src.upper()}</span>'
            )
        return mark_safe(' '.join(badges))
    sources_display.short_description = "Платформи"

    def display_name(self, obj):
        if obj.company:
            return format_html(
                '<span style="font-weight:600">{}</span>'
                '<br><span style="font-size:11px;opacity:.65">{}</span>',
                obj.company, obj.name,
            )
        return format_html('{}', obj.name)
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
        n = obj.total_orders()
        return mark_safe(
            f'<span style="font-size:16px;font-weight:800;letter-spacing:-.5px">{n}</span>'
        )
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
            '<br><small style="opacity:.55">({} дн. тому)</small>',
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

    def strategy_btn(self, obj):
        """Рекомендація стратегії + кнопка одного кліку."""
        from strategy.models import CustomerStrategy
        from strategy.services.engine import recommend_template_behavior

        LABELS = {
            "onboarding":   ("🚀", "Онбординг",       "#1976d2"),
            "reactivation": ("🔄", "Реактивація",      "#e53935"),
            "nurturing":    ("📈", "Нарощування",      "#f57c00"),
            "retention":    ("👑", "Утримання VIP",    "#7b1fa2"),
        }

        # Якщо вже є активна стратегія — показуємо посилання на canvas
        active = CustomerStrategy.objects.filter(
            customer=obj, status="active"
        ).select_related("template").first()
        if active:
            return format_html(
                '<a href="/strategy/{}/canvas/" '
                'style="background:#2e7d32;color:#fff;padding:3px 10px;'
                'border-radius:5px;font-size:11px;text-decoration:none;white-space:nowrap">'
                '⚡ {}</a>',
                active.pk,
                (active.template.name[:22] + "…") if len(active.template.name) > 22
                else active.template.name,
            )

        behavior = recommend_template_behavior(obj)
        manual_url = f"/admin/crm/customer/{obj.pk}/assign-strategy/manual/"
        if not behavior:
            return format_html(
                '<a href="{}" '
                'style="background:#455a64;color:#fff;padding:3px 10px;'
                'border-radius:5px;font-size:11px;text-decoration:none;white-space:nowrap">'
                '✏️ Визначити вручну</a>',
                manual_url,
            )

        icon, label, color = LABELS.get(behavior, ("🎯", behavior, "#607d8b"))
        assign_url = f"/admin/crm/customer/{obj.pk}/assign-strategy/"
        return format_html(
            '<a href="{}" '
            'style="background:{};color:#fff;padding:3px 10px;'
            'border-radius:5px;font-size:11px;text-decoration:none;white-space:nowrap">'
            '{} {}</a>',
            assign_url, color, icon, label,
        )
    strategy_btn.short_description = "🎯 Стратегія"

    def upload_widget_customer(self, obj):
        """HTML-віджет для завантаження документів клієнта."""
        if not obj.pk:
            return format_html('<em style="color:#7d8590">Збережіть клієнта спочатку</em>')

        from django.urls import reverse
        upload_url = reverse('admin:crm_customer_upload_docs', args=[obj.pk])

        # List existing files
        from django.conf import settings
        from django.urls import reverse as _rev
        from pathlib import Path
        delete_url = _rev('admin:crm_customer_delete_doc', args=[obj.pk])
        customer_dir = Path(settings.MEDIA_ROOT) / 'customers' / str(obj.pk)
        files_html = ''
        if customer_dir.exists():
            files = sorted(customer_dir.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
            if files:
                _btn = (
                    'padding:3px 9px;border-radius:4px;font-size:11px;font-weight:600;'
                    'cursor:pointer;border:none;white-space:nowrap'
                )
                rows = ''.join(
                    f'<div class="cust-doc-row" style="display:flex;align-items:center;gap:8px;'
                    f'padding:6px 0;border-bottom:1px solid rgba(128,128,128,.1)">'
                    f'<span style="font-size:14px;flex-shrink:0">📄</span>'
                    f'<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'
                    f'color:var(--text);font-size:13px" title="{f.name}">{f.name}</span>'
                    f'<span style="font-size:11px;color:var(--text-muted);white-space:nowrap;flex-shrink:0">'
                    f'{f.stat().st_size // 1024 or "<1"} KB</span>'
                    f'<a href="{settings.MEDIA_URL}customers/{obj.pk}/{f.name}" download '
                    f'style="{_btn};background:rgba(21,101,192,.15);color:#42a5f5;text-decoration:none">'
                    f'💾 Зберегти</a>'
                    f'<button type="button" style="{_btn};background:rgba(229,57,53,.12);color:#ef5350" '
                    f'onclick="custDocDelete(this,\'{delete_url}\',\'{f.name}\')">'
                    f'🗑️ Видалити</button>'
                    f'</div>'
                    for f in files
                )
                files_html = (
                    f'<div id="custDocList" style="margin-top:10px;padding:8px 12px;'
                    f'background:var(--bg-input,#141f2b);border-radius:6px;'
                    f'border:1px solid var(--border-strong)">'
                    f'<div style="font-size:11px;font-weight:700;color:var(--text-muted);'
                    f'text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px">'
                    f'📂 Файли клієнта ({len(files)} шт.)</div>'
                    f'{rows}</div>'
                )

        widget_html = (
            f'<div id="custDocWidget" data-upload-url="{upload_url}">'
            f'<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">'
            f'<label style="display:inline-flex;align-items:center;gap:8px;padding:7px 16px;'
            f'background:#1565c0;color:#fff;border-radius:6px;cursor:pointer;'
            f'font-size:13px;font-weight:600;white-space:nowrap">'
            f'📎 Вибрати файли'
            f'<input type="file" multiple style="display:none" id="custDocInput">'
            f'</label>'
            f'<span id="custDocStatus" style="font-size:12px;color:var(--text-muted)">Оберіть файли для завантаження</span>'
            f'</div>'
            f'<div id="custDocProgress" style="display:none;margin-top:8px">'
            f'<div style="height:4px;background:var(--border);border-radius:2px;overflow:hidden">'
            f'<div id="custDocBar" style="height:100%;background:#1565c0;width:0;transition:width .3s"></div>'
            f'</div>'
            f'</div>'
            f'{files_html}'
            f'</div>'
            f'<script>'
            f'(function(){{'
            f'var inp=document.getElementById("custDocInput");'
            f'if(!inp)return;'
            f'inp.addEventListener("change",function(){{'
            f'var files=Array.from(this.files);'
            f'if(!files.length)return;'
            f'var url=document.getElementById("custDocWidget").dataset.uploadUrl;'
            f'var fd=new FormData();'
            f'files.forEach(function(f){{fd.append("documents",f);}});'
            f'fd.append("csrfmiddlewaretoken",document.cookie.match(/csrftoken=([^;]+)/)?.[1]||"");'
            f'document.getElementById("custDocProgress").style.display="block";'
            f'document.getElementById("custDocBar").style.width="30%";'
            f'document.getElementById("custDocStatus").textContent="Завантаження...";'
            f'fetch(url,{{method:"POST",body:fd}})'
            f'.then(function(r){{return r.json();}})'
            f'.then(function(d){{'
            f'document.getElementById("custDocBar").style.width="100%";'
            f'document.getElementById("custDocStatus").textContent='
            f'(d.saved||0)+" файл(ів) збережено";'
            f'setTimeout(function(){{location.reload();}},800);'
            f'}})'
            f'.catch(function(){{'
            f'document.getElementById("custDocStatus").textContent="Помилка завантаження";'
            f'}});'
            f'}});'
            f'}})()'
            f'</script>'
            f'<script>'
            f'function custDocDelete(btn,url,fname){{'
            f'if(!confirm("Видалити файл \\""+fname+"\\"?"))return;'
            f'var fd=new FormData();'
            f'fd.append("filename",fname);'
            f'fd.append("csrfmiddlewaretoken",document.cookie.match(/csrftoken=([^;]+)/)?.[1]||"");'
            f'btn.disabled=true;btn.textContent="...";'
            f'fetch(url,{{method:"POST",body:fd}})'
            f'.then(function(r){{return r.json();}})'
            f'.then(function(d){{'
            f'if(d.ok){{btn.closest(".cust-doc-row").remove();}}'
            f'else{{btn.disabled=false;btn.textContent="❌ Помилка";}}'
            f'}})'
            f'.catch(function(){{btn.disabled=false;btn.textContent="❌";}});'
            f'}}'
            f'</script>'
        )
        return mark_safe(widget_html)
    upload_widget_customer.short_description = "📎 Документи клієнта"

    def upload_docs_view_customer(self, request, pk):
        """AJAX: зберігає документи у media/customers/{pk}/"""
        from django.http import JsonResponse
        from django.conf import settings
        from pathlib import Path

        if request.method != 'POST':
            return JsonResponse({'error': 'Method not allowed'}, status=405)

        customer = get_object_or_404(Customer, pk=pk)
        files = request.FILES.getlist('documents')
        if not files:
            return JsonResponse({'error': 'Файли не вибрані'}, status=400)

        dest_dir = Path(settings.MEDIA_ROOT) / 'customers' / str(pk)
        dest_dir.mkdir(parents=True, exist_ok=True)

        results = []
        for f in files:
            try:
                dest = dest_dir / f.name
                with dest.open('wb+') as fh:
                    for chunk in f.chunks():
                        fh.write(chunk)
                results.append({'name': f.name, 'status': 'saved', 'size': f.size})
            except Exception as e:
                results.append({'name': f.name, 'status': 'error', 'error': str(e)})

        saved = sum(1 for r in results if r['status'] == 'saved')
        return JsonResponse({'saved': saved, 'results': results})

    def delete_doc_view_customer(self, request, pk):
        """AJAX: видаляє один документ клієнта."""
        from django.http import JsonResponse
        from django.conf import settings
        from pathlib import Path
        import os

        if request.method != 'POST':
            return JsonResponse({'error': 'Method not allowed'}, status=405)

        filename = request.POST.get('filename', '').strip()
        # Security: no path traversal
        if not filename or os.sep in filename or '/' in filename or '..' in filename:
            return JsonResponse({'error': 'Invalid filename'}, status=400)

        dest = Path(settings.MEDIA_ROOT) / 'customers' / str(pk) / filename
        if not dest.exists():
            return JsonResponse({'error': 'Файл не знайдено'}, status=404)

        try:
            dest.unlink()
            return JsonResponse({'ok': True})
        except Exception as e:
            return JsonResponse({'ok': False, 'error': str(e)}, status=500)

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
        from sales.models import SalesOrderLine
        orders = list(SalesOrder.objects.filter(
            Q(email=obj.email) | Q(customer_key=obj.external_key)
        ).order_by("-order_date")[:20])
        if not orders:
            return "Немає замовлень"
        # Batch-load lines for all orders (1 query instead of N)
        order_ids = [o.pk for o in orders]
        lines_map: dict = {}
        for line in SalesOrderLine.objects.filter(order_id__in=order_ids).select_related('product'):
            lines_map.setdefault(line.order_id, []).append(line)

        _LA = "color:inherit;text-decoration:underline dotted;text-underline-offset:2px"

        def _tracking_url(courier, tn):
            if not tn:
                return None
            cr = (courier or '').lower()
            if 'ups' in cr or tn.startswith('1Z'):
                return f'https://www.ups.com/track?tracknum={tn}&loc=en_US'
            if 'dhl' in cr:
                return f'https://nolp.dhl.de/nextt-online-public/set_identcodes.do?idc={tn}'
            if 'fedex' in cr:
                return f'https://www.fedex.com/fedextrack/?trknbr={tn}'
            if 'dpd' in cr:
                return f'https://www.dpd.com/tracking/?parcelNo={tn}'
            if 'gls' in cr:
                return f'https://gls-group.eu/track/{tn}'
            if 'post' in cr:
                return f'https://www.deutschepost.de/sendung/simpleQueryResult.html?form.sendungsnummer={tn}'
            if 'nova' in cr:
                return f'https://novaposhta.ua/tracking/?cargo_number={tn}'
            return None

        STATUS_BADGE = {
            'received':   ('📥', '#455a64', 'Отримано'),
            'processing': ('⚙️', '#e65100', 'В обробці'),
            'shipped':    ('🚚', '#1565c0', 'Відправлено'),
            'delivered':  ('✅', '#2e7d32', 'Доставлено'),
            'cancelled':  ('🚫', '#c62828', 'Скасовано'),
        }
        TD  = "padding:7px 10px;font-size:13px"
        TDB = TD + ";font-weight:700"
        rows = []
        for o in orders:
            order_url = f'/admin/sales/salesorder/{o.pk}/change/'
            lines = lines_map.get(o.pk, [])
            if lines:
                skus_parts = []
                qtys_parts = []
                for l in lines[:5]:
                    sku = l.sku_raw or (l.product.sku if l.product else '?')
                    q   = l.qty
                    qty_str = str(int(q)) if q == int(q) else str(q)
                    if l.product_id:
                        prod_url = f'/admin/inventory/product/{l.product_id}/change/'
                        skus_parts.append(f'<a href="{prod_url}" style="{_LA}"><b>{sku}</b></a>')
                    else:
                        skus_parts.append(f'<b>{sku}</b>')
                    qtys_parts.append(f'<b>{qty_str}</b>')
                if len(lines) > 5:
                    skus_parts.append(f'<span style="opacity:.5">+{len(lines)-5}</span>')
                    qtys_parts.append('<span style="opacity:.5">…</span>')
                skus_html = '<br>'.join(skus_parts)
                qtys_html = '<br>'.join(qtys_parts)
                total     = sum((l.total_price or 0) for l in lines)
                total_html = f'<b>{total:.2f}</b>&nbsp;{o.currency}' if total else '—'
            else:
                skus_html = qtys_html = total_html = '—'
            ship_cost = ''
            if o.shipping_cost:
                ship_cost = f'{o.shipping_cost:.2f}&nbsp;{o.shipping_currency}'
            tn = o.tracking_number or ''
            track_url = _tracking_url(o.shipping_courier, tn)
            tn_html = (f'<a href="{track_url}" target="_blank" style="{_LA}">{tn}</a>'
                       if track_url else (tn or '—'))
            st = o.status or ''
            st_icon, st_color, st_label = STATUS_BADGE.get(st, ('❓', '#607d8b', st or '—'))
            status_html = (
                f'<span style="display:inline-flex;align-items:center;gap:4px;padding:2px 8px;'
                f'border-radius:12px;font-size:11px;font-weight:600;white-space:nowrap;'
                f'background:{st_color}22;color:{st_color};border:1px solid {st_color}55">'
                f'{st_icon} {st_label}</span>'
            )
            rows.append(
                f"<tr style='border-bottom:1px solid rgba(128,128,128,0.15)'>"
                f"<td style='{TD};white-space:nowrap;opacity:0.85'>{o.order_date.strftime('%d.%m.%Y') if o.order_date else '—'}</td>"
                f"<td style='{TDB};white-space:nowrap'><a href='{order_url}' style='{_LA}'>{o.order_number}</a></td>"
                f"<td style='{TD};opacity:0.75'>{o.source}</td>"
                f"<td style='{TD};font-family:monospace'>{skus_html}</td>"
                f"<td style='{TD};text-align:right'>{qtys_html}</td>"
                f"<td style='{TD};white-space:nowrap;text-align:right'>{total_html}</td>"
                f"<td style='{TD};font-family:monospace;font-size:11px'>{tn_html}</td>"
                f"<td style='{TD};white-space:nowrap;text-align:right;opacity:0.85'>{ship_cost or '—'}</td>"
                f"<td style='{TD};opacity:0.75;white-space:nowrap'>{o.shipping_courier or '—'}</td>"
                f"<td style='{TD};white-space:nowrap'>{status_html}</td>"
                f"</tr>"
            )
        TH = "padding:7px 10px;text-align:left;font-size:12px"
        return mark_safe(
            '<table style="border-collapse:collapse;font-size:13px;width:100%;'
            'border-radius:8px;overflow:hidden;border:1px solid rgba(128,128,128,0.2)">'
            f'<thead><tr style="background:rgba(46,125,50,0.85);color:#e8f5e9">'
            f'<th style="{TH}">Дата</th>'
            f'<th style="{TH}">№ замовлення</th>'
            f'<th style="{TH}">Джерело</th>'
            f'<th style="{TH}">Товари</th>'
            f'<th style="{TH};text-align:right">К-сть</th>'
            f'<th style="{TH};text-align:right">Сума</th>'
            f'<th style="{TH}">Tracking</th>'
            f'<th style="{TH};text-align:right">Доставка</th>'
            f'<th style="{TH}">Перевізник</th>'
            f'<th style="{TH}">Статус</th>'
            '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table>'
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
        from django.db.models import Min
        from sales.models import SalesOrder
        month_ago = date.today().replace(day=1)  # початок поточного місяця
        total      = Customer.objects.count()
        # Нові = клієнти, чиє ПЕРШЕ замовлення було в поточному місяці
        new_keys = (SalesOrder.objects
                    .filter(customer_key__isnull=False)
                    .exclude(customer_key='')
                    .values('customer_key')
                    .annotate(first_order=Min('order_date'))
                    .filter(first_order__gte=month_ago)
                    .values_list('customer_key', flat=True))
        new_month  = Customer.objects.filter(external_key__in=new_keys).count()
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