from django.contrib import admin, messages
from django.utils.html import format_html, format_html_join

from .models import CalendarEvent, CalendarSettings, InviteAttendee, MeetingInvite, OutgoingInvite


def _invite_url(event) -> str:
    """Календар місяця події з відкритим вікном «👥 Учасники й запрошення»."""
    from django.utils import timezone
    d = timezone.localtime(event.start_at)
    return f'/calendar/?year={d.year}&month={d.month}&invite={event.pk}'


@admin.register(CalendarEvent)
class CalendarEventAdmin(admin.ModelAdmin):
    list_display  = ('title', 'event_type', 'start_at', 'all_day', 'crm_customer', 'is_done')
    list_filter   = ('event_type', 'all_day', 'is_done')
    search_fields = ('title', 'description')
    readonly_fields = ('created_at', 'invite_panel')
    date_hierarchy  = 'start_at'

    fieldsets = (
        (None, {
            'fields': ('user', 'title', 'description', 'event_type'),
        }),
        ('📅 Час', {
            'fields': ('start_at', 'end_at', 'all_day'),
        }),
        ('👥 Учасники й запрошення', {
            'fields': ('invite_panel',),
        }),
        ('🔗 Зв\'язки', {
            'fields': ('crm_customer', 'email_message', 'sales_order'),
            'classes': ('collapse',),
        }),
        ('🔔 Нагадування', {
            'fields': ('remind_minutes_before', 'remind_sent', 'push_sent'),
        }),
        ('Статус', {
            'fields': ('is_done', 'created_at'),
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(user=request.user)

    @admin.display(description='Учасники')
    def invite_panel(self, obj):
        """Хто запрошений і що відповів (calendar_app/outgoing_invites.py); надсилає — календар."""
        from . import outgoing_invites as oi
        if not obj or not obj.pk:
            return 'Збережіть подію — потім у календарі «👥 Запросити учасників…».'
        inv = oi.for_event(obj)
        link = format_html('<a href="{}">👥 Учасники й запрошення в календарі →</a>', _invite_url(obj))
        if inv is None:
            org = oi.foreign_organizer(obj)
            if org:
                return format_html('Зустріч із запрошення — організатор <span translate="no">{}</span>.', org)
            return format_html('Ще нікого не запрошено. {}', link)
        rows = format_html_join('', '<div>{} <span translate="no">{}</span>{}</div>', (
            (oi.ICON.get(a.partstat, '⏳') if not a.removed else '—', a.name or a.email,
             f' · {a.comment}' if a.comment else '') for a in inv.attendees.all()))
        changed = oi.changed_keys(inv)
        warn = format_html('<div style="color:#d97706">⚠ Змінено після надсилання — учасники ще бачать старе. '
                           '{}</div>', link) if changed else ''
        return format_html('<div>{}</div>{}{}<div style="margin-top:4px">{}</div>',
                           inv.get_status_display(), rows, warn, link)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from . import outgoing_invites as oi
        inv = oi.for_event(obj) if change else None
        if inv is not None and oi.changed_keys(inv):
            messages.warning(request, format_html(
                'Зустріч змінено, а учасники ще бачать старе — <a href="{}">♻ надіслати оновлення</a>.',
                _invite_url(obj)))

    def delete_view(self, request, object_id, extra_context=None):
        from . import outgoing_invites as oi
        if request.method == 'GET':
            event = self.get_object(request, object_id)
            to = oi.cancel_targets(event) if event else []
            if to:
                messages.warning(request, 'Учасникам піде лист «Скасовано»: ' + ', '.join(to))
        return super().delete_view(request, object_id, extra_context)


@admin.register(CalendarSettings)
class CalendarSettingsAdmin(admin.ModelAdmin):
    list_display = ('user', 'notify_telegram', 'notify_email', 'notify_push',
                    'default_remind_minutes')
    readonly_fields = ('user',)

    fieldsets = (
        ('👤 Користувач', {
            'fields': ('user',),
        }),
        ('🔔 Канали сповіщень', {
            'fields': ('notify_telegram', 'notify_email', 'notify_push'),
        }),
        ('⏱ Час нагадування', {
            'fields': ('default_remind_minutes',),
        }),
        ('📬 Адреси (необов\'язково)', {
            'fields': ('email_to', 'telegram_chat_id'),
            'classes': ('collapse',),
            'description': 'Залиш порожнім — використаються системні значення з Налаштування → Сповіщення',
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(user=request.user)

    def has_add_permission(self, request):
        return False  # created automatically via for_user()


@admin.register(MeetingInvite)
class MeetingInviteAdmin(admin.ModelAdmin):
    """Запрошення з пошти — рішення ✓ / ✕ приймаються в календарі (/calendar/)."""
    list_display = ('title', 'start_at', 'organizer', 'status', 'user', 'event')
    list_filter = ('status', 'user')
    search_fields = ('title', 'organizer', 'location', 'uid')
    readonly_fields = [f.name for f in MeetingInvite._meta.fields]

    def has_add_permission(self, request):
        return False


class InviteAttendeeInline(admin.TabularInline):
    model = InviteAttendee
    extra = 0
    can_delete = False
    fields = ('email', 'name', 'partstat', 'invited_at', 'responded_at', 'comment', 'removed', 'reply_message')
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(OutgoingInvite)
class OutgoingInviteAdmin(admin.ModelAdmin):
    """Запрошення, які ми надіслали з календаря, — лише перегляд; надсилання й зміни — у календарі."""
    list_display = ('__str__', 'status', 'answers', 'account', 'last_sent_at', 'event_link')
    list_filter = ('status',)
    search_fields = ('uid', 'attendees__email', 'event__title')
    readonly_fields = [f.name for f in OutgoingInvite._meta.fields]
    inlines = [InviteAttendeeInline]

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related('event', 'account').prefetch_related('attendees')
        return qs.filter(user=request.user)     # чужі запрошення (адреси учасників) — не показуємо

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description='Учасники')
    def answers(self, obj):
        from . import outgoing_invites as oi
        return oi.badge(obj) or '—'

    @admin.display(description='Подія')
    def event_link(self, obj):
        if not obj.event_id:
            return 'видалено'
        return format_html('<a href="{}">👥 у календарі →</a>', _invite_url(obj.event))
