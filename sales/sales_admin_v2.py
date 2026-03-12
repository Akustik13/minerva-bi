from django.contrib import admin
from django.utils.html import format_html
from .models import SalesOrder, SalesOrderLine


class SalesOrderLineInline(admin.TabularInline):
    model = SalesOrderLine
    extra = 0
    readonly_fields = ("total_price",)
    fields = ("product", "sku_raw", "qty", "unit_price", "total_price")


def export_sales_excel(modeladmin, request, queryset):
    """Експорт продажів в Excel."""
    try:
        from exports import export_sales
        return export_sales(queryset)
    except ImportError:
        from django.contrib import messages
        messages.error(request, "Модуль exports.py не знайдено")

export_sales_excel.short_description = "📥 Експортувати в Excel"


@admin.register(SalesOrder)
class SalesOrderAdmin(admin.ModelAdmin):
    list_display = (
        "order_date_fmt", "order_number", "source",
        "client", "email", "shipping_region",
        "tracking_number", "shipped_at",
        "items_count", "order_total",
        "customer_link",
    )
    search_fields = ("order_number", "tracking_number", "client", "email")
    list_filter   = ("source", "document_type", "shipping_region")
    date_hierarchy = "order_date"
    actions = [export_sales_excel]
    inlines = [SalesOrderLineInline]

    def order_date_fmt(self, obj):
        if not obj.order_date:
            return "—"
        d = obj.order_date
        return d.strftime("%d.%m.%Y") if hasattr(d, "strftime") else str(d)
    order_date_fmt.short_description = "Дата"
    order_date_fmt.admin_order_field = "order_date"

    def items_count(self, obj):
        return obj.lines.count()
    items_count.short_description = "Позицій"

    def order_total(self, obj):
        from django.db.models import Sum
        total = obj.lines.aggregate(t=Sum("total_price"))["t"]
        return f"€{float(total):.2f}" if total else "—"
    order_total.short_description = "Сума"

    def customer_link(self, obj):
        if not obj.customer:
            return "—"
        from django.urls import reverse
        url = reverse("admin:crm_customer_change", args=[obj.customer.pk])
        return format_html('<a href="{}">👤 {}</a>', url, obj.customer.name)
    customer_link.short_description = "Клієнт CRM"
