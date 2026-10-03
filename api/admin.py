from django import forms
from django.contrib import admin
from django.utils.html import format_html
from django.contrib import messages
from django.utils import timezone

from .models import APIKey, SCOPE_CHOICES, Webhook, WebhookDelivery, WEBHOOK_EVENTS


class APIKeyForm(forms.ModelForm):
    scopes_input = forms.MultipleChoiceField(
        choices=SCOPE_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label='Права доступу (scopes)',
    )

    class Meta:
        model = APIKey
        fields = ['name', 'is_active', 'expires_at', 'shop', 'default_source']

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
                     'last_used', 'expires_at', 'created_at']
    list_filter   = ['is_active']
    search_fields = ['name']
    readonly_fields = ['key_display', 'created_at', 'last_used']

    fieldsets = [
        ('Загальне', {
            'fields': ['name', 'is_active', 'expires_at', 'shop', 'default_source'],
        }),
        ('Права доступу', {
            'fields': ['scopes_input'],
            'description': 'Оберіть які операції дозволені для цього ключа.',
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
        return f"{obj.key[:8]}…{obj.key[-4:]}"
    key_preview.short_description = 'Ключ'

    def key_display(self, obj):
        if obj.pk:
            return format_html(
                '<code style="font-size:13px;user-select:all;'
                'background:#0d1117;padding:6px 10px;border-radius:4px;'
                'border:1px solid #2a3f52;display:inline-block;">{}</code>'
                '<div style="margin-top:8px;font-size:12px;opacity:.8">Перевірка підключення:</div>'
                '<code style="font-size:12px;user-select:all;display:inline-block;margin-top:4px">'
                'curl -H "Authorization: Token {}" {}/api/v1/ping/</code>'
                '<div style="margin-top:6px;font-size:12px;opacity:.8">'
                'Документація: <code>api/README.md</code> · '
                '<a href="/dashboard/api/console/?preset=minerva_ping">Консоль API</a></div>',
                obj.key, obj.key, self._base_url,
            )
        return '— буде згенеровано після збереження —'
    key_display.short_description = 'API Ключ (скопіюй)'

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
        return format_html(
            '<code style="user-select:all">{}</code><div style="font-size:12px;opacity:.8;margin-top:6px">'
            'Перевіряйте заголовок <code>X-Minerva-Signature</code>: '
            'HMAC-SHA256(secret, "&lt;t&gt;.&lt;body&gt;"). Приклад — api/README.md</div>', obj.secret)
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
