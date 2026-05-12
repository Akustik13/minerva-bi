import json
import logging
from django import forms
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path
from django.utils.html import format_html
from .models import EmailAccount, EmailMessage, EmailThread, EmailDraft, EmailSettings, ScheduledEmail


class ScheduledEmailComposeForm(forms.ModelForm):
    to_raw = forms.CharField(
        required=True, label='Кому',
        widget=forms.TextInput(attrs={'autocomplete': 'off', 'id': 'se-to'}))
    cc_raw = forms.CharField(
        required=False, label='CC',
        widget=forms.TextInput(attrs={'autocomplete': 'off', 'id': 'se-cc'}))

    class Meta:
        model = ScheduledEmail
        fields = ('account', 'subject', 'to_raw', 'cc_raw',
                  'body', 'body_html', 'scheduled_at', 'status', 'trigger')
        widgets = {
            'body':      forms.HiddenInput(attrs={'id': 'se-body'}),
            'body_html': forms.HiddenInput(attrs={'id': 'se-body-html'}),
            'status':    forms.HiddenInput(),
            'trigger':   forms.HiddenInput(),
            'scheduled_at': forms.DateTimeInput(
                format='%Y-%m-%dT%H:%M',
                attrs={'type': 'datetime-local', 'id': 'se-scheduled-at'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        inst = self.instance
        if inst and inst.pk:
            self.initial['to_raw'] = ', '.join(inst.to_emails or [])
            self.initial['cc_raw'] = ', '.join(inst.cc_emails or [])
        if not (inst and inst.pk):
            self.initial.setdefault('status', ScheduledEmail.STATUS_PENDING)
            self.initial.setdefault('trigger', 'manual')

logger = logging.getLogger('email_assistant')


@admin.register(EmailAccount)
class EmailAccountAdmin(admin.ModelAdmin):
    list_display  = ('email_address', 'user', 'display_name', 'is_primary', 'is_active', 'last_sync_col')
    list_filter   = ('is_active', 'is_primary')
    search_fields = ('email_address', 'user__username', 'display_name')
    readonly_fields = ('last_sync_at', 'last_seen_uid', 'sync_all_widget', 'export_import_widget')

    fieldsets = (
        ("👤 Акаунт", {
            'fields': ('user', 'email_address', 'display_name', 'is_primary', 'is_active'),
        }),
        ("✍️ Підпис", {
            'fields': ('signature', 'signature_position'),
            'description': "HTML підпис. {name} = повне ім'я. Замінює підпис у профілі.",
        }),
        ("📥 IMAP (читання)", {
            'fields': ('imap_host', 'imap_port', 'imap_use_ssl',
                       'imap_username', 'imap_password',
                       'imap_folder_inbox', 'imap_folder_sent',
                       'sync_days_back', 'sync_interval_minutes',
                       'sync_limit', 'sync_no_limit',
                       'sync_all_widget'),
            'description': 'IONOS: host=imap.ionos.de, port=993, SSL=✓',
        }),
        ("📤 SMTP (відправка)", {
            'fields': ('smtp_host', 'smtp_port', 'smtp_use_tls', 'smtp_use_ssl',
                       'smtp_username', 'smtp_password'),
            'description': 'IONOS: host=smtp.ionos.de, port=587, TLS=✓',
        }),
        ("📊 Статус", {
            'fields': ('last_sync_at', 'last_seen_uid'),
            'classes': ('collapse',),
        }),
        ("💾 Експорт / Імпорт", {
            'fields': ('export_import_widget',),
            'description': 'Зберегти листи на ПК або завантажити листи з файлу.',
            'classes': ('collapse',),
        }),
    )

    def sync_all_widget(self, obj):
        if not obj or not obj.pk:
            return '—'
        return format_html(
            '<button type="button" id="sync-all-btn" '
            'style="padding:7px 18px;border-radius:6px;font-size:12px;font-weight:600;'
            'background:linear-gradient(135deg,#1565c0,#1a73e8);color:#fff;'
            'border:none;cursor:pointer;transition:opacity .15s" '
            'onclick="doSyncAll({})">🔄 Синхронізувати все (всі папки, вся історія)</button>'
            '<span id="sync-all-status" style="font-size:12px;margin-left:10px;color:var(--text-muted)"></span>'
            '<pre id="sync-all-log" style="display:none;margin-top:8px;padding:8px;'
            'background:var(--bg-input);border-radius:6px;font-size:11px;'
            'color:var(--text-muted);max-height:200px;overflow-y:auto;white-space:pre-wrap"></pre>',
            obj.pk,
        )
    sync_all_widget.short_description = 'Синхронізувати все'

    def export_import_widget(self, obj):
        if not obj or not obj.pk:
            return '—'
        export_url = f'/email/export/'
        return format_html(
            '<div style="display:flex;flex-wrap:wrap;gap:10px;align-items:flex-start">'
            '<a href="{}" target="_blank" '
            'style="padding:7px 14px;border-radius:6px;font-size:12px;font-weight:600;'
            'background:linear-gradient(135deg,#2e7d32,#388e3c);color:#fff;'
            'text-decoration:none;display:inline-block">📥 Завантажити листи (.zip)</a>'
            '<label style="display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--text-muted)">'
            '<span>📤 Завантажити з ПК (.eml або .zip):</span>'
            '<input type="file" id="eml-import-file" accept=".eml,.zip" '
            'style="font-size:12px">'
            '<button type="button" onclick="doImportEmails()"'
            'style="padding:5px 12px;border-radius:6px;font-size:12px;font-weight:600;'
            'background:var(--bg-card);color:var(--text);border:1px solid var(--border-strong);cursor:pointer;width:fit-content">'
            '📤 Імпортувати</button>'
            '</label>'
            '<span id="import-status" style="font-size:12px;color:var(--text-muted);align-self:center"></span>'
            '</div>',
            export_url,
        )
    export_import_widget.short_description = 'Листи'

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('<int:pk>/sync-all/',
                 self.admin_site.admin_view(self._sync_all_view),
                 name='emailaccount_sync_all'),
        ]
        return custom + urls

    def _sync_all_view(self, request, pk):
        """Admin AJAX: full historical sync — streams NDJSON progress lines."""
        import json as _json
        from django.http import StreamingHttpResponse
        from email_assistant.imap_client import IMAPClient
        from email_assistant.models import EmailThread

        def _ev(d):
            return _json.dumps(d, ensure_ascii=False) + '\n'

        def _imap_err(e):
            """Strip b'...' bytes notation from imaplib error messages."""
            msg = str(e)
            if (msg.startswith("b'") and msg.endswith("'")) or \
               (msg.startswith('b"') and msg.endswith('"')):
                msg = msg[2:-1]
            return msg

        def _save_msg(account, msg_data, folder_name, folder_type):
            """Save one message to DB. Returns True if created, False if duplicate."""
            if EmailMessage.objects.filter(
                    account=account, imap_uid=msg_data['uid'],
                    imap_folder_name=folder_name).exists():
                return False
            tk = (msg_data.get('in_reply_to') or
                  msg_data.get('message_id') or msg_data['subject'])
            thread = EmailThread.objects.filter(
                account=account, thread_id=tk[:500]).first()
            if not thread:
                thread = EmailThread.objects.create(
                    account=account,
                    thread_id=tk[:500] if tk else '',
                    subject=msg_data['subject'][:500],
                    participants=[msg_data['from_email']] + msg_data['to_emails'],
                )
            EmailMessage.objects.create(
                account=account, thread=thread,
                imap_uid=msg_data['uid'], imap_folder_name=folder_name,
                message_id=msg_data['message_id'], in_reply_to=msg_data['in_reply_to'],
                folder=folder_type,
                subject=msg_data['subject'], from_email=msg_data['from_email'],
                from_name=msg_data['from_name'], to_emails=msg_data['to_emails'],
                cc_emails=msg_data['cc_emails'], body_text=msg_data['body_text'],
                body_html=msg_data['body_html'], attachments=msg_data['attachments'],
                is_read=msg_data['is_read'], sent_at=msg_data['sent_at'],
            )
            return True

        def generate(account):
            yield _ev({'type': 'step', 'msg': '🔄 Перевіряю підключення до IMAP…'})

            try:
                with IMAPClient(account) as _chk:
                    pass
            except Exception as e:
                yield _ev({'type': 'error',
                           'msg': f'Помилка автентифікації IMAP: {_imap_err(e)}. '
                                  'Перевірте логін/пароль та налаштування сервера.'})
                return

            _lim = None if account.sync_no_limit else account.sync_limit

            # 1. Sync inbox + sent with per-message progress
            yield _ev({'type': 'step', 'msg': '📥 Синхронізую Вхідні та Надіслані…'})
            inbox_new = 0
            for _fname, _ftype in [
                (account.imap_folder_inbox, EmailMessage.FOLDER_INBOX),
                (account.imap_folder_sent,  EmailMessage.FOLDER_SENT),
            ]:
                if not _fname:
                    continue
                yield _ev({'type': 'inbox_folder_start', 'folder': _fname})
                try:
                    with IMAPClient(account) as _imap:
                        _msgs = _imap.fetch_messages(
                            folder=_fname, days_back=3650, since_uid=0, limit=_lim)
                    _total = len(_msgs)
                    yield _ev({'type': 'inbox_folder_info', 'folder': _fname, 'total': _total})
                    _created = 0
                    for _i, _m in enumerate(_msgs):
                        if _i % 20 == 0 and _i > 0:
                            yield _ev({'type': 'inbox_progress', 'folder': _fname,
                                       'scanned': _i, 'created': _created, 'total': _total})
                        try:
                            if _save_msg(account, _m, _fname, _ftype):
                                _created += 1
                        except Exception:
                            pass
                    inbox_new += _created
                    yield _ev({'type': 'inbox_folder_done', 'folder': _fname,
                               'created': _created, 'total': _total})
                except Exception as _e:
                    yield _ev({'type': 'folder_error', 'folder': _fname,
                               'error': _imap_err(_e), 'traceback': ''})

            # 2. List all IMAP folders
            yield _ev({'type': 'step', 'msg': '📂 Отримую список папок…'})
            try:
                with IMAPClient(account) as imap:
                    imap_folders = imap.list_folders()
            except Exception as e:
                yield _ev({'type': 'error', 'msg': f'list_folders: {_imap_err(e)}'})
                return

            standard = {account.imap_folder_inbox.lower(), account.imap_folder_sent.lower()}
            extra = [f for f in imap_folders
                     if f.get('selectable') and f['name'].lower() not in standard]
            yield _ev({'type': 'folders_found', 'count': len(extra),
                       'names': [f['name'] for f in extra]})

            # 3. Sync each custom folder
            total_extra = 0
            for f in extra:
                name = f['name']
                yield _ev({'type': 'folder_start', 'folder': name})
                created = 0
                try:
                    with IMAPClient(account) as imap:
                        messages = imap.fetch_messages(folder=name,
                                                       days_back=account.sync_days_back,
                                                       since_uid=0, limit=_lim)
                    total_f = len(messages)
                    yield _ev({'type': 'folder_info', 'folder': name, 'total': total_f})
                    for i, msg_data in enumerate(messages):
                        if i % 20 == 0 and i > 0:
                            yield _ev({'type': 'folder_progress', 'folder': name,
                                       'created': created, 'scanned': i, 'total': total_f})
                        try:
                            if _save_msg(account, msg_data, name, EmailMessage.FOLDER_INBOX):
                                created += 1
                        except Exception:
                            pass
                    total_extra += created
                    yield _ev({'type': 'folder_done', 'folder': name,
                               'created': created, 'total': total_f})
                except BaseException as e:
                    import traceback as _tb
                    tb = _tb.format_exc()
                    logger.error('sync-all folder %s failed:\n%s', name, tb)
                    yield _ev({'type': 'folder_error', 'folder': name,
                               'error': str(e), 'traceback': tb[-500:]})

            yield _ev({'type': 'done', 'inbox_new': inbox_new, 'extra_new': total_extra})

        try:
            account = EmailAccount.objects.get(pk=pk)
        except EmailAccount.DoesNotExist:
            return JsonResponse({'ok': False, 'error': 'Акаунт не знайдено'})

        resp = StreamingHttpResponse(generate(account), content_type='application/x-ndjson')
        resp['X-Accel-Buffering'] = 'no'   # disable nginx proxy buffering
        resp['Cache-Control'] = 'no-cache'
        return resp

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        for f in ('imap_password', 'smtp_password'):
            if f in form.base_fields:
                form.base_fields[f].widget.input_type = 'password'
        return form

    def last_sync_col(self, obj):
        if not obj.last_sync_at:
            return format_html('<span style="color:var(--text-dim)">—</span>')
        from django.utils.formats import date_format
        return date_format(obj.last_sync_at, 'd.m.Y H:i')
    last_sync_col.short_description = 'Остання синхронізація'

    def has_module_perms(self, request):
        return request.user.is_superuser or getattr(
            getattr(request.user, 'profile', None), 'role', '') in ('superadmin', 'admin')


@admin.register(EmailSettings)
class EmailSettingsAdmin(admin.ModelAdmin):
    list_display = ('user', 'ai_auto_suggest_reply', 'sync_to_crm_timeline', 'mark_read_on_server', 'telegram_notify_new')
    fieldsets = (
        ("🤖 AI функції", {
            'fields': ('ai_auto_suggest_reply', 'ai_auto_translate', 'ai_translate_to',
                       'deadline_detection'),
        }),
        ("🔄 Автовідповідь", {
            'fields': ('auto_reply_enabled', 'auto_reply_mode', 'auto_reply_prompt'),
            'description': 'Minerva AI генерує відповідь автоматично при отриманні нового листа.',
        }),
        ("🛒 Тригер: нове замовлення", {
            'fields': ('order_trigger_enabled',),
            'description': 'При створенні нового замовлення AI генерує чернетку листа клієнту.',
        }),
        ("✍️ Підпис (застарілий — використовуй підпис акаунту)", {
            'fields': ('auto_signature', 'signature_position', 'signature'),
            'classes': ('collapse',),
        }),
        ("🖥️ Інтерфейс", {
            'fields': ('show_admin_sidebar',),
        }),
        ("📬 Читання та синхронізація", {
            'fields': ('mark_read_on_server', 'spam_folder'),
        }),
        ("🔗 CRM синхронізація", {
            'fields': ('sync_to_crm_timeline',),
        }),
        ("📱 Telegram сповіщення", {
            'fields': ('telegram_notify_new', 'telegram_quiet_from', 'telegram_quiet_to'),
            'description': 'Тихий режим: якщо quiet_from < quiet_to — блокує в цьому проміжку. Якщо quiet_from > quiet_to — блокує через північ.',
        }),
    )


@admin.register(EmailMessage)
class EmailMessageAdmin(admin.ModelAdmin):
    list_display  = ('subject_col', 'from_email', 'folder', 'is_read', 'attach_col', 'sent_at')
    list_filter   = ('folder', 'is_read', 'account')
    search_fields = ('subject', 'from_email', 'body_text')
    readonly_fields = ('account', 'thread', 'imap_uid', 'message_id', 'from_email',
                       'to_emails', 'cc_emails', 'body_text', 'body_html',
                       'sent_at', 'created_at', 'ai_summary', 'ai_reply_draft', 'ai_translated')

    def subject_col(self, obj):
        return obj.subject[:70] or '(без теми)'
    subject_col.short_description = 'Тема'

    def attach_col(self, obj):
        if obj.has_attachments:
            return format_html('<span title="{} вкладень">📎</span>', len(obj.attachments))
        return ''
    attach_col.short_description = '📎'

    def has_add_permission(self, request):
        return False

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(account__user=request.user)


@admin.register(EmailThread)
class EmailThreadAdmin(admin.ModelAdmin):
    list_display  = ('subject_col', 'account', 'message_count', 'has_unread',
                     'crm_customer', 'last_message_at')
    list_filter   = ('has_unread', 'is_archived')
    search_fields = ('subject',)
    readonly_fields = ('account', 'thread_id', 'participants', 'message_count',
                       'last_message_at', 'crm_customer')

    def subject_col(self, obj):
        return obj.subject[:70]
    subject_col.short_description = 'Тема'

    def has_add_permission(self, request):
        return False

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(account__user=request.user)


@admin.register(ScheduledEmail)
class ScheduledEmailAdmin(admin.ModelAdmin):
    form                = ScheduledEmailComposeForm
    change_form_template = 'admin/email_assistant/scheduledemail/change_form.html'

    list_display    = ('subject_col', 'account', 'to_col', 'status', 'scheduled_at', 'sent_at')
    list_filter     = ('status', 'trigger', 'account')
    search_fields   = ('subject',)
    readonly_fields = ('sent_at', 'error_msg', 'created_at')

    def subject_col(self, obj):
        return obj.subject[:60]
    subject_col.short_description = 'Тема'

    def to_col(self, obj):
        return ', '.join(obj.to_emails[:2])
    to_col.short_description = 'Кому'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(account__user=request.user)

    def save_model(self, request, obj, form, change):
        to_raw = form.cleaned_data.get('to_raw', '')
        cc_raw = form.cleaned_data.get('cc_raw', '')
        obj.to_emails = [e.strip() for e in to_raw.split(',') if e.strip()]
        obj.cc_emails = [e.strip() for e in cc_raw.split(',') if e.strip()]
        if not change:
            obj.status  = ScheduledEmail.STATUS_PENDING
            obj.trigger = 'manual'
        super().save_model(request, obj, form, change)

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        from email_assistant.views import _crm_contacts
        extra_context = extra_context or {}

        accounts = EmailAccount.objects.filter(is_active=True)
        if not request.user.is_superuser:
            accounts = accounts.filter(user=request.user)

        extra_context['crm_contacts_json'] = json.dumps(_crm_contacts(request.user))
        extra_context['account_sigs_json'] = json.dumps({
            str(a.pk): {
                'signature':    a.signature or '',
                'display_name': a.display_name or a.email_address,
                'email':        a.email_address,
            }
            for a in accounts
        })
        if object_id:
            try:
                obj = ScheduledEmail.objects.get(pk=object_id)
                extra_context['existing_body_html'] = json.dumps(obj.body_html or '')
                extra_context['existing_body_text'] = json.dumps(obj.body or '')
            except ScheduledEmail.DoesNotExist:
                pass
        return super().changeform_view(request, object_id, form_url, extra_context)
