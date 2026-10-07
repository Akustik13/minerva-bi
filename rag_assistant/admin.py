from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .client import RagClient, RagError
from .models import RagConversation, RagMessage, RagSettings


class RagSettingsForm(forms.ModelForm):
    api_key = forms.CharField(
        label="Ключ RAG API", required=False, max_length=200,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password", "size": 50}),
        help_text="Порожньо — залишити збережений ключ. Ключ зберігається лише на сервері Minerva.",
    )
    clear_key = forms.BooleanField(label="Видалити збережений ключ", required=False)

    class Meta:
        model = RagSettings
        fields = "__all__"

    def clean_api_key(self):
        value = (self.cleaned_data.get("api_key") or "").strip()
        if not value and self.instance and self.instance.pk and not self.data.get("clear_key"):
            return self.instance.api_key  # не змінювати
        return value

    def clean(self):
        d = super().clean()
        if d.get("clear_key"):
            d["api_key"] = ""
        return d


@admin.register(RagSettings)
class RagSettingsAdmin(admin.ModelAdmin):
    form = RagSettingsForm
    fieldsets = [
        (None, {"fields": ["enabled", "name"]}),
        ("Підключення до RAG API", {
            "fields": ["base_url", "api_key", "key_status", "clear_key", "verify_tls", "timeout", "check_status"],
            "description": "Minerva звертається до RAG API зі свого сервера (NAS); браузер ключа не бачить. "
                           "Адреса має бути доступна з NAS: напр. https://app.sevskiy.com або внутрішня "
                           "http://192.168.2.68:8787 (RAG API має слухати мережу, а не лише 127.0.0.1).",
        }),
        ("Відповіді", {"fields": ["language", "latest_only"]}),
    ]
    readonly_fields = ["key_status", "check_status"]

    def has_add_permission(self, request):
        return not RagSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        return redirect(reverse("admin:rag_assistant_ragsettings_change", args=[RagSettings.get().pk]))

    @admin.display(description="Збережений ключ")
    def key_status(self, obj):
        if not obj or not obj.api_key:
            return format_html('<span style="color:#e57373">{}</span>', "не задано")
        return f"задано (…{obj.api_key[-4:]})"

    @admin.display(description="Перевірка зв'язку")
    def check_status(self, obj):
        url = reverse("admin:rag_assistant_ragsettings_check")
        last = (f"{timezone.localtime(obj.last_check_at):%d.%m.%Y %H:%M} — {obj.last_check_result}"
                if obj and obj.last_check_at else "ще не перевірялось")
        return format_html('<button type="submit" class="button" formaction="{}" formnovalidate>'
                           '🔌 Перевірити зв\'язок</button> <span style="margin-left:8px">{}</span>'
                           '<div class="help">Перевіряє збережені налаштування (спершу збережіть зміни).</div>',
                           url, last)

    def get_urls(self):
        return [path("check/", self.admin_site.admin_view(self.check_view), name="rag_assistant_ragsettings_check")] \
            + super().get_urls()

    def check_view(self, request):
        st = RagSettings.get()
        back = redirect(reverse("admin:rag_assistant_ragsettings_change", args=[st.pk]))
        if request.method != "POST":
            return back
        try:
            client = RagClient(st, client_id=f"minerva:admin-u{request.user.pk}")
            health = client.health()
            docs = len(client.documents())
            result = (f"✅ OK · API v{health.get('api_version', '?')} · документів: {docs}"
                      + (" · гібридний режим" if health.get("hybrid_enabled") else ""))
            level = messages.SUCCESS
        except RagError as e:
            result, level = f"❌ {e.message}", messages.ERROR
        st.last_check_at, st.last_check_result = timezone.now(), result[:255]
        st.save(update_fields=["last_check_at", "last_check_result"])
        self.message_user(request, result, level)
        return back


class RagMessageInline(admin.TabularInline):
    model = RagMessage
    extra = 0
    can_delete = False
    fields = ["created_at", "role", "status", "content_short", "error"]
    readonly_fields = fields

    @admin.display(description="Текст")
    def content_short(self, obj):
        return (obj.content or "")[:300]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(RagConversation)
class RagConversationAdmin(admin.ModelAdmin):
    list_display = ["title", "user", "messages_count", "updated_at"]
    list_filter = ["user"]
    search_fields = ["title", "messages__content"]
    readonly_fields = ["user", "title", "created_at", "updated_at"]
    inlines = [RagMessageInline]

    def has_add_permission(self, request):
        return False

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(user=request.user)  # чужі розмови — лише суперадміну

    @admin.display(description="Повідомлень")
    def messages_count(self, obj):
        return obj.messages.count()
