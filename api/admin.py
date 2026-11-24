from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join
from django.contrib import messages
from django.utils import timezone

from .models import (APIKey, APIRequestLog, OAuthClient, OAuthToken, SCOPE_CHOICES, Webhook, WebhookDelivery,
                     WEBHOOK_EVENTS)


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
            if self.instance.oauth_client_id:      # 🔌 OAuth: права — ті, що людина дозволила у вікні «Дозволити»
                self.fields['scopes_input'].disabled = True

    request_user = None           # хто зберігає (APIKeyAdmin.get_form) — власник нового ключа

    def clean(self):
        data = super().clean()
        owner = self.instance.owner if self.instance.owner_id else self.request_user
        agent = data.get('agent_user')
        if data.get('private') and agent is not None and owner is not None and agent.pk != owner.pk:
            self.add_error('agent_user', '🔒 Особистий ключ — агент лише від імені власника ключа ({}).'.format(
                owner.get_full_name() or owner.get_username()))
        return data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.scopes = self.cleaned_data.get('scopes_input', [])
        if commit:
            instance.save()
        return instance


@admin.register(APIKey)
class APIKeyAdmin(admin.ModelAdmin):
    form = APIKeyForm
    list_display  = ['name', 'key_preview', 'owner_col', 'scopes_display', 'is_active',
                     'last_used', 'activity_col', 'limits_col', 'expires_at', 'created_at']
    list_filter   = ['is_active', 'private']
    search_fields = ['name']
    readonly_fields = ['key_display', 'key_hidden_display', 'owner_display', 'created_at', 'last_used',
                       'usage_display', 'activity_panel', 'insight_panel', 'oauth_display']
    OAUTH_LOCKED = ('agent_user', 'private', 'shop', 'default_source')
    actions = ['action_deactivate']

    fieldsets = [
        ('Загальне', {
            'fields': ['name', 'is_active', 'expires_at', 'shop', 'default_source', 'private', 'owner_display'],
        }),
        ('Права доступу', {
            'fields': ['scopes_input'],
            'description': 'Оберіть які операції дозволені для цього ключа.',
        }),
        ('🤖 Агент (MCP)', {
            'fields': ['agent_user'],
            'description': 'Для зовнішніх агентів (Claude Code, Claude Desktop): адреса <code>/api/v1/mcp/</code>, '
                           'права «Агент (MCP) — читання / пропонувати зміни». Пропозиції стають завданням цієї людини '
                           'в «🧩 Завдання агенту» — застосовує вона ✓. Як підключити — гід «Агент через MCP».',
        }),
        ('🔍 Що бачить і що може змінити цей ключ', {
            'fields': ['insight_panel'],
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

    # ── 🔒 Особистий ключ: повний ключ бачить і змінює лише власник ─────────────
    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.request_user = request.user
        return form

    def get_fieldsets(self, request, obj=None):
        sets = super().get_fieldsets(request, obj)
        if obj is not None and obj.hidden_from(request.user):     # чужий особистий — без повного ключа на сторінці
            sets = [(title, {**opts, 'fields': ['key_hidden_display' if f == 'key_display' else f
                                                for f in opts['fields']]}) for title, opts in sets]
        if obj is not None and obj.oauth_client_id:               # 🔌 OAuth: сам ключ не діє — лише токени
            sets = [(title, {**opts, 'fields': ([] if i else ['oauth_display']) + [
                f for f in opts['fields'] if f not in ('key_display', 'key_hidden_display')]})
                for i, (title, opts) in enumerate(sets)]
        return sets

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.oauth_client_id:
            fields += [f for f in self.OAUTH_LOCKED if f not in fields]
        return fields

    @admin.display(description='🔌 Підключення OAuth')
    def oauth_display(self, obj):
        from django.utils import timezone as tz
        c = obj.oauth_client if obj and obj.oauth_client_id else None
        if c is None:
            return '—'
        live = obj.oauth_tokens.filter(kind=OAuthToken.REFRESH, used_at__isnull=True, expires_at__gt=tz.now())
        last = obj.oauth_tokens.exclude(kind=OAuthToken.CODE).order_by('-created_at').first()
        hosts = ', '.join(sorted({(u.split('://', 1)[-1].split('/', 1)[0] or u) for u in c.redirect_uris}))
        allowed = ['👁 бачити дані'] + (['✍️ пропонувати зміни'] if 'agent:write' in obj.scopes else []) + (
            ['🔒 пошта, календар, робочий час'] if last is not None and last.personal else [])
        return format_html(
            '<b translate="no">{}</b> · повернення на <code translate="no">{}</code> · зареєстровано {}<br>'
            'Дозволено: {} · {}<div style="margin-top:6px;font-size:12px;opacity:.8">Підключено через «Дозволити» в '
            'Minerva — права змінити не можна: підключіть програму знову. Вимкнути — зніміть «Активний» (токени '
            'перестануть діяти одразу).</div>',
            c.name, hosts or '—', tz.localtime(c.created_at).strftime('%d.%m.%Y %H:%M'), ' · '.join(allowed),
            'підключення діє' if live.exists() else 'токенів немає — програма має підключитись знову')

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.hidden_from(request.user):
            return False                    # лише перегляд (замасковано); вимкнути — «⏸ Вимкнути», видалити — можна
        return super().has_change_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if not obj.owner_id and (not change or obj.private):    # новий ключ / старий без власника стає особистим
            obj.owner = request.user
        super().save_model(request, obj, form, change)

    @admin.action(description='⏸ Вимкнути вибрані ключі (і особисті — на випадок витоку)', permissions=['delete'])
    def action_deactivate(self, request, queryset):
        n = queryset.filter(is_active=True).update(is_active=False)
        self.message_user(request, f'Вимкнено ключів: {n}. Увімкнути особистий ключ може лише його власник.')

    @admin.display(description='Власник')
    def owner_col(self, obj):
        who = (obj.owner.get_full_name() or obj.owner.get_username()) if obj.owner_id else '—'
        return format_html('{}{}{}', '🔌 ' if obj.oauth_client_id else '', '🔒 ' if obj.private else '', who)

    @admin.display(description='Власник')
    def owner_display(self, obj):
        if not obj or not obj.pk:
            return 'ви (той, хто створює ключ)'
        who = (obj.owner.get_full_name() or obj.owner.get_username()) if obj.owner_id else '— (створено до 11.2026)'
        return format_html('{}{}', who, ' · 🔒 особистий: повний ключ бачить і змінює лише власник'
                           if obj.private else ' · спільний: бачать усі, кому відкриті ключі API')

    @admin.display(description='API Ключ')
    def key_hidden_display(self, obj):
        from core.secrets import mask
        who = (obj.owner.get_full_name() or obj.owner.get_username()) if obj.owner_id else '?'
        return format_html('<code>{}</code><div style="margin-top:6px;font-size:12px;opacity:.8">🔒 Особистий ключ '
                           '{} — повністю бачить і змінює лише власник. Вимкнути (на випадок витоку): дія «⏸ Вимкнути '
                           'вибрані ключі» у списку.</div>', mask(obj.key), who)

    def change_view(self, request, object_id, form_url='', extra_context=None):
        self._base_url = request.build_absolute_uri('/').rstrip('/')
        return super().change_view(request, object_id, form_url, extra_context)

    def key_preview(self, obj):
        from core.secrets import mask
        return ('🔒 ' if obj.private else '') + mask(obj.key)
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
            '<div>Сьогодні: <b>{}</b> запитів{} · за 7 днів: <b>{}</b> · <a href="{}">📜 увесь журнал ключа</a> · '
            '<a href="{}">📊 Аналіз журналу ключа</a></div>'
            '<div style="margin-top:4px">Адреси за 7 днів: {}</div>'
            '<div style="margin-top:4px">Найчастіше: {}</div>'
            '<table style="margin-top:8px;font-size:12px"><tr><th>Коли</th><th>IP</th><th>Запит</th><th>Код</th>'
            '<th>Що</th></tr>{}</table>',
            st['today'], format_html(', помилок <b style="color:var(--err)">{}</b>', st['today_errors'])
            if st['today_errors'] else '', st['week'], url,
            reverse('admin:api_apirequestlog_analysis') + f'?key={obj.pk}', ips, top, recent)

    @admin.display(description='Можливості ключа')
    def insight_panel(self, obj):
        if not obj.pk:
            return '— після збереження —'
        from .key_insight import capabilities, summary
        sm = summary(capabilities(obj, detail=False))
        writes = ', '.join(sm['write_paths'][:6]) + (f' … +{len(sm["write_paths"]) - 6}' if len(sm['write_paths']) > 6 else '')
        return format_html(
            '<div>Читає адрес: <b>{}</b> · змінює: <b>{}</b>{} · заборонено правами: {}</div>'
            '<div style="margin-top:8px"><a class="button" href="{}">🔍 Таблиця можливостей, приклад даних і симуляція</a> '
            '<a class="button" href="{}">📊 Аналіз журналу ключа</a></div>',
            sm['read'], sm['write'], format_html(' <small>({})</small>', writes) if writes else '', sm['denied'],
            reverse('admin:api_apikey_insight', args=[obj.pk]),
            reverse('admin:api_apirequestlog_analysis') + f'?key={obj.pk}')

    # ── 🔍 Що бачить і що може змінити ключ (api/key_insight.py) ──

    def get_urls(self):
        view = self.admin_site.admin_view
        return [
            path('<int:pk>/insight/', view(self.insight_view), name='api_apikey_insight'),
            path('<int:pk>/insight/preview/', view(self.preview_view), name='api_apikey_preview'),
            path('<int:pk>/insight/simulate/', view(self.simulate_view), name='api_apikey_simulate'),
        ] + super().get_urls()

    def insight_view(self, request, pk):
        from . import key_insight as ki
        key = get_object_or_404(APIKey, pk=pk)
        if not self.has_view_permission(request, key):
            raise PermissionDenied
        ctx = ki.KeyContext(key)
        rows = ki.capabilities(key, ctx)
        can_run = self.has_change_permission(request, key)
        context = {
            **self.admin_site.each_context(request), 'opts': self.model._meta, 'key': key,
            'title': f'🔍 Що бачить і що може змінити ключ «{key.name}»',
            'restrictions': ki.restrictions(key, ctx), 'groups': ki.grouped(rows), 'sum': ki.summary(rows),
            'can_run': can_run, 'preview_max': ki.PREVIEW_MAX_ROWS, 'preview_rows': ki.PREVIEW_ROWS,
            'preview_choices': [r for r in rows if r['preview'] and r['access'] == 'allowed'],
            'simulate_choices': [r for r in rows if r['simulate'] and r['access'] == 'allowed'],
            'examples': ki.examples(rows),
            'change_url': reverse('admin:api_apikey_change', args=[key.pk]),
            'log_url': reverse('admin:api_apirequestlog_changelist') + f'?key__id__exact={key.pk}',
            'analysis_url': reverse('admin:api_apirequestlog_analysis') + f'?key={key.pk}',
        }
        return TemplateResponse(request, 'admin/api/apikey/insight.html', context)

    def _run_as_key(self, request, pk):
        """Запуск від імені ключа — лише POST і лише з правом змінювати ключі API."""
        if request.method != 'POST':
            return None, HttpResponseNotAllowed(['POST'])
        key = get_object_or_404(APIKey, pk=pk)
        if not self.has_change_permission(request, key):
            raise PermissionDenied
        return key, None

    @staticmethod
    def _audit(request, key, what, target):
        """Хто дивився дані як ключ — у журнал аудиту Minerva (журнал API — лише дії самих програм)."""
        try:
            from core.models import AuditLog
            AuditLog.log(AuditLog.Action.VIEW, module='api', model_name='apikey', object_id=key.pk,
                         object_repr=key.name, extra={'what': what, 'path': target[:300]}, request=request)
        except Exception:  # noqa: BLE001 — аудит не ламає сторінку
            pass

    def preview_view(self, request, pk):
        key, refused = self._run_as_key(request, pk)
        if refused:
            return refused
        from .key_insight import preview
        target = request.POST.get('path', '')
        result = preview(request, key, target, request.POST.get('query', ''))
        self._audit(request, key, '👁 Приклад даних як ключ', result.get('url') or target)
        return JsonResponse(result, json_dumps_params={'ensure_ascii': False})

    def simulate_view(self, request, pk):
        key, refused = self._run_as_key(request, pk)
        if refused:
            return refused
        from .key_insight import simulate
        target = request.POST.get('path', '')
        result = simulate(key, request.POST.get('method', ''), target, request.POST.get('body', ''))
        self._audit(request, key, '🧪 Симуляція запиту ключа (без запису)', f"{request.POST.get('method', '')} {target}")
        return JsonResponse(result, json_dumps_params={'ensure_ascii': False})

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


@admin.register(OAuthClient)
class OAuthClientAdmin(admin.ModelAdmin):
    """🔌 Програми, що зареєструвались для OAuth MCP (api/oauth.py). Підключення людей — у «🔑 API ключі» (🔌);
    видалити програму — видалити й усі її підключення."""
    list_display = ['name', 'hosts_col', 'keys_col', 'created_at', 'created_ip']
    search_fields = ['name', 'client_id']
    readonly_fields = ['name', 'client_id', 'redirect_uris', 'client_uri', 'created_at', 'created_ip', 'keys_col']
    fields = readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description='Адреси повернення')
    def hosts_col(self, obj):
        return ', '.join(sorted({(u.split('://', 1)[-1].split('/', 1)[0] or u) for u in obj.redirect_uris}))

    @admin.display(description='Підключення')
    def keys_col(self, obj):
        keys = list(obj.keys.select_related('owner'))
        if not keys:
            return 'ще ніхто не дозволив'
        return format_html_join(', ', '<a href="{}">{}{}</a>', (
            (reverse('admin:api_apikey_change', args=[k.pk]),
             (k.owner.get_full_name() or k.owner.get_username()) if k.owner_id else '?',
             '' if k.is_active else ' (вимкнено)') for k in keys))


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
    list_display = ['created_at', 'key_col', 'plain_col', 'ip', 'request_col', 'status_col', 'what_col',
                    'duration_ms']
    list_filter = [('key', admin.RelatedOnlyFieldListFilter), StatusClassFilter, WriteFilter, 'method']
    search_fields = ['ip', 'path', 'objects_text', 'key_name', 'key_hint', 'code', 'note', 'user_agent']
    date_hierarchy = 'created_at'
    list_per_page = 100
    list_select_related = ['key']
    readonly_fields = ['created_at', 'plain_display', 'objects_display', 'result_display']
    fieldsets = [
        ('Що сталося', {'fields': ['plain_display']}),
        ('Хто й звідки', {'fields': ['created_at', 'key', 'key_name', 'key_hint', 'ip', 'user_agent']}),
        ('Запит', {'fields': ['method', 'path', 'query', 'view', 'scope']}),
        ('Відповідь', {'fields': ['status', 'code', 'items', 'objects_display', 'result_display', 'note',
                                  'duration_ms']}),
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        key = request.GET.get('key__id__exact', '')
        query = f'key={key}' if key.isdigit() else ''
        return super().changelist_view(request, {'analysis_query': query, **(extra_context or {})})

    @admin.display(description='Що сталося')
    def plain_col(self, obj):
        from .log_analysis import plain_line
        return format_html('<div style="min-width:240px;max-width:420px;white-space:normal">{}</div>', plain_line(obj))

    @admin.display(description='Що сталося')
    def plain_display(self, obj):
        from .log_analysis import plain_line
        return format_html('<b>{}</b>', plain_line(obj))

    @admin.display(description='Усі записи відповіді')
    def objects_display(self, obj):
        """Кожен запис, який ключ отримав, — з посиланням на товар / замовлення в Minerva."""
        from django.utils.html import format_html_join
        from .log_analysis import record_links
        records = record_links(obj)
        if not records:
            return obj.objects_text or '—'
        more = (obj.items or 0) - len(records)
        return format_html(
            '<div style="max-height:260px;overflow:auto;line-height:1.7">{}</div>{}',
            format_html_join(', ', '{}', ((format_html('<a href="{}">{}</a>', r['url'], r['value'])
                                           if r['url'] else r['value'],) for r in records)),
            format_html('<div style="margin-top:4px"><small>ще {} записів не збережено (ліміт {})</small></div>',
                        more, len(records)) if more > 0 and obj.object_ids else '')

    @admin.display(description='Що змінилось')
    def result_display(self, obj):
        if not obj.result:
            return '—'
        return format_html('<pre style="margin:0;font-size:12px;white-space:pre-wrap">{}</pre>',
                           json.dumps(obj.result, ensure_ascii=False, indent=2))

    # ── 📊 Аналіз журналу (api/log_analysis.py) ──

    def get_urls(self):
        view = self.admin_site.admin_view
        return [path('analysis/', view(self.analysis_view), name='api_apirequestlog_analysis')] + super().get_urls()

    def analysis_view(self, request):
        from . import log_analysis as la
        if not self.has_view_permission(request):
            raise PermissionDenied
        params = request.GET
        start, end, days = la.period(params)
        qs = la.filtered(params)
        key = params.get('key', '')
        if params.get('export') == 'csv':
            name = f"api-log-{key or 'all'}-{start:%Y%m%d}-{end:%Y%m%d}.csv"
            return la.csv_response(qs, name)
        keys = list(APIKey.objects.order_by('name').values('pk', 'name', 'is_active'))
        selected = next((k for k in keys if str(k['pk']) == key), None)
        export = request.GET.copy()
        export['export'] = 'csv'
        context = {
            **self.admin_site.each_context(request), 'opts': self.model._meta,
            'title': '📊 Аналіз журналу API' + (f" — {selected['name']}" if selected else ''),
            'keys': keys, 'key': key, 'selected': selected, 'start': start, 'end': end, 'days': days,
            'periods': la.PERIODS, 'st': params.get('st', ''), 'rw': params.get('rw', ''),
            'a': la.analyse(qs, start, end), 'csv_url': '?' + export.urlencode(),
            'log_url': reverse('admin:api_apirequestlog_changelist') + (f'?key__id__exact={key}' if key.isdigit() else ''),
            'key_url': reverse('admin:api_apikey_change', args=[key]) if selected else '',
            'insight_url': reverse('admin:api_apikey_insight', args=[key]) if selected else '',
            'records_show': la.RECORDS_SHOW, 'scan_max': la.RECORDS_SCAN_MAX,
        }
        return TemplateResponse(request, 'admin/api/apirequestlog/analysis.html', context)

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
