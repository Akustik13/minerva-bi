from django.contrib import admin
from .models import CalendarEvent


@admin.register(CalendarEvent)
class CalendarEventAdmin(admin.ModelAdmin):
    list_display  = ('title', 'event_type', 'start_at', 'all_day', 'crm_customer', 'is_done')
    list_filter   = ('event_type', 'all_day', 'is_done')
    search_fields = ('title', 'description')
    readonly_fields = ('created_at',)
    date_hierarchy  = 'start_at'

    fieldsets = (
        (None, {
            'fields': ('user', 'title', 'description', 'event_type'),
        }),
        ('📅 Час', {
            'fields': ('start_at', 'end_at', 'all_day'),
        }),
        ('🔗 Зв\'язки', {
            'fields': ('crm_customer', 'email_message', 'sales_order'),
            'classes': ('collapse',),
        }),
        ('🔔 Нагадування', {
            'fields': ('remind_minutes_before', 'remind_sent'),
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
