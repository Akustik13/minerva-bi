from django.contrib import admin
from django.utils.html import format_html
from .models import EmailAccount, EmailMessage, EmailThread, EmailDraft, EmailSettings


@admin.register(EmailAccount)
class EmailAccountAdmin(admin.ModelAdmin):
    list_display  = ('email_address', 'user', 'display_name', 'is_primary', 'is_active', 'last_sync_col')
    list_filter   = ('is_active', 'is_primary')
    search_fields = ('email_address', 'user__username', 'display_name')
    readonly_fields = ('last_sync_at', 'last_seen_uid')

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
                       'imap_folder_inbox', 'imap_folder_sent', 'sync_days_back'),
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
