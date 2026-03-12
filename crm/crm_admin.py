from __future__ import annotations
from decimal import Decimal
from django.contrib import admin
from django.db.models import Sum, Count, Max
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from .models import Customer, CustomerNote


class CustomerNoteInline(admin.TabularInline):
    model = CustomerNote
    extra = 1
    fields = ("note_type", "subject", "body", "created_at", "created_by")
    readonly_fields = ("created_at",)


class RepeatCustomerFilter(admin.SimpleListFilter):
    title = "Повторні клієнти"
    parameter_name = "repeat"

    def lookups(self, request, model_admin):
        return [("yes", "Так (>1 замовлення)"), ("no", "Ні (1 замовлення)")]

    def queryset(self, request, queryset):
        from django.db.models import Count
        qs = queryset.annotate(order_count=Count("sales_orders"))
        if self.value() == "yes":
            return qs.filter(order_count__gt=1)
        if self.value() == "no":
            return qs.filter(order_count=1)
        return queryset


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = (
        "name", "email", "country_flag", "segment_badge",
        "status_badge", "orders_count", "revenue_display",
        "avg_order_display", "last_order_display",
        "repeat_badge", "rfm_display",
    )
    list_filter  = ("segment", "status", "country", RepeatCustomerFilter)
    search_fields = ("name", "email", "company", "phone")
    # search_fields потрібно для autocomplete_fields в інших адмінах
    readonly_fields = (
        "created_at", "updated_at",
        "orders_count", "revenue_display", "avg_order_display",
        "last_order_display", "rfm_display", "repeat_badge",
        "top_products_display", "order_history_display",
    )
    inlines = [CustomerNoteInline]

    fieldsets = (
        ("📋 Контактна інформація", {
            "fields": ("name", "email", "phone", "company", "country", "shipping_address")
        }),
        ("🎯 Сегментація", {
            "fields": ("segment", "status", "source", "notes")
        }),
        ("📊 Аналітика", {
            "fields": (
                "orders_count", "revenue_display", "avg_order_display",
                "last_order_display", "repeat_badge", "rfm_display",
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

    # ---- Computed columns ----

    def country_flag(self, obj):
        c = (obj.country or "").upper()
        flags = {"USA": "🇺🇸", "UKR": "🇺🇦", "DEU": "🇩🇪",
                 "GBR": "🇬🇧", "POL": "🇵🇱", "CAN": "🇨🇦",
                 "AUS": "🇦🇺", "FRA": "🇫🇷", "NLD": "🇳🇱"}
        return f"{flags.get(c, '🌍')} {c}" if c else "—"
    country_flag.short_description = "Країна"

    def segment_badge(self, obj):
        colors = {
            "b2b": "#1976d2", "b2c": "#388e3c",
            "distributor": "#f57c00", "reseller": "#7b1fa2", "other": "#757575"
        }
        color = colors.get(obj.segment, "#757575")
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;border-radius:10px;font-size:11px">{}</span>',
            color, obj.get_segment_display()
        )
    segment_badge.short_description = "Сегмент"

    def status_badge(self, obj):
        colors = {
            "active": "#4caf50", "inactive": "#9e9e9e",
            "vip": "#ffd700", "blocked": "#f44336"
        }
        color = colors.get(obj.status, "#9e9e9e")
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;border-radius:10px;font-size:11px">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = "Статус"

    def orders_count(self, obj):
        return obj.total_orders()
    orders_count.short_description = "Замовлень"

    def revenue_display(self, obj):
        rev = obj.total_revenue()
        return f"€{rev:,.2f}" if rev else "—"
    revenue_display.short_description = "Виручка"

    def avg_order_display(self, obj):
        avg = obj.avg_order_value()
        return f"€{avg:,.2f}" if avg else "—"
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
            '<span style="color:{}">{}</span> <small style="color:#999">({} дн. тому)</small>',
            color, d.strftime("%d.%m.%Y"), days or "?"
        )
    last_order_display.short_description = "Останнє замовлення"

    def repeat_badge(self, obj):
        if obj.is_repeat_customer():
            return format_html('<span style="color:#4caf50;font-weight:bold">✅ Постійний</span>')
        return format_html('<span style="color:#ff9800">🔸 Новий</span>')
    repeat_badge.short_description = "Тип клієнта"

    def rfm_display(self, obj):
        rfm = obj.rfm_score()
        score = rfm["score"]
        color = "#4caf50" if score >= 12 else ("#ff9800" if score >= 8 else "#f44336")
        return format_html(
            'R:<b>{R}</b> F:<b>{F}</b> M:<b>{M}</b> → '
            '<span style="background:{color};color:#fff;padding:2px 8px;border-radius:10px">{score}/15</span>',
            **rfm, color=color
        )
    rfm_display.short_description = "RFM оцінка"

    def top_products_display(self, obj):
        from sales.models import SalesOrderLine
        from django.db.models import Sum
        lines = (
            SalesOrderLine.objects
            .filter(order__customer=obj)
            .values("product__sku", "product__name")
            .annotate(total_qty=Sum("qty"), total_val=Sum("total_price"))
            .order_by("-total_qty")[:5]
        )
        if not lines:
            return "Немає даних"
        rows = "".join(
            f"<tr><td>{i+1}</td><td><b>{l['product__sku']}</b></td>"
            f"<td>{l['total_qty']}</td>"
            f"<td>€{float(l['total_val'] or 0):.2f}</td></tr>"
            for i, l in enumerate(lines)
        )
        return mark_safe(
            '<table style="border-collapse:collapse;font-size:12px">'
            '<tr><th>#</th><th>SKU</th><th>Кількість</th><th>Сума</th></tr>'
            + rows + '</table>'
        )
    top_products_display.short_description = "Топ-5 товарів"

    def order_history_display(self, obj):
        orders = obj.sales_orders.order_by("-order_date")[:20]
        if not orders:
            return "Немає замовлень"
        rows = "".join(
            f"<tr>"
            f"<td>{o.order_date.strftime('%d.%m.%Y') if o.order_date else '—'}</td>"
            f"<td><b>{o.order_number}</b></td>"
            f"<td>{o.source}</td>"
            f"<td>{o.tracking_number or '—'}</td>"
            f"</tr>"
            for o in orders
        )
        return mark_safe(
            '<table style="border-collapse:collapse;font-size:12px">'
            '<tr><th>Дата</th><th>№ замовлення</th><th>Джерело</th><th>Tracking</th></tr>'
            + rows + '</table>'
        )
    order_history_display.short_description = "Останні 20 замовлень"


@admin.register(CustomerNote)
class CustomerNoteAdmin(admin.ModelAdmin):
    list_display = ("customer", "note_type", "subject", "created_at", "created_by")
    list_filter = ("note_type",)
    search_fields = ("customer__name", "subject", "body")
    autocomplete_fields = ("customer",)
