from __future__ import annotations

from decimal import Decimal

from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils.html import format_html

from .models import (
    CompanySettings, Invoice, InvoiceLine,
    Payment, Expense, ExpenseCategory,
)


# ── CompanySettings (Singleton) ────────────────────────────────────────────────

@admin.register(CompanySettings)
class CompanySettingsAdmin(admin.ModelAdmin):
    readonly_fields = ("next_number",)
    fieldsets = (
        ("🏢 Компанія", {
            "fields": ("name", "legal_name", "logo")
        }),
        ("📍 Адреса", {
            "fields": ("addr_street", ("addr_city", "addr_zip", "addr_country"))
        }),
        ("💳 Реквізити", {
            "fields": ("vat_id", "iban", "swift", "bank_name")
        }),
        ("📞 Контакти", {
            "fields": ("email", "phone")
        }),
        ("🔢 Нумерація рахунків", {
            "fields": ("invoice_prefix", "next_number"),
            "description": "Рахунки нумеруються: <prefix>-<рік>-<номер>. "
                           "Наступний номер збільшується автоматично."
        }),
    )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "",
                self.admin_site.admin_view(self.singleton_redirect),
                name="accounting_companysettings_changelist",
            ),
        ]
        return custom + urls

    def singleton_redirect(self, request):
        """Перенаправляємо список → єдиний об'єкт (pk=1)."""
        obj = CompanySettings.get()
        return HttpResponseRedirect(
            reverse("admin:accounting_companysettings_change", args=[obj.pk])
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ── InvoiceLine Inline ─────────────────────────────────────────────────────────

class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 1
    autocomplete_fields = ("product",)
    fields = ("description", "product", "quantity", "unit", "unit_price", "discount", "line_total_display")
    readonly_fields = ("line_total_display",)

    def line_total_display(self, obj):
        if obj.pk:
            return format_html("<b>{}</b>", f"{obj.line_total:.2f}")
        return "—"
    line_total_display.short_description = "Сума"


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = ("date", "amount", "method", "notes")


# ── Invoice ────────────────────────────────────────────────────────────────────

@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    change_form_template = "accounting/invoice_change_form.html"

    list_display = (
        "number", "client_name", "status_badge", "currency",
        "issue_date", "due_date", "total_display", "paid_display", "balance_display",
    )
    list_filter  = ("status", "currency", "issue_date")
    search_fields = ("number", "client_name", "client_vat")
    date_hierarchy = "issue_date"
    readonly_fields = ("number", "created_at", "subtotal_display",
                       "vat_display", "total_display_ro", "paid_display_ro",
                       "balance_display_ro", "pdf_button")
    autocomplete_fields = ("customer", "order")
    inlines = (InvoiceLineInline, PaymentInline)

    fieldsets = (
        ("📄 Рахунок", {
            "fields": ("number", "status", ("issue_date", "service_date", "due_date"),
                       ("currency", "vat_rate"))
        }),
        ("🔗 Прив'язка", {
            "fields": ("customer", "order"),
            "classes": ("collapse",),
        }),
        ("👤 Клієнт (snapshot)", {
            "fields": ("client_name", "client_addr", "client_vat"),
            "description": "Ці поля зберігаються як знімок і не змінюються "
                           "при оновленні картки клієнта.",
        }),
        ("💰 Підсумки та дії", {
            "fields": ("subtotal_display", "vat_display",
                       "total_display_ro", "paid_display_ro", "balance_display_ro",
                       "pdf_button"),
        }),
        ("📝 Примітки", {
            "fields": ("notes", "created_at"),
        }),
    )

    # ── Computed list columns ─────────────────────────────────────────────────

    def status_badge(self, obj):
        colors_map = {
            "draft":     "#607d8b",
            "sent":      "#2196f3",
            "paid":      "#4caf50",
            "overdue":   "#f44336",
            "cancelled": "#9e9e9e",
        }
        color = colors_map.get(obj.status, "#607d8b")
        return format_html(
            '<span style="background:{};color:#fff;padding:3px 10px;border-radius:12px;'
            'font-size:11px;font-weight:bold;white-space:nowrap">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = "Статус"
    status_badge.admin_order_field = "status"

    def total_display(self, obj):
        return format_html("<b>{} {}</b>", f"{obj.total:.2f}", obj.currency)
    total_display.short_description = "Всього"

    def paid_display(self, obj):
        p = obj.paid_amount
        if p == 0:
            return format_html('<span style="opacity:.4">—</span>')
        return format_html('<span style="color:#4caf50">{}</span>', f"{p:.2f}")
    paid_display.short_description = "Сплачено"

    def balance_display(self, obj):
        b = obj.balance_due
        if b <= 0:
            return format_html('<span style="color:#4caf50">✅ 0</span>')
        return format_html('<span style="color:#f44336;font-weight:bold">{}</span>', f"{b:.2f}")
    balance_display.short_description = "До сплати"

    # ── Readonly в формі ──────────────────────────────────────────────────────

    def subtotal_display(self, obj):
        if obj.pk:
            return format_html("{} {}", f"{obj.subtotal:.2f}", obj.currency)
        return "—"
    subtotal_display.short_description = "Subtotal"

    def vat_display(self, obj):
        if obj.pk and obj.vat_rate:
            return format_html("{} {} ({}%)", f"{obj.vat_amount:.2f}", obj.currency, obj.vat_rate)
        return "—"
    vat_display.short_description = "VAT"

    def total_display_ro(self, obj):
        if obj.pk:
            return format_html("<b>{} {}</b>", f"{obj.total:.2f}", obj.currency)
        return "—"
    total_display_ro.short_description = "TOTAL"

    def paid_display_ro(self, obj):
        if obj.pk:
            return format_html("{} {}", f"{obj.paid_amount:.2f}", obj.currency)
        return "—"
    paid_display_ro.short_description = "Сплачено"

    def balance_display_ro(self, obj):
        if obj.pk:
            b = obj.balance_due
            color = "#4caf50" if b <= 0 else "#f44336"
            return format_html(
                '<span style="color:{};font-weight:bold">{} {}</span>',
                color, f"{b:.2f}", obj.currency
            )
        return "—"
    balance_display_ro.short_description = "До сплати"

    def pdf_button(self, obj):
        if not obj.pk:
            return format_html(
                '<span style="color:#607d8b;font-size:12px">'
                'Збережіть рахунок, щоб завантажити PDF</span>'
            )
        url = f"/accounting/invoice/{obj.pk}/pdf/"
        return format_html(
            '<a href="{}" target="_blank" style="'
            'display:inline-block;padding:10px 24px;'
            'background:linear-gradient(135deg,#1565c0,#1976d2);'
            'color:#fff;border-radius:8px;text-decoration:none;'
            'font-weight:700;font-size:14px;letter-spacing:.3px;'
            'box-shadow:0 2px 8px rgba(25,118,210,.4);'
            'transition:opacity .2s" '
            'onmouseover="this.style.opacity=\'.85\'" '
            'onmouseout="this.style.opacity=\'1\'">'
            '📄&nbsp; Завантажити PDF</a>',
            url
        )
    pdf_button.short_description = "PDF-рахунок"

    # ── Snapshot клієнта при збереженні ──────────────────────────────────────

    def save_model(self, request, obj, form, change):
        if obj.customer and not obj.client_name:
            c = obj.customer
            obj.client_name = c.name or ""
            addr_parts = []
            if hasattr(c, "addr_street") and c.addr_street:
                addr_parts.append(c.addr_street)
            city_line = " ".join(filter(None, [
                getattr(c, "addr_zip", ""),
                getattr(c, "addr_city", ""),
            ]))
            if city_line:
                addr_parts.append(city_line)
            if c.country:
                addr_parts.append(c.country)
            obj.client_addr = "\n".join(addr_parts)
        super().save_model(request, obj, form, change)

    # ── Pre-fill при створенні з SalesOrder ───────────────────────────────────

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        try:
            from config.models import SystemSettings
            initial["vat_rate"] = SystemSettings.get().default_vat_rate
        except Exception:
            pass
        order_id = request.GET.get("order")
        if order_id:
            try:
                from sales.models import SalesOrder
                order = SalesOrder.objects.select_related("customer_obj").get(pk=order_id)
                initial["order"] = order.pk
                if hasattr(order, "customer_obj") and order.customer_obj:
                    initial["customer"] = order.customer_obj.pk
                initial["client_name"] = order.client or ""
                # Складаємо snapshot адреси з addr_* полів замовлення
                addr_parts = []
                if order.addr_street:
                    addr_parts.append(order.addr_street)
                city_line = " ".join(filter(None, [order.addr_zip, order.addr_city]))
                if city_line:
                    addr_parts.append(city_line)
                if order.addr_country:
                    addr_parts.append(order.addr_country)
                initial["client_addr"] = "\n".join(addr_parts)
                initial["currency"] = "EUR"
            except Exception:
                pass
        return initial


# ── ExpenseCategory ────────────────────────────────────────────────────────────

@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    list_display  = ("name", "parent")
    search_fields = ("name",)


# ── Expense ────────────────────────────────────────────────────────────────────

@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display   = ("date", "description_short", "amount_display",
                      "currency", "category", "supplier", "is_vat_deductible")
    list_filter    = ("category", "currency", "is_vat_deductible", "date")
    search_fields  = ("description", "supplier__name")
    date_hierarchy = "date"
    autocomplete_fields = ("supplier",)

    fieldsets = (
        ("💸 Витрата", {
            "fields": ("date", "description", ("amount", "currency"),
                       "category", "supplier")
        }),
        ("📎 Документ", {
            "fields": ("receipt", "is_vat_deductible")
        }),
    )

    def description_short(self, obj):
        s = obj.description
        return (s[:55] + "…") if len(s) > 55 else s
    description_short.short_description = "Опис"

    def amount_display(self, obj):
        return format_html("<b>{}</b>", f"{obj.amount:.2f}")
    amount_display.short_description = "Сума"
    amount_display.admin_order_field = "amount"


# ── Inject invoice stats into accounting app_index context ─────────────────────

def _get_invoice_stats():
    try:
        from django.db.models import Count
        qs = Invoice.objects.values("status").annotate(n=Count("pk"))
        by_status = {row["status"]: row["n"] for row in qs}
        return {
            "total":   sum(by_status.values()),
            "sent":    by_status.get("sent", 0),
            "overdue": by_status.get("overdue", 0),
            "draft":   by_status.get("draft", 0),
            "paid":    by_status.get("paid", 0),
        }
    except Exception:
        return {"total": "—", "sent": "—", "overdue": "—", "draft": "—", "paid": "—"}


_orig_app_index = admin.site.app_index


def _accounting_app_index(request, app_label, extra_context=None):
    if app_label == "accounting":
        extra_context = extra_context or {}
        extra_context["invoice_stats"] = _get_invoice_stats()
        try:
            from config.models import SystemSettings
            extra_context["accounting_level"] = SystemSettings.get().accounting_level
        except Exception:
            extra_context["accounting_level"] = 2
    return _orig_app_index(request, app_label, extra_context)


admin.site.app_index = _accounting_app_index
