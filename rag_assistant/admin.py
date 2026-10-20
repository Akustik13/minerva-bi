from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

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


# ── 🧠 База знань (пошта → RAG) ───────────────────────────────────────────────

from email_assistant.models import EmailMessage as _EmailMessage  # noqa: E402

from . import knowledge  # noqa: E402
from .models import KnowledgeDoc, KnowledgeSettings  # noqa: E402


class KnowledgeSettingsForm(forms.ModelForm):
    categories = forms.MultipleChoiceField(
        label="Категорії листів", required=False, choices=_EmailMessage.CATEGORY_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        help_text="Розсилки й сервісні сповіщення за замовчуванням не індексуються.")
    attachment_types_text = forms.CharField(
        label="Типи вкладень", required=False, help_text="Через кому: pdf, docx, txt, csv")
    private_api_key = forms.CharField(
        label="Ключ приватного пошуку RAG", required=False, max_length=200,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password", "size": 50}),
        help_text="Окремий Bearer-ключ RAG для /v2/* (не ключ магазину). Порожньо — залишити збережений.")
    clear_key = forms.BooleanField(label="Видалити збережений ключ", required=False)

    class Meta:
        model = KnowledgeSettings
        fields = ["enabled", "accounts", "categories", "index_from", "include_attachments", "attachment_max_mb",
                  "private_base_url", "private_api_key"]
        widgets = {"accounts": forms.CheckboxSelectMultiple}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["attachment_types_text"].initial = ", ".join(self.instance.attachment_types or [])

    def clean_private_api_key(self):
        value = (self.cleaned_data.get("private_api_key") or "").strip()
        if not value and self.instance and self.instance.pk and not self.data.get("clear_key"):
            return self.instance.private_api_key
        return value

    def clean(self):
        d = super().clean()
        if d.get("clear_key"):
            d["private_api_key"] = ""
        return d

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.attachment_types = [t.strip().lower().lstrip(".") for t in
                                (self.cleaned_data.get("attachment_types_text") or "").split(",") if t.strip()]
        if commit:
            obj.save()
            self.save_m2m()
        return obj


@admin.register(KnowledgeSettings)
class KnowledgeSettingsAdmin(admin.ModelAdmin):
    form = KnowledgeSettingsForm
    readonly_fields = ["status_panel", "connection_help", "key_status", "search_status"]
    fieldsets = [
        ("Стан", {"fields": ["status_panel"]}),
        ("Що індексувати", {
            "fields": ["enabled", "accounts", "categories", "index_from", "include_attachments",
                       "attachment_types_text", "attachment_max_mb"],
            "description": "Minerva вирішує, які листи можна передати в RAG; RAG сам забирає зміни (pull) і звітує. "
                           "Вектори створюються локально на ПК RAG. Після збереження стрічка звіряється у фоні.",
        }),
        ("Підключення RAG", {"fields": ["connection_help", "private_base_url", "private_api_key", "key_status",
                                        "clear_key", "search_status"]}),
    ]

    def has_add_permission(self, request):
        return not KnowledgeSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        return redirect(reverse("admin:rag_assistant_knowledgesettings_change", args=[KnowledgeSettings.get().pk]))

    def save_model(self, request, obj, form, change):
        if change:
            obj.settings_version += 1
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)   # скриньки (M2M) збережено — тепер звірка
        knowledge.start_refresh()
        self.message_user(request, "Налаштування збережено; стрічка бази знань звіряється у фоні.")

    @admin.display(description="Збережений ключ")
    def key_status(self, obj):
        if not obj or not obj.private_api_key:
            return format_html('<span style="color:#e57373">{}</span>', "не задано (потрібен для пошуку, не для індексації)")
        return f"задано (…{obj.private_api_key[-4:]})"

    @admin.display(description="Стан індексації")
    def status_panel(self, obj):
        inst = knowledge.instance_id()
        s = knowledge.summary()
        d = s["documents"]
        rows = [
            ("Середовище (MINERVA_INSTANCE_ID)", inst or format_html(
                '<b style="color:#e57373">не задано — маршрути для RAG вимкнені (503)</b>')),
            ("Покоління журналу", s["generation"]),
            ("Остання подія журналу (seq)", s["last_seq"]),
            ("Мають бути в індексі", d["eligible"]),
            ("✅ Проіндексовано (чинна ревізія)", d["indexed"]),
            ("⏳ Очікують RAG", d["pending"]),
            ("⚠️ Помилки / пропущено", f"{d['failed']} / {d['skipped']}"),
            ("🗑 Видалення ще не підтверджено", d["pending_delete"]),
            ("Останній звіт RAG", timezone.localtime(knowledge._parse_ts(s["last_status_at"])).strftime("%d.%m.%Y %H:%M")
             if s["last_status_at"] else "ще не було"),
            ("Остання звірка стрічки", obj.last_refresh_result or "ще не було"),
        ]
        table = format_html_join("", "<tr><td style='padding:2px 12px 2px 0;color:var(--body-quiet-color)'>{}</td>"
                                     "<td style='padding:2px 0'><b>{}</b></td></tr>", rows)
        failed = (KnowledgeDoc.objects.filter(fed=True, state=KnowledgeDoc.FAILED).order_by("-last_status_at")[:10])
        errors = format_html_join("", '<li><a href="/email/message/{}/" target="_blank">{}</a>: {}</li>',
                                  ((f.obj_id, f.document_id, f.error) for f in failed if f.status_revision == f.revision))
        buttons = format_html(
            '<div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">'
            '<button type="submit" class="button" formaction="{}" formnovalidate>🔄 Звірити стрічку зараз</button>'
            '<button type="submit" class="button" formaction="{}" formnovalidate '
            'onclick="return confirm(\'Прибрати всі листи з індексу RAG і поставити на паузу?\')">🧹 Прибрати все з індексу</button>'
            '<button type="submit" class="button" formaction="{}" formnovalidate '
            'onclick="return confirm(\'Нове покоління — лише після відновлення БД з бекапу. RAG зробить повну звірку. Продовжити?\')">'
            '♻ Нове покоління (після відновлення БД)</button></div>',
            reverse("admin:rag_assistant_knowledgesettings_refresh"),
            reverse("admin:rag_assistant_knowledgesettings_clear"),
            reverse("admin:rag_assistant_knowledgesettings_generation"))
        return format_html('<table>{}</table>{}{}', table,
                           format_html('<div style="margin-top:8px"><b>Останні помилки RAG:</b><ul>{}</ul></div>', errors)
                           if errors else "", buttons)

    @admin.display(description="Приватний пошук (/v2)")
    def search_status(self, obj):
        """Стан запобіжника й останні capabilities — без звернення до RAG при відкритті сторінки."""
        from . import retrieval
        reason = retrieval.not_configured_reason(obj)
        until = retrieval.breaker_until(obj)
        lines = []
        if reason:
            lines.append(format_html('<span style="color:#e57373">не налаштовано: {}</span>', reason))
        elif until:
            lines.append(format_html('<span style="color:#e57373">⏸ запобіжник: Minerva не звертається до RAG до {}</span>',
                                     timezone.localtime(until).strftime("%H:%M")))
        else:
            lines.append(f"адреса: {retrieval.base_url(obj)}")
        caps = obj.last_capabilities or {}
        if caps:
            ready, idx = caps.get("readiness") or {}, caps.get("index") or {}
            lines.append(format_html(
                "останнє: пошук <b>{}</b>, індексація <b>{}</b> · документів {} · отримано seq {}, застосовано {} · {}",
                ready.get("retrieval", "?"), ready.get("indexing", "?"), idx.get("docs", "?"),
                idx.get("received_seq", "?"), idx.get("applied_seq", "?"),
                timezone.localtime(obj.last_capabilities_at).strftime("%d.%m %H:%M") if obj.last_capabilities_at else ""))
        button = format_html('<button type="submit" class="button" formaction="{}" formnovalidate>'
                             '🔌 Перевірити пошук RAG</button>', reverse("admin:rag_assistant_knowledgesettings_check"))
        return format_html("{}<div style='margin-top:6px'>{}</div>",
                           format_html_join("", "<div>{}</div>", ((l,) for l in lines)), button)

    @admin.display(description="Як підключити RAG")
    def connection_help(self, obj):
        return format_html(
            "<ol style='margin:0;padding-left:18px'>"
            "<li>На сервері задайте змінну середовища <code>MINERVA_INSTANCE_ID</code> (напр. <code>sevskiy-prod</code>).</li>"
            "<li><a href='{}' target='_blank'>Адмін → REST API → API Ключі</a>: новий ключ <b>без магазину</b> з правами "
            "<code>knowledge:read</code> і <code>knowledge:write</code> — передайте його адміністратору RAG.</li>"
            "<li>RAG сам забирає зміни з <code>/api/v1/knowledge/changes/</code> і звітує статуси — прогрес видно вище.</li>"
            "<li>Почніть з короткого періоду однієї скриньки; масову індексацію архіву — лише після перевірки.</li>"
            "</ol><div class='help'>Контракт: api/schemas/knowledge/README.md</div>",
            reverse("admin:api_apikey_add"))

    def get_urls(self):
        return [
            path("refresh/", self.admin_site.admin_view(self.refresh_view), name="rag_assistant_knowledgesettings_refresh"),
            path("clear/", self.admin_site.admin_view(self.clear_view), name="rag_assistant_knowledgesettings_clear"),
            path("generation/", self.admin_site.admin_view(self.generation_view),
                 name="rag_assistant_knowledgesettings_generation"),
            path("check/", self.admin_site.admin_view(self.check_view), name="rag_assistant_knowledgesettings_check"),
        ] + super().get_urls()

    def _back(self):
        return redirect(reverse("admin:rag_assistant_knowledgesettings_change", args=[KnowledgeSettings.get().pk]))

    def refresh_view(self, request):
        if request.method == "POST":
            knowledge.start_refresh()
            self.message_user(request, "Звірку стрічки запущено у фоні — оновіть сторінку за хвилину.")
        return self._back()

    def clear_view(self, request):
        if request.method == "POST":
            n = knowledge.clear_index()
            self.message_user(request, f"Індексацію поставлено на паузу; на видалення з RAG передано документів: {n}.",
                              messages.WARNING)
        return self._back()

    def check_view(self, request):
        from . import retrieval
        if request.method == "POST":
            res = retrieval.capabilities(request.user, force=True)
            if res["ok"]:
                ready = res["data"].get("readiness") or {}
                self.message_user(request, f"✅ RAG відповів: пошук — {ready.get('retrieval')}, "
                                           f"індексація — {ready.get('indexing')}.")
            else:
                self.message_user(request, f"❌ {res['message']}", messages.ERROR)
        return self._back()

    def generation_view(self, request):
        if request.method == "POST":
            gen = knowledge.new_generation()
            self.message_user(request, f"Нове покоління журналу: {gen}. RAG зробить повну звірку.", messages.WARNING)
        return self._back()


@admin.register(KnowledgeDoc)
class KnowledgeDocAdmin(admin.ModelAdmin):
    list_display = ["document_id_col", "revision", "fed", "state_col", "chunks", "error", "last_status_at"]
    list_filter = ["fed", "state"]
    search_fields = ["obj_id", "error"]
    readonly_fields = [f.name for f in KnowledgeDoc._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Документ")
    def document_id_col(self, obj):
        return format_html('<a href="/email/message/{}/" target="_blank">{}</a>', obj.obj_id, obj.document_id)

    @admin.display(description="Стан чинної ревізії")
    def state_col(self, obj):
        return dict(KnowledgeDoc.STATES).get(obj.current_state, obj.current_state)
