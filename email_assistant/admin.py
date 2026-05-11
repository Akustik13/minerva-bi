from django.contrib import admin
from django.http import JsonResponse
from django.urls import path
from django.utils.html import format_html
from .models import EmailAccount, EmailMessage, EmailThread, EmailDraft, EmailSettings


@admin.register(EmailAccount)
class EmailAccountAdmin(admin.ModelAdmin):
    list_display  = ('email_address', 'user', 'display_name', 'is_primary', 'is_active', 'last_sync_col')
    list_filter   = ('is_active', 'is_primary')
    search_fields = ('email_address', 'user__username', 'display_name')
    readonly_fields = ('last_sync_at', 'last_seen_uid', 'sync_all_widget')

    fieldsets = (
        ("👤 Акаунт", {
            'fields': ('user', 'email_address', 'display_name', 'is_primary', 'is_active'),
        }),
        ("✍️ Підпис", {
            'fields': ('signature',),
            'description': "HTML підпис для листів з цього акаунту. {name} = повне ім'я. Замінює підпис у профілі.",
        }),
        ("📥 IMAP (читання)", {
            'fields': ('imap_host', 'imap_port', 'imap_use_ssl',
                       'imap_username', 'imap_password',
                       'imap_folder_inbox', 'imap_folder_sent',
                       'sync_days_back', 'sync_interval_minutes',
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

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('<int:pk>/sync-all/',
                 self.admin_site.admin_view(self._sync_all_view),
                 name='emailaccount_sync_all'),
        ]
        return custom + urls

    def _sync_all_view(self, request, pk):
        """Admin AJAX: full historical sync for one account (all folders)."""
        from django.core.management import call_command
        from io import StringIO
        from email_assistant.imap_client import IMAPClient
        from email_assistant.models import EmailThread

        try:
            account = EmailAccount.objects.get(pk=pk)
        except EmailAccount.DoesNotExist:
            return JsonResponse({'ok': False, 'error': 'Акаунт не знайдено'})

        out = StringIO()
        try:
            call_command('sync_email', account=account.pk, stdout=out, stderr=out, **{'all': True})
        except Exception as e:
            return JsonResponse({'ok': False, 'error': str(e)})

        folder_results = []
        try:
            with IMAPClient(account) as imap:
                imap_folders = imap.list_folders()

            standard_server = {
                account.imap_folder_inbox.lower(),
                account.imap_folder_sent.lower(),
            }
            for f in imap_folders:
                if not f.get('selectable'):
                    continue
                name = f['name']
                if name.lower() in standard_server:
                    continue
                created = 0
                try:
                    with IMAPClient(account) as imap:
                        messages = imap.fetch_messages(folder=name, days_back=3650, since_uid=0)
                    for msg_data in messages:
                        if EmailMessage.objects.filter(
                                account=account, imap_uid=msg_data['uid'],
                                imap_folder_name=name).exists():
                            continue
                        thread_key = (msg_data.get('in_reply_to') or
                                      msg_data.get('message_id') or msg_data['subject'])
                        thread = EmailThread.objects.filter(
                            account=account, thread_id=thread_key[:500]).first()
                        if not thread:
                            thread = EmailThread.objects.create(
                                account=account,
                                thread_id=thread_key[:500] if thread_key else '',
                                subject=msg_data['subject'][:500],
                                participants=[msg_data['from_email']] + msg_data['to_emails'],
                            )
                        EmailMessage.objects.create(
                            account=account, thread=thread,
                            imap_uid=msg_data['uid'], imap_folder_name=name,
                            message_id=msg_data['message_id'],
                            in_reply_to=msg_data['in_reply_to'],
                            folder=EmailMessage.FOLDER_INBOX,
                            subject=msg_data['subject'], from_email=msg_data['from_email'],
                            from_name=msg_data['from_name'], to_emails=msg_data['to_emails'],
                            cc_emails=msg_data['cc_emails'], body_text=msg_data['body_text'],
                            body_html=msg_data['body_html'], attachments=msg_data['attachments'],
                            is_read=msg_data['is_read'], sent_at=msg_data['sent_at'],
                        )
                        created += 1
                    folder_results.append({'folder': name, 'created': created})
                except Exception as e:
                    folder_results.append({'folder': name, 'error': str(e)})
        except Exception as e:
            pass

        return JsonResponse({'ok': True, 'output': out.getvalue(), 'folders': folder_results})

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
            'fields': ('ai_auto_suggest_reply', 'ai_auto_translate', 'ai_translate_to'),
        }),
        ("✍️ Підпис (застарілий — використовуй підпис акаунту)", {
            'fields': ('auto_signature', 'signature_position', 'signature'),
            'classes': ('collapse',),
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
