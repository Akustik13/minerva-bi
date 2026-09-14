from django.contrib import admin, messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils.html import format_html
from django.forms import PasswordInput

from .models import ScraperDocument, ScraperRun, ScraperSiteConfig

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


# ── ScraperRun inline ─────────────────────────────────────────────────────────

class ScraperRunInline(admin.TabularInline):
    model   = ScraperRun
    extra   = 0
    max_num = 0
    can_delete          = False
    show_change_link    = True
    fields  = ('started_at', 'status_badge', 'files_downloaded', 'duration_display',
                'triggered_by', 'error_short')
    readonly_fields = fields

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
    list_display  = ('site_name', 'username', 'enabled', 'status_col',
                      'last_run_at', 'last_run_files', 'run_btn')
    list_filter   = ('enabled', 'last_run_status', 'site_name')
    inlines       = [ScraperRunInline]

    fieldsets = (
        ('Основне', {
            'fields': ('site_name', 'enabled', 'username', 'password'),
        }),
        ('Розклад', {
            'fields': ('cron_schedule', 'lookback_days'),
            'description': 'Для автозапуску на сервері: '
                           '<code>python manage.py run_scraper --all</code> у cron.',
        }),
        ('Сповіщення', {
            'fields': ('notify_email', 'notify_telegram'),
        }),
        ('Бухгалтерія', {
            'fields': ('auto_create_expense', 'expense_category', 'supplier'),
            'description': 'Після завантаження PDF автоматично створюється '
                           'запис Expense з сумою 0 (заповнити вручну).',
        }),
        ('Останній запуск', {
            'fields': ('last_run_at', 'last_run_status', 'last_run_files', 'run_now_btn'),
            'classes': ('collapse',),
        }),
    )
    readonly_fields = ('last_run_at', 'last_run_status', 'last_run_files', 'run_now_btn')
    autocomplete_fields = ('expense_category', 'supplier')

    # ── Password field hidden ─────────────────────────────────────────────────

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if 'password' in form.base_fields:
            form.base_fields['password'].widget = PasswordInput(render_value=True)
        return form

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
        from scraper_hub.services import run_site_in_thread
        run_site_in_thread(cfg, triggered_by=f'admin:{request.user.username}')
        self.message_user(
            request,
            f'▶ Запущено {cfg.get_site_name_display()} у фоні — '
            f'оновіть сторінку через кілька хвилин.',
            messages.SUCCESS,
        )
        return redirect(reverse('admin:scraper_hub_scrapersiteconfig_change', args=[pk]))

    # ── Display helpers ───────────────────────────────────────────────────────

    def status_col(self, obj):
        return _status_badge(obj.last_run_status, obj.get_last_run_status_display())
    status_col.short_description = 'Статус'

    def run_btn(self, obj):
        if not obj.pk:
            return '—'
        url = reverse('admin:scraper_run_now', args=[obj.pk])
        return format_html(
            '<a class="button" href="{}" style="white-space:nowrap">▶ Запустити</a>',
            url,
        )
    run_btn.short_description = ''

    def run_now_btn(self, obj):
        return self.run_btn(obj)
    run_now_btn.short_description = 'Запуск'


# ── ScraperRun admin ──────────────────────────────────────────────────────────

@admin.register(ScraperRun)
class ScraperRunAdmin(admin.ModelAdmin):
    list_display  = ('__str__', 'status_col', 'files_downloaded',
                      'duration_col', 'triggered_by', 'started_at')
    list_filter   = ('status', 'config__site_name')
    readonly_fields = ('config', 'started_at', 'finished_at', 'status',
                        'files_downloaded', 'error_message', 'log_output', 'triggered_by')
    search_fields = ('config__site_name', 'error_message')

    def has_add_permission(self, request):
        return False

    def status_col(self, obj):
        return _status_badge(obj.status, obj.get_status_display())
    status_col.short_description = 'Статус'

    def duration_col(self, obj):
        s = obj.duration_s
        if s is None:
            return '—'
        return f'{s // 60}хв {s % 60}с' if s >= 60 else f'{s}с'
    duration_col.short_description = 'Тривалість'


# ── ScraperDocument admin ─────────────────────────────────────────────────────

@admin.register(ScraperDocument)
class ScraperDocumentAdmin(admin.ModelAdmin):
    list_display  = ('batch_num', 'site_col', 'invoice_date',
                      'amount_col', 'pdf_link', 'expense_link', 'created_at')
    list_filter   = ('run__config__site_name', 'currency')
    search_fields = ('batch_num',)
    readonly_fields = ('run', 'batch_num', 'file', 'expense',
                        'created_at', 'pdf_link', 'expense_link')
    fields = ('run', 'batch_num', 'invoice_date', 'amount', 'currency',
              'pdf_link', 'expense_link', 'created_at')

    def has_add_permission(self, request):
        return False

    def site_col(self, obj):
        return obj.run.config.get_site_name_display()
    site_col.short_description = 'Сайт'

    def amount_col(self, obj):
        if obj.amount is None:
            return format_html('<span style="color:var(--text-dim)">не вказано</span>')
        return f'{obj.amount} {obj.currency}'
    amount_col.short_description = 'Сума'

    def pdf_link(self, obj):
        if not obj.file:
            return '—'
        return format_html(
            '<a href="/media/{}" target="_blank">📄 PDF</a>',
            obj.file,
        )
    pdf_link.short_description = 'PDF'

    def expense_link(self, obj):
        if not obj.expense_id:
            return format_html('<span style="color:var(--text-dim)">—</span>')
        url = reverse('admin:accounting_expense_change', args=[obj.expense_id])
        return format_html('<a href="{}">💰 Витрата #{}</a>', url, obj.expense_id)
    expense_link.short_description = 'Витрата'
