from django.contrib import admin
from django.utils.html import format_html
from .models import AIConversation, AIMessage, AIBudgetLog


class AIMessageInline(admin.TabularInline):
    model = AIMessage
    extra = 0
    readonly_fields = ('role', 'content_short', 'tool_name', 'model_used', 'cost_usd', 'created_at')
    fields = readonly_fields
    can_delete = False
    max_num = 0

    @admin.display(description='Зміст')
    def content_short(self, obj):
        return obj.content[:120] + '…' if len(obj.content) > 120 else obj.content


@admin.register(AIConversation)
class AIConversationAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'channel', 'total_cost_usd', 'messages_count', 'last_message_at', 'is_active')
    list_filter = ('channel', 'is_active')
    search_fields = ('user_profile__user__username', 'telegram_chat_id')
    readonly_fields = ('started_at', 'last_message_at', 'total_input_tokens',
                       'total_output_tokens', 'total_cost_usd')
    inlines = [AIMessageInline]

    @admin.display(description='Повідомлень')
    def messages_count(self, obj):
        return obj.messages.count()

    def has_add_permission(self, request):
        return False


@admin.register(AIBudgetLog)
class AIBudgetLogAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'total_requests', 'total_input_tokens',
                    'total_output_tokens', 'total_cost_usd', 'alert_sent')
    readonly_fields = ('year', 'month', 'total_requests', 'total_input_tokens',
                       'total_output_tokens', 'total_cost_usd', 'alert_sent')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
