"""Адмінка розділу «🧾 Рахунки»: сам розділ — сторінки /rechnungen/ (sales/views_invoices.py), тут лише пункт меню
(список → /rechnungen/) і «⚙️ Налаштування рахунків» (синглтон: префікс, мова, строки, ПДВ, банк, тексти DE / EN, лист)."""
from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.html import format_html

from .models_invoice import InvoiceSettings, SalesInvoice

PLACEHOLDER_HELP = ("Підстановки: {number} — номер, {date} — дата, {due_date} — оплатити до, {amount} — сума брутто з "
                    "валютою, {order} — номер замовлення, {customer} — покупець, {method} — спосіб оплати, "
                    "{salutation} — звертання, {signer} — підписант, {company} — ваша фірма.")


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
            "fields": ("number_prefix", "default_language", "file_name", "order_button"),
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
            "fields": ("auto_send", "email_subject_de", "email_body_de", "email_subject_en", "email_body_en"),
            "description": PLACEHOLDER_HELP + " «Deutsch + English» — обидва тексти. Тестовий лист і журнал "
                           "автоматичних листів — <a href=\"/rechnungen/#auto\">🧾 Рахунки → «✉️ Рахунок покупцю "
                           "автоматично»</a>.",
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


ORDER_BUTTON_JS = '<script src="/static/admin/js/ri_order_button.js?v=2" defer></script>'


def order_button(order, last, style: str) -> str:
    """Кнопка «🧾 Rechnung» у картці замовлення (SalesOrderAdmin.doc_templates_panel): рахунку ще немає — клік за
    `InvoiceSettings.order_button` (⚡ одразу — POST /rechnungen/from-order/<pk>/, або форма), є — відкрити його;
    правий клік — інші варіанти (MVMenu)."""
    from django.utils.html import escape
    if last is not None:
        attrs = {"data-ri-menu": "1", "data-ri-open": f"/rechnungen/{last.pk}/",
                 "data-ri-again": f"/rechnungen/new/?order={order.pk}&again=1",
                 "data-ri-regen": f"/rechnungen/{last.pk}/regenerate/",
                 "data-ri-pdf": f"/rechnungen/{last.pk}/file/pdf/"}         # PDF ще немає — сформується при відкритті
        if last.pdf_file or last.docx_file:
            attrs["data-ri-mail"] = f"/email/compose/?invoice={last.pk}"
        data = " ".join(f'{k}="{escape(v)}"' for k, v in attrs.items())
        html = (f'<a href="/rechnungen/{last.pk}/" style="{style}" {data} '
                f'title="Відкрити рахунок · правий клік — PDF, перегенерувати, лист, ще один рахунок">'
                f'🧾 Rechnung <span translate="no">{escape(last.number)}</span></a>')
    else:
        mode = InvoiceSettings.get().order_button
        form = f"/rechnungen/new/?order={order.pk}"
        title = ("Клік — одразу сформувати рахунок із даних замовлення · правий клік — форма"
                 if mode == "quick" else "Клік — форма рахунку · правий клік — сформувати одразу")
        html = (f'<a href="{form}" style="{style}" data-ri-menu="1" data-ri-mode="{escape(mode)}" '
                f'data-ri-quick="/rechnungen/from-order/{order.pk}/" data-ri-form="{form}" title="{title}">'
                f'🧾 Rechnung (рахунок)</a>')
    return html + ORDER_BUTTON_JS
