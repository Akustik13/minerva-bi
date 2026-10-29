from django import forms
from django.contrib import admin
from django.utils.html import format_html
from django.contrib import messages
from django.utils import timezone

from .models import APIKey, APIRequestLog, SCOPE_CHOICES, Webhook, WebhookDelivery, WEBHOOK_EVENTS


class APIKeyForm(forms.ModelForm):
    scopes_input = forms.MultipleChoiceField(
        choices=SCOPE_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label='Права доступу (scopes)',
    )

    class Meta:
        model = APIKey
        fields = ['name', 'is_active', 'expires_at', 'shop', 'default_source', 'allowed_ips', 'limit_minute',
                  'limit_hour', 'limit_day', 'items_per_day', 'fields', 'log_mode']
        widgets = {
            'allowed_ips': forms.Textarea(attrs={'rows': 2, 'placeholder': '203.0.113.5, 10.0.0.0/24'}),
            'fields': forms.Textarea(attrs={'rows': 2, 'placeholder': 'sku, available'}),
        }

    def clean_allowed_ips(self):
        from .key_limits import parse_networks
        value = self.cleaned_data.get('allowed_ips', '')
        try:
            parse_networks(value)
        except ValueError as exc:
            raise forms.ValidationError(str(exc))
        return value.strip()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['scopes_input'].initial = self.instance.scopes

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.scopes = self.cleaned_data.get('scopes_input', [])
        if commit:
            instance.save()
        return instance


@admin.register(APIKey)
class APIKeyAdmin(admin.ModelAdmin):
    form = APIKeyForm
    list_display  = ['name', 'key_preview', 'scopes_display', 'is_active',
                     'last_used', 'activity_col', 'limits_col', 'expires_at', 'created_at']
    list_filter   = ['is_active']
    search_fields = ['name']
    readonly_fields = ['key_display', 'created_at', 'last_used', 'usage_display', 'activity_panel']

    fieldsets = [
        ('Загальне', {
            'fields': ['name', 'is_active', 'expires_at', 'shop', 'default_source'],
        }),
        ('Права доступу', {
            'fields': ['scopes_input'],
            'description': 'Оберіть які операції дозволені для цього ключа.',
        }),
        ('🛡 Обмеження', {
            'fields': ['allowed_ips', ('limit_minute', 'limit_hour', 'limit_day'), 'items_per_day', 'fields',
                       'usage_display'],
            'description': 'Порожнє поле — без обмеження. Приклад «лише кількість зі складу»: право «Склад — читання», '
                           'поля <code>sku, available</code>. «Різних записів за добу» = 10 — ключ побачить не більше '
                           '10 різних товарів за добу (уже переглянуті — без обмежень). Відмова — 401 (адреса) або '
                           '429 з причиною й Retry-After.',
        }),
        ('📜 Журнал запитів', {
            'fields': ['log_mode', 'activity_panel'],
            'description': 'Хто й звідки користувався ключем і що робив — «📜 Журнал API». '
                           'Записи старші за API_LOG_DAYS (60 днів) видаляються щоночі.',
        }),
        ('Технічна інформація', {
            'fields': ['key_display', 'created_at', 'last_used'],
            'classes': ['collapse'],
        }),
    ]

    _base_url = ''

    def change_view(self, request, object_id, form_url='', extra_context=None):
        self._base_url = request.build_absolute_uri('/').rstrip('/')
        return super().change_view(request, object_id, form_url, extra_context)

    def key_preview(self, obj):
        from core.secrets import mask
        return mask(obj.key)
    key_preview.short_description = 'Ключ'

    def key_display(self, obj):
        if obj.pk:
            from core.secrets import reveal
            # ключ замасковано (видно початок і кінець); «Копіювати» бере повний — для shop-config.php сайту
            return format_html(
                '{}<div style="margin-top:8px;font-size:12px;opacity:.8">Перевірка підключення '
                '(замість &lt;ключ&gt; — скопійований ключ):</div>'
                '<code style="font-size:12px;user-select:all;display:inline-block;margin-top:4px">'
                'curl -H "Authorization: Token &lt;ключ&gt;" {}/api/v1/ping/</code>'
                '<div style="margin-top:6px;font-size:12px;opacity:.8">'
                'Документація: <code>api/README.md</code> · '
                '<a href="/dashboard/api/console/?preset=minerva_ping">Консоль API</a></div>',
                reveal(obj.key), self._base_url,
            )
        return '— буде згенеровано після збереження —'
    key_display.short_description = 'API Ключ (скопіюй)'

    @admin.display(description='Сьогодні')
    def activity_col(self, obj):
        from django.urls import reverse
        today = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
        qs = obj.request_logs.filter(created_at__gte=today)
        n = qs.count()
        if not n:
            return '—'
        bad = qs.filter(status__gte=400).count()
        last = obj.request_logs.exclude(ip=None).order_by('-created_at').values_list('ip', flat=True).first()
        url = reverse('admin:api_apirequestlog_changelist') + f'?key__id__exact={obj.pk}'
        return format_html('<a href="{}">{} запитів</a>{}{}', url, n,
                           format_html(' · <span style="color:var(--err)">{} помилок</span>', bad) if bad else '',
                           format_html('<br><small>{}</small>', last) if last else '')

    @admin.display(description='Обмеження')
    def limits_col(self, obj):
        parts = []
        if obj.allowed_ips.strip():
            parts.append('🛡 IP')
        for value, label in ((obj.limit_minute, '/хв'), (obj.limit_hour, '/год'), (obj.limit_day, '/добу')):
            if value:
                parts.append(f'{value}{label}')
        if obj.items_per_day:
            parts.append(f'{obj.items_per_day} записів/добу')
        if obj.fields.strip():
            parts.append('поля')
        return ' · '.join(parts) or '—'

    @admin.display(description='Використано зараз')
    def usage_display(self, obj):
        if not obj.pk:
            return '—'
        from .key_limits import usage
        u = usage(obj)
        from django.utils.html import format_html_join
        rows = [(label, u[field], f' з {getattr(obj, field)}' if getattr(obj, field) else '')
                for field, label in (('limit_hour', 'за цю годину'), ('limit_day', 'за сьогодні'),
                                     ('items_per_day', 'різних записів сьогодні'))]
        return format_html_join(' · ', '{}: <b>{}</b>{}', rows)

    @admin.display(description='Активність')
    def activity_panel(self, obj):
        if not obj.pk:
            return '—'
        from django.urls import reverse
        from django.utils.html import format_html_join
        from .key_limits import stats
        st = stats(obj)
        url = reverse('admin:api_apirequestlog_changelist') + f'?key__id__exact={obj.pk}'
        ips = format_html_join(', ', '<code>{}</code> ({}, {})', (
            (r['ip'], r['n'], timezone.localtime(r['last']).strftime('%d.%m %H:%M')) for r in st['ips'])) or '—'
        top = format_html_join(' · ', '{} {} — {}', ((r['method'], r['view'] or '?', r['n']) for r in st['top'])) or '—'
        recent = format_html_join('', '<tr><td>{}</td><td><code>{}</code></td><td>{} {}</td><td>{}</td><td>{}</td></tr>', (
            (timezone.localtime(r.created_at).strftime('%d.%m %H:%M:%S'), r.ip or '—', r.method, r.path,
             r.status, r.objects_text or r.note or '') for r in st['recent']))
        return format_html(
            '<div>Сьогодні: <b>{}</b> запитів{} · за 7 днів: <b>{}</b> · <a href="{}">📜 увесь журнал ключа</a></div>'
            '<div style="margin-top:4px">Адреси за 7 днів: {}</div>'
            '<div style="margin-top:4px">Найчастіше: {}</div>'
            '<table style="margin-top:8px;font-size:12px"><tr><th>Коли</th><th>IP</th><th>Запит</th><th>Код</th>'
            '<th>Що</th></tr>{}</table>',
            st['today'], format_html(', помилок <b style="color:var(--err)">{}</b>', st['today_errors'])
            if st['today_errors'] else '', st['week'], url, ips, top, recent)

    def scopes_display(self, obj):
        if not obj.scopes:
            return '—'
        labels = {k: v for k, v in SCOPE_CHOICES}
        return ', '.join(labels.get(s, s) for s in obj.scopes)
    scopes_display.short_description = 'Права'


# ── Вебхуки ───────────────────────────────────────────────────────────────────

class WebhookForm(forms.ModelForm):
    events_input = forms.MultipleChoiceField(
        choices=WEBHOOK_EVENTS, widget=forms.CheckboxSelectMultiple, required=False,
        label='Події', help_text='Нічого не обрано = всі події',
    )

    class Meta:
        model = Webhook
        fields = ['name', 'url', 'order_sources', 'is_active']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['events_input'].initial = self.instance.events

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.events = self.cleaned_data.get('events_input', [])
        if commit:
            instance.save()
        return instance


@admin.register(Webhook)
class WebhookAdmin(admin.ModelAdmin):
    form = WebhookForm
    list_display = ['name', 'url', 'events_display', 'is_active', 'health',
                    'last_delivery_at', 'api_key']
    list_filter = ['is_active']
    search_fields = ['name', 'url']
    readonly_fields = ['secret_display', 'api_key', 'created_at', 'last_delivery_at',
                       'last_status_code', 'consecutive_failures']
    actions = ['action_ping']
    fieldsets = [
        ('Загальне', {'fields': ['name', 'url', 'is_active']}),
        ('Що надсилати', {'fields': ['events_input', 'order_sources']}),
        ('Підпис і стан', {'fields': ['secret_display', 'api_key', 'created_at', 'last_delivery_at',
                                      'last_status_code', 'consecutive_failures']}),
    ]

    def events_display(self, obj):
        return ', '.join(obj.events) if obj.events else 'всі'
    events_display.short_description = 'Події'

    def health(self, obj):
        if obj.last_status_code is None and not obj.consecutive_failures:
            return '—'
        if obj.consecutive_failures:
            return format_html('<span style="color:#e53935">❌ {} невдач поспіль (HTTP {})</span>',
                               obj.consecutive_failures, obj.last_status_code or '—')
        return format_html('<span style="color:#43a047">✅ HTTP {}</span>', obj.last_status_code)
    health.short_description = 'Стан'

    def secret_display(self, obj):
        if not obj.pk:
            return '— буде згенеровано після збереження —'
        from core.secrets import reveal
        return reveal(obj.secret, format_html(
            'Той самий секрет — на сайті в private/shop-config.php (<code>webhook_secret</code>). Перевіряйте заголовок '
            '<code>X-Minerva-Signature</code>: HMAC-SHA256(secret, "&lt;t&gt;.&lt;body&gt;"). Приклад — api/README.md'))
    secret_display.short_description = 'Секрет підпису'

    @admin.action(description='📡 Надіслати тестову подію (ping)')
    def action_ping(self, request, queryset):
        from .webhooks import emit
        for hook in queryset:
            (d,) = emit('ping', {'message': 'Minerva webhook test', 'webhook': hook.name},
                        only=hook, sync=True)
            d.refresh_from_db()
            if d.status == WebhookDelivery.SUCCESS:
                self.message_user(request, f'{hook.name}: доставлено (HTTP {d.response_code})')
            else:
                self.message_user(request, f'{hook.name}: помилка — {d.response_code or ""} '
                                           f'{d.response_body[:200]}', level=messages.ERROR)


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'webhook', 'event', 'status', 'attempts', 'response_code',
                    'next_attempt_at', 'delivered_at']
    list_filter = ['status', 'event', 'webhook']
    readonly_fields = [f.name for f in WebhookDelivery._meta.fields]
    actions = ['action_retry']
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    @admin.action(description='🔁 Надіслати повторно зараз')
    def action_retry(self, request, queryset):
        from .webhooks import deliver
        ok = 0
        for d in queryset.select_related('webhook'):
            d.status, d.next_attempt_at = WebhookDelivery.PENDING, timezone.now()
            ok += deliver(d)
        self.message_user(request, f'Доставлено {ok} з {queryset.count()}')


# ── 📜 Журнал API ─────────────────────────────────────────────────────────────

class StatusClassFilter(admin.SimpleListFilter):
    title = 'Відповідь'
    parameter_name = 'st'

    def lookups(self, request, model_admin):
        return [('ok', '✅ Успішні (2xx)'), ('err', '⚠ Помилки (4xx / 5xx)'), ('401', '🔒 Ключ не прийнято (401)'),
                ('403', '⛔ Немає права (403)'), ('429', '⏳ Ліміт (429)'), ('5xx', '❌ Помилка сервера (5xx)')]

    def queryset(self, request, queryset):
        v = self.value()
        if v == 'ok':
            return queryset.filter(status__lt=400)
        if v == 'err':
            return queryset.filter(status__gte=400)
        if v == '5xx':
            return queryset.filter(status__gte=500)
        if v in ('401', '403', '429'):
            return queryset.filter(status=int(v))
        return queryset


class WriteFilter(admin.SimpleListFilter):
    title = 'Що робив'
    parameter_name = 'rw'

    def lookups(self, request, model_admin):
        return [('read', '👁 Читання (GET)'), ('write', '✏️ Зміни (POST / PATCH / PUT / DELETE)')]

    def queryset(self, request, queryset):
        if self.value() == 'read':
            return queryset.filter(method__in=['GET', 'HEAD'])
        if self.value() == 'write':
            return queryset.exclude(method__in=['GET', 'HEAD'])
        return queryset


@admin.register(APIRequestLog)
class APIRequestLogAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'key_col', 'ip', 'request_col', 'status_col', 'what_col', 'duration_ms']
    list_filter = [('key', admin.RelatedOnlyFieldListFilter), StatusClassFilter, WriteFilter, 'method']
    search_fields = ['ip', 'path', 'objects_text', 'key_name', 'key_hint', 'code', 'note', 'user_agent']
    date_hierarchy = 'created_at'
    list_per_page = 100
    list_select_related = ['key']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description='Ключ', ordering='key_name')
    def key_col(self, obj):
        if obj.key_id:
            from django.urls import reverse
            return format_html('<a href="{}">{}</a>', reverse('admin:api_apikey_change', args=[obj.key_id]),
                               obj.key_name or obj.key_id)
        return format_html('<span style="color:var(--err)">невідомий {}</span>', obj.key_hint or '')

    @admin.display(description='Запит', ordering='path')
    def request_col(self, obj):
        return format_html('<div style="min-width:240px;max-width:420px;white-space:normal;overflow-wrap:anywhere"><b>{}</b> <code>{}</code>{}{}</div>', obj.method, obj.path,
                           format_html('<small>?{}</small>', obj.query) if obj.query else '',
                           format_html('<br><small style="color:var(--text-muted)">{} · {}</small>', obj.view, obj.scope)
                           if obj.view else '')

    @admin.display(description='Код', ordering='status')
    def status_col(self, obj):
        color = 'var(--ok)' if obj.status < 400 else 'var(--warn)' if obj.status < 500 else 'var(--err)'
        return format_html('<b style="color:{}">{}</b>{}', color, obj.status,
                           format_html('<br><small>{}</small>', obj.code) if obj.code else '')

    @admin.display(description='Що')
    def what_col(self, obj):
        parts = []
        if obj.items is not None:
            parts.append(f'записів: {obj.items}')
        if obj.objects_text:
            parts.append(obj.objects_text)
        text = ' · '.join(parts)
        if not (text or obj.note):
            return '—'
        return format_html('<div style="min-width:220px;max-width:380px;white-space:normal">{}{}</div>', text,
                           format_html('<br><small style="color:var(--warn)">{}</small>', obj.note) if obj.note else '')
