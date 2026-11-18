"""Адмінка розділу «🧾 Рахунки»: сам розділ — сторінки /rechnungen/ (sales/views_invoices.py), тут лише пункт меню
(список → /rechnungen/) і «⚙️ Налаштування рахунків» (синглтон: префікс, мова, строки, ПДВ, банк, тексти DE / EN, лист)."""
from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.html import format_html

from .models_invoice import InvoiceSettings, SalesInvoice

PLACEHOLDER_HELP = ("Підстановки: {number} — номер, {date} — дата, {due_date} — оплатити до, {amount} — сума брутто з "
                    "валютою, {order} — номер замовлення, {customer} — покупець, {method} — спосіб оплати.")


@admin.register(SalesInvoice)
class SalesInvoiceAdmin(admin.ModelAdmin):
    """Пункт «🧾 Рахунки (Rechnung)» у меню «Продажі»; працюють сторінки /rechnungen/."""

    def changelist_view(self, request, extra_context=None):
        return redirect("/rechnungen/")

    def add_view(self, request, form_url="", extra_context=None):
        return redirect("/rechnungen/")

    def change_view(self, request, object_id, form_url="", extra_context=None):
        return redirect(f"/rechnungen/{int(object_id)}/" if str(object_id).isdigit() else "/rechnungen/")


@admin.register(InvoiceSettings)
class InvoiceSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        ("🔢 Номер і мова", {
            "fields": ("number_prefix", "default_language", "file_name"),
            "description": "Замовлення з інтернет-магазину — номер магазину CI-&lt;Kundennummer&gt;-NNN (той самий, що в "
                           "Commercial Invoice й у кабінеті покупця). Інші — префікс + номер замовлення.",
        }),
        ("📅 Строки й ПДВ", {
            "fields": ("due_days", "prepayment_due_days", "vat_rate", "home_country"),
        }),
        ("✍️ Підпис і банк", {
            "fields": ("signer_name", "signer_position", "payment_info"),
        }),
        ("🇩🇪 Тексти рахунку — Deutsch", {
            "fields": ("intro_de", "pay_invoice_de", "pay_prepayment_de", "pay_paid_de", "vat_reverse_de",
                       "vat_export_de", "outro_de"),
            "description": PLACEHOLDER_HELP,
        }),
        ("🇬🇧 Тексти рахунку — English", {
            "fields": ("intro_en", "pay_invoice_en", "pay_prepayment_en", "pay_paid_en", "vat_reverse_en",
                       "vat_export_en", "outro_en"),
            "description": PLACEHOLDER_HELP,
        }),
        ("✉️ Лист з рахунком", {
            "fields": ("email_subject_de", "email_body_de", "email_subject_en", "email_body_en"),
            "description": PLACEHOLDER_HELP + " «Deutsch + English» — обидва тексти.",
        }),
        ("📄 Шаблон", {
            "fields": ("template_info",),
        }),
    )
    readonly_fields = ("template_info",)

    def template_info(self, obj):
        from . import invoice_service
        kind = invoice_service.template_kind(obj)
        text = {"custom": "власний (завантажений)", "blank": "на бланку Commercial Invoice",
                "simple": "простий (без бланка)"}[kind]
        return format_html('Зараз: <b>{}</b>. Завантажити свій, приклад, змінні — <a href="/rechnungen/#template">'
                           '🧾 Рахунки → 📄 Шаблон Word</a>.', text)
    template_info.short_description = "Шаблон Word"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = InvoiceSettings.get()
        return redirect(reverse("admin:sales_invoicesettings_change", args=[obj.pk]))
