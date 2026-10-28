"""Адмінка «🏷 Свої категорії листів» (DigiKey, Інтернет-магазин…): правила, листи за 30 днів, час askDANTE.
Блок «⏱ askDANTE» додає застосунок worktime (inline MailCategoryTime), пошта про askDANTE не знає."""
import datetime

from django import forms
from django.contrib import admin, messages
from django.db import models
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from email_assistant import work_categories
from email_assistant.models import EmailMessage, MailCategory


def _extra_inlines():
    try:
        from worktime.admin_mail import MailCategoryTimeInline
        return [MailCategoryTimeInline]
    except ImportError:   # робочий час не встановлено
        return []


@admin.register(MailCategory)
class MailCategoryAdmin(admin.ModelAdmin):
    list_display = ('label_col', 'rules_col', 'mails_col', 'dante_col', 'order', 'is_active')
    list_editable = ('order', 'is_active')
    list_display_links = ('label_col',)
    filter_horizontal = ('sources', 'accounts')
    formfield_overrides = {models.TextField: {'widget': forms.Textarea(attrs={'rows': 4, 'cols': 60})}}
    change_list_template = 'admin/email_assistant/mailcategory/change_list.html'
    fieldsets = (
        (None, {'fields': (('name', 'icon', 'color'), ('order', 'is_active'))}),
        ('✉️ Які листи сюди йдуть', {
            'description': 'Досить однієї ознаки. Те саме можна робити зі скриньки: правий клік по листу → '
                           '«Віднести до категорії» → своя (адреса або весь домен потрапляє в «Відправники»). '
                           'Відповіді в тій самій гілці йдуть у ту саму категорію.',
            'fields': ('senders', 'subject_words', 'sources', 'by_customer_orders', 'accounts')}),
    )

    def get_inlines(self, request, obj):
        return _extra_inlines()

    @admin.display(description='Категорія', ordering='name')
    def label_col(self, obj):
        return format_html('<span style="color:{}">●</span> {}', obj.color, obj.label)

    @admin.display(description='Правила')
    def rules_col(self, obj):
        parts = []
        if obj.lines('senders'):
            parts.append(f"✉️ {len(obj.lines('senders'))}")
        if obj.lines('subject_words'):
            parts.append(f"🔤 {len(obj.lines('subject_words'))}")
        n = obj.sources.count()
        if n:
            parts.append(f'🧾 {n}')
        n = obj.accounts.count()
        if n:
            parts.append(f'📮 {n}')
        return ' · '.join(parts) or '—'

    @admin.display(description='Листів за 30 днів')
    def mails_col(self, obj):
        since = timezone.now() - datetime.timedelta(days=30)
        n = EmailMessage.objects.filter(work_category=obj, sent_at__gte=since, is_deleted=False).count()
        return format_html('<a href="/email/?wcat={}">{}</a>', obj.pk, n)

    @admin.display(description='askDANTE')
    def dante_col(self, obj):
        dante = getattr(obj, 'dante', None)
        if dante is None or not dante.dante_project_id:
            return '—'
        return format_html('{}{}', dante.dante_project, ' · авто' if dante.auto else '')

    def get_urls(self):
        return [path('recompute/', self.admin_site.admin_view(self.recompute_view),
                     name='email_assistant_mailcategory_recompute')] + super().get_urls()

    def recompute_view(self, request):
        if request.method == 'POST' and self.has_change_permission(request):
            days = int(request.POST.get('days') or 730)
            work_categories.start_recompute(days)
            messages.info(request, 'Перерахунок запущено у фоні — стан видно над списком.')
        return redirect(reverse('admin:email_assistant_mailcategory_changelist'))

    def changelist_view(self, request, extra_context=None):
        extra_context = {**(extra_context or {}), 'scan_status': work_categories.status()}
        return super().changelist_view(request, extra_context)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if {'senders', 'subject_words', 'sources', 'by_customer_orders', 'accounts', 'is_active', 'order'} \
                & set(form.changed_data):
            work_categories.start_recompute(365)
            messages.info(request, 'Правила змінились — листи за рік перераховуються у фоні.')
