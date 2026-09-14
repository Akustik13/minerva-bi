import os

from django import forms
from django.contrib import admin, messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils.html import format_html
from django.forms import PasswordInput

from .models import ScraperDocument, ScraperRun, ScraperSiteConfig

_VNC_URL = os.environ.get('SCRAPER_VNC_URL', '')

# ── Статус-бейджі ─────────────────────────────────────────────────────────────

_STATUS_COLORS = {
    'idle':    ('#607d8b', '⏳'),
    'running': ('#1976d2', '⚙️'),
    'ok':      ('#388e3c', '✅'),
    'partial': ('#f57c00', '⚠️'),
    'error':   ('#d32f2f', '❌'),
}


def _status_badge(status, label):
    color, icon = _STATUS_COLORS.get(status, ('#607d8b', ''))
    return format_html(
        '<span style="color:{};font-weight:600">{} {}</span>',
        color, icon, label,
    )


# ── Custom admin form (weekday checkboxes + time picker) ──────────────────────

_WEEKDAY_CHOICES = [
    ('1', 'Пн'), ('2', 'Вт'), ('3', 'Ср'), ('4', 'Чт'),
    ('5', 'Пт'), ('6', 'Сб'), ('7', 'Нд'),
]


class ScraperSiteConfigForm(forms.ModelForm):
    schedule_weekdays_select = forms.MultipleChoiceField(
        choices=_WEEKDAY_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label='Дні тижня',
        help_text='Виберіть один або кілька днів тижня.',
    )

    class Meta:
        model   = ScraperSiteConfig
        exclude = ['schedule_weekdays']
        widgets = {
            'schedule_time': forms.TimeInput(
                attrs={'type': 'time', 'class': 'vTimeField'},
                format='%H:%M',
            ),
            'password': PasswordInput(render_value=True),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        obj = self.instance
        if obj.pk and obj.schedule_weekdays:
            days = [d.strip() for d in obj.schedule_weekdays.split(',') if d.strip()]
            self.fields['schedule_weekdays_select'].initial = days
        # password widget (also set via form Meta widget, belt-and-suspenders)
        if 'password' in self.fields:
            self.fields['password'].widget = PasswordInput(render_value=True)

    def save(self, commit=True):
        obj = super().save(commit=False)
        days = sorted(self.cleaned_data.get('schedule_weekdays_select') or [])
        obj.schedule_weekdays = ','.join(days)
        if commit:
            obj.save()
            self.save_m2m()
        return obj


# ── ScraperRun inline ─────────────────────────────────────────────────────────

class ScraperRunInline(admin.TabularInline):
    model            = ScraperRun
    extra            = 0
    max_num          = 0
    can_delete       = False
    show_change_link = True
    fields           = ('started_at', 'status_badge', 'files_downloaded',
                         'duration_display', 'triggered_by', 'error_short')
    readonly_fields  = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def status_badge(self, obj):
        return _status_badge(obj.status, obj.get_status_display())
    status_badge.short_description = 'Статус'

    def duration_display(self, obj):
        s = obj.duration_s
        if s is None:
            return '—'
        return f'{s // 60}хв {s % 60}с' if s >= 60 else f'{s}с'
    duration_display.short_description = 'Тривалість'

    def error_short(self, obj):
        if not obj.error_message:
            return '—'
        return obj.error_message[:80] + ('…' if len(obj.error_message) > 80 else '')
    error_short.short_description = 'Помилка'


# ── ScraperSiteConfig admin ───────────────────────────────────────────────────

@admin.register(ScraperSiteConfig)
class ScraperSiteConfigAdmin(admin.ModelAdmin):
    form          = ScraperSiteConfigForm
    list_display  = ('site_name', 'username', 'enabled', 'schedule_col',
                      'status_col', 'last_run_at', 'last_run_files', 'run_btn')
    list_filter   = ('enabled', 'last_run_status', 'site_name', 'schedule_type')
    inlines       = [ScraperRunInline]

    fieldsets = (
        ('Основне', {
            'fields': ('site_name', 'enabled', 'username', 'password'),
        }),
        ('Розклад', {
            'fields': (
                'schedule_type', 'schedule_time', 'schedule_weekdays_select',
                'lookback_days', 'schedule_info',
            ),
            'description': (
                'Для автозапуску на NAS (раз на хвилину) додайте один системний cron:<br>'
                '<code>* * * * * docker exec tabele_mvp-web-1 python manage.py run_scraper --cron</code><br>'
                'Або через Synology Task Scheduler.'
            ),
        }),
        ('Посилання', {
            'fields': ('site_url', 'invoice_url_tpl'),
            'description': (
                'Шаблон рахунку має містити <code>{batch}</code> — '
                'напр. <code>https://jlcpcb.com/order/{batch}</code>.'
            ),
        }),
        ('Сповіщення', {
            'fields': ('notify_email', 'notify_telegram', 'notify_on_error', 'notify_email_to'),
        }),
        ('Бухгалтерія', {
            'fields': ('auto_create_expense', 'expense_category', 'supplier'),
        }),
        ('Email / IMAP налаштування', {
            'fields': (
                'imap_host', 'imap_port', 'imap_use_ssl', 'imap_folder',
                'email_from_filter', 'email_subject_kw', 'email_attach_ext', 'email_mark_read',
            ),
            'classes': ('collapse',),
            'description': 'Заповнюйте тільки для типу <b>Email (IMAP)</b>.',
        }),
        ('Конфіг власного скрапера', {
            'fields': (
                'login_url', 'invoice_list_url',
                'email_selector', 'password_selector', 'submit_selector', 'login_success_url',
                'row_selector', 'batch_col_index', 'date_col_index', 'download_selector',
            ),
            'classes': ('collapse',),
            'description': 'Заповнюйте тільки для типу <b>Власний сайт (custom)</b>.',
        }),
        ('Останній запуск', {
            'fields': ('last_run_at', 'last_run_status', 'last_run_files', 'run_now_btn'),
            'classes': ('collapse',),
        }),
    )
    readonly_fields     = ('last_run_at', 'last_run_status', 'last_run_files',
                           'run_now_btn', 'schedule_info')
    autocomplete_fields = ('expense_category', 'supplier')

    class Media:
        js = ('scraper_hub/schedule_widget.js',)
        css = {'all': ('scraper_hub/schedule_widget.css',)}

    # ── Custom URLs ───────────────────────────────────────────────────────────

    def get_urls(self):
        return [
            path(
                '<int:pk>/run/',
                self.admin_site.admin_view(self._run_view),
                name='scraper_run_now',
            ),
        ] + super().get_urls()

    def _run_view(self, request, pk):
        cfg = get_object_or_404(ScraperSiteConfig, pk=pk)
        if cfg.last_run_status == 'running':
            self.message_user(request, '⚙️ Scraper вже виконується.', messages.WARNING)
            return redirect(reverse('admin:scraper_hub_scrapersiteconfig_change', args=[pk]))

        from scraper_hub.services import create_and_run_in_thread
        run = create_and_run_in_thread(cfg, triggered_by=f'admin:{request.user.username}')
        return redirect(reverse('admin:scraper_hub_scraperrun_change', args=[run.pk]))

    # ── Display helpers ───────────────────────────────────────────────────────

    def schedule_col(self, obj):
        desc = obj.schedule_description
        if obj.schedule_type == 'manual':
            return format_html('<span style="color:var(--text-dim)">—</span>')
        cron = obj.cron_expression
        return format_html(
            '<span title="cron: {}">{}</span>',
            cron, desc,
        )
    schedule_col.short_description = 'Розклад'

    def schedule_info(self, obj):
        if not obj.pk:
            return '—'
        desc = obj.schedule_description
        cron = obj.cron_expression
        if cron:
            return format_html(
                '<strong>{}</strong><br>'
                '<code style="font-size:11px;color:var(--text-dim)">{}</code>',
                desc, cron,
            )
        return format_html('<span style="color:var(--text-dim)">{}</span>', desc)
    schedule_info.short_description = 'Підсумок розкладу'

    def status_col(self, obj):
        return _status_badge(obj.last_run_status, obj.get_last_run_status_display())
    status_col.short_description = 'Статус'

    def run_btn(self, obj):
        if not obj.pk:
            return '—'
        url = reverse('admin:scraper_run_now', args=[obj.pk])
        return format_html('<a class="button" href="{}">▶ Запустити</a>', url)
    run_btn.short_description = ''

    def run_now_btn(self, obj):
        return self.run_btn(obj)
    run_now_btn.short_description = 'Запуск'


# ── ScraperRun admin ──────────────────────────────────────────────────────────

@admin.register(ScraperRun)
class ScraperRunAdmin(admin.ModelAdmin):
    list_display    = ('__str__', 'status_col', 'files_downloaded',
                        'duration_col', 'triggered_by', 'started_at')
    list_filter     = ('status', 'config__site_name')
    search_fields   = ('config__site_name', 'error_message')
    readonly_fields = ('config', 'started_at', 'finished_at', 'status_badge_field',
                        'files_downloaded', 'error_message', 'log_display',
                        'triggered_by', 'duration_field', 'documents_inline')

    fieldsets = (
        (None, {
            'fields': ('config', 'triggered_by', 'started_at',
                        'finished_at', 'duration_field', 'status_badge_field',
                        'files_downloaded', 'error_message'),
        }),
        ('Лог виконання', {
            'fields': ('log_display',),
        }),
        ('Завантажені рахунки', {
            'fields': ('documents_inline',),
            'classes': ('collapse',),
        }),
    )

    def has_add_permission(self, request):
        return False

    def change_view(self, request, object_id, form_url='', extra_context=None):
        response = super().change_view(request, object_id, form_url, extra_context)
        try:
            obj = ScraperRun.objects.get(pk=object_id)
            if obj.status == 'running' and hasattr(response, 'content'):
                vnc_banner = b''
                if _VNC_URL:
                    vnc_html = (
                        f'<div style="background:#1565c0;color:#fff;padding:10px 16px;'
                        f'border-radius:4px;margin:8px 0;font-size:13px">'
                        f'\U0001f5a5️  Браузер виконується на сервері. '
                        f'<a href="{_VNC_URL}/vnc.html" target="_blank" '
                        f'style="color:#90caf9;font-weight:600">Відкрити noVNC ↗</a>'
                        f' &nbsp;— для CAPTCHA або спостереження</div>'
                    )
                    vnc_banner = vnc_html.encode('utf-8')
                script = (
                    b'<script>'
                    b'(function(){'
                    b'  var t=setTimeout(function(){location.reload()},3000);'
                    b'  document.addEventListener("visibilitychange",function(){'
                    b'    if(document.hidden){clearTimeout(t)}'
                    b'    else{t=setTimeout(function(){location.reload()},3000)}'
                    b'  });'
                    b'})();'
                    b'</script>'
                )
                response.content = response.content.replace(b'</body>', vnc_banner + script + b'</body>')
        except Exception:
            pass
        return response

    def status_col(self, obj):
        return _status_badge(obj.status, obj.get_status_display())
    status_col.short_description = 'Статус'

    def status_badge_field(self, obj):
        return _status_badge(obj.status, obj.get_status_display())
    status_badge_field.short_description = 'Статус'

    def duration_col(self, obj):
        s = obj.duration_s
        if s is None:
            return '⚙️ …' if obj.status == 'running' else '—'
        return f'{s // 60}хв {s % 60}с' if s >= 60 else f'{s}с'
    duration_col.short_description = 'Тривалість'

    def duration_field(self, obj):
        return self.duration_col(obj)
    duration_field.short_description = 'Тривалість'

    def log_display(self, obj):
        if not obj.log_output:
            return format_html('<span style="color:var(--text-dim)">Лог порожній</span>')
        return format_html(
            '<pre style="'
            'max-height:500px;overflow-y:auto;'
            'background:var(--bg-input,#141f2b);'
            'color:var(--text,#c9d8e4);'
            'padding:12px;border-radius:4px;'
            'font-size:12px;line-height:1.5;'
            'white-space:pre-wrap;word-break:break-all'
            '">{}</pre>',
            obj.log_output,
        )
    log_display.short_description = 'Лог'

    def documents_inline(self, obj):
        docs = list(obj.documents.select_related('expense').all())
        if not docs:
            return format_html('<span style="color:var(--text-dim)">Немає</span>')
        rows = []
        for d in docs:
            pdf = (
                format_html('<a href="/media/{}" target="_blank">📄 PDF</a>', d.file)
                if d.file else format_html('—')
            )
            exp = (
                format_html(
                    '<a href="{}">💰 #{}</a>',
                    reverse('admin:accounting_expense_change', args=[d.expense_id]),
                    d.expense_id,
                )
                if d.expense_id else format_html('—')
            )
            rows.append(format_html(
                '<tr><td style="padding:4px 8px">{}</td>'
                '<td style="padding:4px 8px">{}</td>'
                '<td style="padding:4px 8px">{}</td></tr>',
                d.batch_num, pdf, exp,
            ))
        rows_html = format_html('{}' * len(rows), *rows)
        return format_html(
            '<table style="border-collapse:collapse">'
            '<tr><th style="padding:4px 8px;text-align:left">Batch #</th>'
            '<th style="padding:4px 8px;text-align:left">PDF</th>'
            '<th style="padding:4px 8px;text-align:left">Витрата</th></tr>'
            '{}</table>',
            rows_html,
        )
    documents_inline.short_description = 'Завантажені рахунки'


# ── ScraperDocument admin ─────────────────────────────────────────────────────

@admin.register(ScraperDocument)
class ScraperDocumentAdmin(admin.ModelAdmin):
    list_display  = ('batch_num_link', 'site_col', 'jlc_order_link', 'invoice_date',
                      'amount_col', 'pdf_link', 'expense_link', 'created_at')
    list_filter   = ('run__config__site_name', 'currency')
    search_fields = ('batch_num',)
    readonly_fields = ('run', 'batch_num', 'batch_num_link', 'jlc_order_link',
                       'created_at', 'pdf_link', 'expense_link')
    fields = ('run', 'batch_num', 'batch_num_link', 'jlc_order', 'jlc_order_link',
              'invoice_date', 'amount', 'currency', 'pdf_link', 'expense_link', 'created_at')

    def has_add_permission(self, request):
        return False

    def batch_num_link(self, obj):
        cfg = obj.run.config
        tpl = cfg.invoice_url_tpl
        base = cfg.site_url
        if tpl and '{batch}' in tpl:
            url = tpl.format(batch=obj.batch_num)
        elif base:
            url = base
        else:
            return obj.batch_num
        return format_html(
            '<a href="{}" target="_blank" rel="noopener">{} ↗</a>',
            url, obj.batch_num,
        )
    batch_num_link.short_description = 'Batch #'
    batch_num_link.admin_order_field = 'batch_num'

    def site_col(self, obj):
        return obj.run.config.get_site_name_display()
    site_col.short_description = 'Сайт'

    def amount_col(self, obj):
        if obj.amount is None:
            return format_html('<span style="color:var(--text-dim)">—</span>')
        return f'{obj.amount} {obj.currency}'
    amount_col.short_description = 'Сума'

    def pdf_link(self, obj):
        if not obj.file:
            return '—'
        return format_html('<a href="/media/{}" target="_blank">📄 PDF</a>', obj.file)
    pdf_link.short_description = 'PDF'

    def jlc_order_link(self, obj):
        if not obj.jlc_order_id:
            return format_html('<span style="color:var(--text-dim)">—</span>')
        url = reverse('admin:jlcpcb_jlcorder_change', args=[obj.jlc_order_id])
        return format_html(
            '<a href="{}">🔧 {}</a>',
            url, obj.jlc_order.jlc_order_number or obj.jlc_order_id,
        )
    jlc_order_link.short_description = 'JLCPCB замовлення'

    def expense_link(self, obj):
        if not obj.expense_id:
            return format_html('<span style="color:var(--text-dim)">—</span>')
        url = reverse('admin:accounting_expense_change', args=[obj.expense_id])
        return format_html('<a href="{}">💰 Витрата #{}</a>', url, obj.expense_id)
    expense_link.short_description = 'Витрата'
