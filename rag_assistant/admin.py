from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
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
        ("🖥 RAG-ПК зараз", {
            "fields": ["pc_panel"],
            "description": "Чим зараз відповідає RAG (локальна модель — без оплати, хмара — платить ключ RAG), що він "
                           "робить, хто звертається; зупинка зайвого запиту й звільнення відеопам'яті.",
        }),
    ]
    readonly_fields = ["key_status", "check_status", "pc_panel"]

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
        from core.secrets import mask
        return f"задано ({mask(obj.api_key)})"

    @admin.display(description="Перевірка зв'язку")
    def check_status(self, obj):
        url = reverse("admin:rag_assistant_ragsettings_check")
        last = (f"{timezone.localtime(obj.last_check_at):%d.%m.%Y %H:%M} — {obj.last_check_result}"
                if obj and obj.last_check_at else "ще не перевірялось")
        return format_html('<button type="submit" class="button" formaction="{}" formnovalidate>'
                           '🔌 Перевірити зв\'язок</button> <span style="margin-left:8px">{}</span>'
                           '<div class="help">Перевіряє збережені налаштування (спершу збережіть зміни).</div>',
                           url, last)

    @admin.display(description="Стан")
    def pc_panel(self, obj):
        request = getattr(self, "_request", None)
        return render_to_string("admin/rag_assistant/rag_pc.html", {
            "status_url": reverse("admin:rag_assistant_ragsettings_pc"),
            "cancel_url": reverse("admin:rag_assistant_ragsettings_pc_cancel", args=["RUN"]),
            "unload_url": reverse("admin:rag_assistant_ragsettings_pc_unload"),
            "can_change": bool(request and self.has_change_permission(request, obj)),
        })

    def change_view(self, request, object_id, form_url="", extra_context=None):
        self._request = request   # для pc_panel: право керувати RAG-ПК
        return super().change_view(request, object_id, form_url, extra_context)

    def get_urls(self):
        return [path("check/", self.admin_site.admin_view(self.check_view), name="rag_assistant_ragsettings_check"),
                path("pc/", self.admin_site.admin_view(self.pc_view), name="rag_assistant_ragsettings_pc"),
                path("pc/cancel/<str:run>/", self.admin_site.admin_view(self.pc_cancel_view),
                     name="rag_assistant_ragsettings_pc_cancel"),
                path("pc/unload/", self.admin_site.admin_view(self.pc_unload_view),
                     name="rag_assistant_ragsettings_pc_unload")] \
            + super().get_urls()

    def pc_view(self, request):
        from . import pc
        if not self.has_view_permission(request):
            raise PermissionDenied
        return JsonResponse(pc.status())

    def pc_cancel_view(self, request, run):
        from . import pc
        if request.method != "POST" or not self.has_change_permission(request):
            raise PermissionDenied
        return JsonResponse(pc.cancel(run))

    def pc_unload_view(self, request):
        from . import pc
        if request.method != "POST" or not self.has_change_permission(request):
            raise PermissionDenied
        return JsonResponse(pc.unload())

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

import re  # noqa: E402

# Адреса RAG-ПК: IPv4 або ім'я хоста (без схеми, порту й шляху)
_HOST_RE = re.compile(r"^(?=.{1,150}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
FEED_SCOPES = ["knowledge:read", "knowledge:write"]


class KnowledgeSettingsForm(forms.ModelForm):
    categories = forms.MultipleChoiceField(
        label="Категорії листів", required=False, choices=_EmailMessage.CATEGORY_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        help_text="Розсилки й сервісні сповіщення за замовчуванням не індексуються.")
    attachment_types_text = forms.CharField(
        label="Типи вкладень", required=False, help_text="Через кому: pdf, docx, txt, csv")
    rag_host = forms.CharField(
        label="Адреса RAG-ПК", required=False, max_length=150,
        widget=forms.TextInput(attrs={"size": 40, "placeholder": "192.168.2.68"}),
        help_text="IP або ім'я, за яким сервер Minerva (NAS) бачить приватний API RAG: напряму, через тунель "
                  "чи проксі на NAS. Без http:// і без порту. Порожньо — пошук RAG вимкнено.")
    rag_port = forms.IntegerField(label="Порт", min_value=1, max_value=65535, initial=8788, required=False)
    rag_https = forms.BooleanField(label="HTTPS", required=False,
                                   help_text="Увімкніть, якщо перед RAG стоїть HTTPS-проксі.")
    private_api_key = forms.CharField(
        label="API-ключ RAG (Minerva → RAG)", required=False, max_length=200,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password", "size": 50}),
        help_text="Bearer-ключ приватного API RAG (файл private-api-key.txt на RAG-ПК), не ключ магазину. "
                  "Порожньо — залишити збережений.")
    clear_key = forms.BooleanField(label="Видалити збережений ключ", required=False)

    class Meta:
        model = KnowledgeSettings
        fields = ["enabled", "accounts", "categories", "index_from", "include_attachments", "attachment_max_mb",
                  "instance_id", "private_api_key", "private_verify_tls", "auto_offers", "auto_order_files",
                  "auto_datasheets"]
        widgets = {"accounts": forms.CheckboxSelectMultiple,
                   "instance_id": forms.TextInput(attrs={"size": 30, "placeholder": "sevskiy-prod"})}

    def __init__(self, *args, **kwargs):
        from urllib.parse import urlsplit
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["attachment_types_text"].initial = ", ".join(self.instance.attachment_types or [])
            url = urlsplit(self.instance.private_base_url or "")
            if url.hostname:
                self.fields["rag_host"].initial = url.hostname
                self.fields["rag_port"].initial = url.port or (443 if url.scheme == "https" else 80)
                self.fields["rag_https"].initial = url.scheme == "https"

    def clean_instance_id(self):
        value = (self.cleaned_data.get("instance_id") or "").strip()
        if value and not knowledge._INSTANCE_RE.match(value):
            raise forms.ValidationError("Лише малі латинські літери, цифри, «.», «_», «-»; 2–64 символи, напр. sevskiy-prod.")
        return value

    def clean_rag_host(self):
        value = (self.cleaned_data.get("rag_host") or "").strip()
        if value and not _HOST_RE.match(value):
            raise forms.ValidationError("Лише адреса або ім'я хоста, напр. 192.168.2.68 — без http://, порту й шляху.")
        return value

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
        host = self.cleaned_data.get("rag_host")
        port = self.cleaned_data.get("rag_port") or 8788
        scheme = "https" if self.cleaned_data.get("rag_https") else "http"
        obj.private_base_url = f"{scheme}://{host}:{port}" if host else ""
        if commit:
            obj.save()
            self.save_m2m()
        return obj


@admin.register(KnowledgeSettings)
class KnowledgeSettingsAdmin(admin.ModelAdmin):
    form = KnowledgeSettingsForm
    readonly_fields = ["live_panel", "status_panel", "connection_help", "key_status", "feed_keys", "search_status",
                       "documents_panel"]
    fieldsets = [
        ("Стан", {"fields": ["live_panel", "status_panel"]}),
        ("Що індексувати", {
            "fields": ["enabled", "accounts", "categories", "index_from", "include_attachments",
                       "attachment_types_text", "attachment_max_mb"],
            "description": "Minerva вирішує, які листи можна передати в RAG; RAG сам забирає зміни (pull) і звітує. "
                           "Вектори створюються локально на ПК RAG. Після збереження стрічка звіряється у фоні.",
        }),
        ("📎 Документи й посилання", {
            "fields": ["documents_panel", "auto_offers", "auto_order_files", "auto_datasheets"],
            "description": "Крім листів — файли й посилання, які додають у картках клієнта, проєкту, замовлення й товару "
                           "(«📎 Документи й посилання»), а за галочками нижче — і документи, які Minerva додає сама. "
                           "Передаються, лише коли ввімкнено «Індексувати пошту»; RAG отримує їх, коли сам уміє "
                           "(оновлений RAG-ПК). Галочки — масова індексація: вмикайте по одній і дивіться на стан.",
        }),
        ("Підключення RAG", {"fields": ["connection_help", "instance_id", "rag_host", "rag_port", "rag_https",
                                        "private_verify_tls", "search_status"]}),
        ("API-ключі", {"fields": ["private_api_key", "key_status", "clear_key", "feed_keys"],
                       "description": "Два різні ключі: Minerva → RAG (пошук) і RAG → Minerva (стрічка листів)."}),
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

    @admin.display(description="Збережений ключ RAG")
    def key_status(self, obj):
        if not obj or not obj.private_api_key:
            return format_html('<span style="color:#e57373">{}</span>', "не задано (потрібен для пошуку, не для індексації)")
        from core.secrets import mask
        return f"задано ({mask(obj.private_api_key)})"

    @admin.display(description="Ключ Minerva для RAG (RAG → Minerva)")
    def feed_keys(self, obj):
        """API-ключі Minerva з правами бази знань: RAG ними забирає стрічку /api/v1/knowledge/."""
        from api.models import APIKey
        from core.secrets import mask
        keys = [k for k in APIKey.objects.filter(is_active=True).order_by("-created_at")
                if "knowledge:read" in (k.scopes or [])]
        rows = format_html_join("", "<li><a href='{}'>{}</a> · {} · {}</li>", (
            (reverse("admin:api_apikey_change", args=[k.pk]), k.name, mask(k.key),
             f"востаннє {timezone.localtime(k.last_used):%d.%m.%Y %H:%M}" if k.last_used else "ще не використовувався")
            for k in keys))
        button = format_html(
            '<button type="submit" class="button" formaction="{}" formnovalidate>🔑 Створити ключ для RAG</button>',
            reverse("admin:rag_assistant_knowledgesettings_feedkey"))
        return format_html(
            "{}<div style='margin-top:6px'>{}</div><div class='help'>Ключ із правами <code>knowledge:read</code> і "
            "<code>knowledge:write</code>, без магазину. Повний ключ — на його сторінці (відкрийте посилання); "
            "його вписують у конфігурацію RAG разом з адресою Minerva, яку бачить RAG-ПК.</div>",
            format_html("<ul style='margin:0;padding-left:18px'>{}</ul>", rows) if keys
            else format_html('<span style="color:#e57373">{}</span>', "ще немає"), button)

    @admin.display(description="Що зараз робить RAG")
    def live_panel(self, obj):
        """Етап, прогрес і остання дія RAG; оновлюється сама (live/ кожні 5 с, capabilities RAG — раз на ~хвилину)."""
        progress = knowledge.progress()
        return render_to_string("admin/rag_assistant/knowledge_live.html", {
            "progress": progress, "paused": progress["paused"],
            "live_url": reverse("admin:rag_assistant_knowledgesettings_live"),
            "pause_url": reverse("admin:rag_assistant_knowledgesettings_pause"),
            "refresh_url": reverse("admin:rag_assistant_knowledgesettings_refresh"),
            "clear_url": reverse("admin:rag_assistant_knowledgesettings_clear"),
            "generation_url": reverse("admin:rag_assistant_knowledgesettings_generation")})

    @admin.display(description="Стан індексації")
    def status_panel(self, obj):
        inst = knowledge.instance_id()
        source = {"env": "змінна середовища", "settings": "поле нижче"}.get(knowledge.instance_id_source(), "")
        s = knowledge.summary()
        d = s["documents"]
        rows = [
            ("Назва сервера для RAG", f"{inst} ({source})" if inst else format_html(
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
        errors = format_html_join("", '<li><a href="{}" target="_blank">{}</a>: {}</li>',
                                  ((f"/rag/knowledge/{f.obj_id}/" if f.doc_type == "document" else f"/email/message/{f.obj_id}/",
                                    f.document_id, f.error) for f in failed if f.status_revision == f.revision))
        return format_html('<table>{}</table>{}', table,
                           format_html('<div style="margin-top:8px"><b>Останні помилки RAG:</b><ul>{}</ul></div>', errors)
                           if errors else "")

    @admin.display(description="Стан документів")
    def documents_panel(self, obj):
        """Файли й посилання (тип document): скільки в Minerva, чи RAG їх уже приймає, стан індексації."""
        from . import retrieval
        from .models import KnowledgeItem
        d = knowledge.summary(types=("document",))["documents"]
        total = KnowledgeItem.objects.count()
        auto = KnowledgeItem.objects.filter(auto=True).count()
        if retrieval.documents_supported():
            rag = format_html("✅ RAG приймає документи · у індексі <b>{}</b>, чекають <b>{}</b>, помилки / пропущено {} / {}",
                              d["indexed"], d["pending"], d["failed"], d["skipped"])
        else:
            rag = format_html('<b style="color:#e57373">RAG ще не приймає документи</b> — потрібне оновлення RAG-ПК '
                              '(private_rag з підтримкою document). До того документи працюють лише в Minerva: '
                              '«❓ Запитати» й огляди карток беруть їх напряму.')
        return format_html('Документів і посилань: <b>{}</b> (автоматичних {}) · <a href="{}">📎 список</a><br>'
                           'Передано в стрічку: <b>{}</b><br>{}', total, auto,
                           reverse("admin:rag_assistant_knowledgeitem_changelist"), d["eligible"], rag)

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
            "<li><b>Назва сервера</b> — напр. <code>sevskiy-prod</code> (або змінна середовища <code>MINERVA_INSTANCE_ID</code>).</li>"
            "<li><b>Мережа:</b> сервер Minerva (NAS) має бачити приватний API RAG за адресою й портом нижче "
            "(напряму, через тунель або HTTPS-проксі на NAS), а RAG-ПК — Minerva (стрічку <code>/api/v1/knowledge/</code>).</li>"
            "<li><b>Ключі:</b> «API-ключ RAG» — з RAG-ПК; «🔑 Створити ключ для RAG» — ключ Minerva, який вписують у RAG.</li>"
            "<li><b>«🔌 Перевірити пошук RAG»</b> — Minerva звернеться до RAG і покаже відповідь чи причину відмови.</li>"
            "<li>Увімкніть «Індексувати пошту» для однієї скриньки й короткого періоду; масову індексацію архіву — лише після перевірки.</li>"
            "</ol><div class='help'>Контракт: api/schemas/knowledge/README.md · API-ключі: <a href='{}'>REST API → API Ключі</a></div>",
            reverse("admin:api_apikey_changelist"))

    def get_urls(self):
        return [
            path("refresh/", self.admin_site.admin_view(self.refresh_view), name="rag_assistant_knowledgesettings_refresh"),
            path("clear/", self.admin_site.admin_view(self.clear_view), name="rag_assistant_knowledgesettings_clear"),
            path("generation/", self.admin_site.admin_view(self.generation_view),
                 name="rag_assistant_knowledgesettings_generation"),
            path("check/", self.admin_site.admin_view(self.check_view), name="rag_assistant_knowledgesettings_check"),
            path("feedkey/", self.admin_site.admin_view(self.feedkey_view), name="rag_assistant_knowledgesettings_feedkey"),
            path("live/", self.admin_site.admin_view(self.live_view), name="rag_assistant_knowledgesettings_live"),
            path("pause/", self.admin_site.admin_view(self.pause_view), name="rag_assistant_knowledgesettings_pause"),
        ] + super().get_urls()

    def live_view(self, request):
        """JSON для живої панелі. ?probe=1 — також оновити capabilities RAG (кеш 60 с і запобіжник діють)."""
        from . import retrieval
        if not self.has_view_permission(request):
            raise PermissionDenied
        if request.GET.get("probe") and retrieval.available():
            retrieval.capabilities(request.user)
        return JsonResponse(knowledge.progress())

    def pause_view(self, request):
        if request.method == "POST" and self.has_change_permission(request):
            paused = request.GET.get("action") == "pause"
            knowledge.set_feed_paused(paused)
            if paused:
                self.message_user(request, "⏸ Індексацію призупинено: RAG зупиниться після поточного листа. "
                                           "Пошук працює з уже проіндексованим.", messages.WARNING)
            else:
                self.message_user(request, "▶ Індексацію продовжено: RAG підхопить протягом хвилини. Уже оброблені "
                                           "листи він лише звірить, тож швидко дійде до місця зупинки.")
        return self._back()

    def feedkey_view(self, request):
        """Новий API-ключ Minerva для RAG (лише права бази знань, без магазину) → його сторінка з повним ключем."""
        from api.models import APIKey
        if request.method != "POST" or not request.user.has_perm("api.add_apikey"):
            return self._back()
        key = APIKey.objects.create(name=f"RAG — база знань ({timezone.localdate():%d.%m.%Y})", scopes=FEED_SCOPES)
        self.message_user(request, "Створено ключ для RAG. Скопіюйте його з розділу «Технічна інформація» у "
                                   "конфігурацію RAG; старий ключ, якщо він більше не потрібен, вимкніть.")
        return redirect(reverse("admin:api_apikey_change", args=[key.pk]))

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


DOC_TYPES_FOR_ADMIN = knowledge.DOC_TYPES


@admin.register(KnowledgeDoc)
class KnowledgeDocAdmin(admin.ModelAdmin):
    list_display = ["document_id_col", "revision", "fed", "state_col", "chunks", "error", "last_status_at"]
    list_filter = ["doc_type", "fed", "state"]
    actions = ["resend_action"]

    @admin.action(description="🔄 Передати в RAG ще раз (нова ревізія — RAG обробить заново)")
    def resend_action(self, request, queryset):
        n = 0
        for doc_type in DOC_TYPES_FOR_ADMIN:
            n += knowledge.resend(doc_type, queryset.filter(doc_type=doc_type, fed=True).values_list("obj_id", flat=True))
        self.message_user(request, f"Передано ще раз: {n}. Решта не в базі знань (видалені / поза правилами).")
    search_fields = ["obj_id", "error"]
    readonly_fields = [f.name for f in KnowledgeDoc._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Документ")
    def document_id_col(self, obj):
        url = f"/rag/knowledge/{obj.obj_id}/" if obj.doc_type == "document" else f"/email/message/{obj.obj_id}/"
        return format_html('<a href="{}" target="_blank">{}</a>', url, obj.document_id)

    @admin.display(description="Стан чинної ревізії")
    def state_col(self, obj):
        return dict(KnowledgeDoc.STATES).get(obj.current_state, obj.current_state)


from django.db.models import Q  # noqa: E402

from . import knowledge_items  # noqa: E402
from .models import KnowledgeItem  # noqa: E402


@admin.register(KnowledgeItem)
class KnowledgeItemAdmin(admin.ModelAdmin):
    """Файли й посилання бази знань (тип document). Додавати зручніше в картці клієнта / проєкту / замовлення /
    товару («📎 Документи й посилання»); тут — увесь список, пошук, зміна прив'язок і видимості."""
    list_display = ["title", "kind", "links_col", "visibility", "owner", "rag_col", "created_at"]
    list_filter = ["kind", "visibility", "auto"]
    search_fields = ["title", "note", "url", "file_name", "source_key"]
    raw_id_fields = ["customer", "project", "order", "product", "offer"]
    readonly_fields = ["file_name", "file_size", "content_type", "fetched_at", "fetch_error", "owner", "auto",
                       "source_key", "rag_col", "created_at", "updated_at"]
    fieldsets = [
        (None, {"fields": ["kind", "title", "note", "file", "url", "visibility"]}),
        ("Прив'язки", {"fields": ["customer", "project", "order", "product", "offer"]}),
        ("Стан", {"fields": ["rag_col", "file_name", "file_size", "content_type", "fetched_at", "fetch_error",
                             "owner", "auto", "source_key", "created_at", "updated_at"]}),
    ]

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("owner", "customer", "project", "order", "product")
        if request.user.is_superuser:
            return qs
        return qs.filter(Q(visibility=KnowledgeItem.TEAM) | Q(owner=request.user))

    def has_change_permission(self, request, obj=None):
        if obj is not None and not knowledge_items.can_change(request.user, obj) and not request.user.is_superuser:
            return False
        return super().has_change_permission(request, obj)

    has_delete_permission = has_change_permission

    def save_model(self, request, obj, form, change):
        if not change:
            obj.owner = request.user
        if "file" in form.changed_data and obj.file:
            data = obj.file.read()
            obj.file.seek(0)
            obj.file_name = obj.file.name.rsplit("/", 1)[-1][:300]
            obj.file_size, obj.file_sha256 = len(data), knowledge_items._sha(data)
            obj.content_type = (getattr(obj.file.file, "content_type", "") or "")[:200]
        refetch = bool(obj.url) and "url" in form.changed_data
        if refetch:
            obj.fetched_at, obj.fetch_error = None, ""
        super().save_model(request, obj, form, change)
        if refetch:
            knowledge_items.start_fetch(obj.pk)

    def delete_model(self, request, obj):
        knowledge_items.delete(obj)

    @admin.display(description="Прив'язки")
    def links_col(self, obj):
        parts = [f"👤 {obj.customer}" if obj.customer_id else "", f"📁 {obj.project.code}" if obj.project_id else "",
                 f"🛒 {obj.order.order_number}" if obj.order_id else "", f"📦 {obj.product.sku}" if obj.product_id else ""]
        return " · ".join(p for p in parts if p) or "—"

    @admin.display(description="У RAG")
    def rag_col(self, obj):
        if not obj or not obj.pk:
            return "—"
        from .views_entity import item_json
        return item_json(obj, None)["rag"]
