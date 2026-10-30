from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from . import access
from .client import RagClient, RagError
from .models import RagConversation, RagMessage, RagSettings


class RightsMixin:
    """Сторінки бази знань і помічника: перегляд / зміна — за правами rag_assistant/access.py (поверх модуля).
    view_rights — будь-яке з них відкриває перегляд, change_rights — зміну, додавання й видалення."""
    view_rights = ("manage",)
    change_rights = ("manage",)

    def _right(self, request, rights) -> bool:
        return any(access.has(request.user, r) for r in rights)

    def has_module_permission(self, request):
        return self._right(request, self.view_rights)

    def has_view_permission(self, request, obj=None):
        return self._right(request, self.view_rights)

    def has_change_permission(self, request, obj=None):
        return self._right(request, self.change_rights)

    def has_add_permission(self, request):
        return self._right(request, self.change_rights)

    def has_delete_permission(self, request, obj=None):
        return self._right(request, self.change_rights)


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
class RagSettingsAdmin(RightsMixin, admin.ModelAdmin):
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
        return super().has_add_permission(request) and not RagSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
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


class GuideGapFilter(admin.SimpleListFilter):
    """Розмови, де 📚 гід не знайшов відповіді («У гіді цього немає…» або збій) — чого бракує в гіді."""
    title = "📚 Гід"
    parameter_name = "guide_gap"

    def lookups(self, request, model_admin):
        return [("gap", "🕳 Гід не знайшов відповіді")]

    def queryset(self, request, queryset):
        if self.value() != "gap":
            return queryset
        from django.db.models import Exists, OuterRef, Q

        from guide.assistant import NOT_IN_GUIDE
        gaps = RagMessage.objects.filter(conversation=OuterRef("pk"), mode=RagMessage.GUIDE).filter(
            Q(content__startswith=NOT_IN_GUIDE) | Q(status=RagMessage.FAILED))
        return queryset.filter(Exists(gaps))


@admin.register(RagConversation)
class RagConversationAdmin(RightsMixin, admin.ModelAdmin):
    view_rights = change_rights = ("guide", "products", "manage")   # свої розмови (чужі — лише суперадміну)
    list_display = ["title", "user", "messages_count", "updated_at"]
    list_filter = [GuideGapFilter, "user"]
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
class KnowledgeSettingsAdmin(RightsMixin, admin.ModelAdmin):
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
        return super().has_add_permission(request) and not KnowledgeSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
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
        return format_html('<table>{}</table>{}<div style="margin-top:8px"><a class="button" href="{}">'
                           '📊 Огляд бази знань — що є, що в RAG і чому ні</a> <a class="button" href="{}">'
                           '🔐 Доступ — хто може користуватись базою знань і RAG</a></div>', table,
                           format_html('<div style="margin-top:8px"><b>Останні помилки RAG:</b><ul>{}</ul></div>', errors)
                           if errors else "", reverse("admin:rag_assistant_knowledgesettings_overview"),
                           reverse("admin:rag_assistant_knowledgesettings_access"))

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
            path("overview/", self.admin_site.admin_view(self.overview_view),
                 name="rag_assistant_knowledgesettings_overview"),
            path("access/", self.admin_site.admin_view(self.access_view), name="rag_assistant_knowledgesettings_access"),
        ] + super().get_urls()

    def access_view(self, request):
        """«🔐 Доступ до бази знань і RAG»: люди × права (за роллю / ✓ дозволено / ✗ заборонено). Змінювати —
        суперкористувач або «Може керувати юзерами» (access.can_grant), кожна зміна — у журналі аудиту."""
        from django.template.response import TemplateResponse
        can_grant = access.can_grant(request.user)
        if not (can_grant or access.has(request.user, "manage")):
            raise PermissionDenied
        users = list(access.staff_users())
        if request.method == "POST":
            if not can_grant:
                raise PermissionDenied
            from core.utils import client_ip
            ip = client_ip(request)
            changed = 0
            for u in users:
                if u.is_superuser:
                    continue
                values = {}
                for key in access.RIGHT_KEYS:
                    raw = request.POST.get(f"r_{u.pk}_{key}")
                    if raw is None:
                        continue
                    values[key] = {"1": True, "0": False}.get(raw)           # "" — за роллю
                before = access.overrides(u)
                after = access.set_rights(u, values, request.user, ip=ip)
                changed += after != before
            messages.success(request, f"Права збережено (змінено людей: {changed}). Зміни — у журналі аудиту.")
            return redirect(reverse("admin:rag_assistant_knowledgesettings_access"))
        rows = []
        for u in users:
            profile = getattr(u, "profile", None)
            state = access.rights_of(u)
            rows.append({"user": u, "role": profile.get_role_display() if profile else "—",
                         "admin": u.is_superuser or bool(profile and profile.role in access.ADMIN_ROLES),
                         "cells": [{"key": k, **state[k]} for k in access.RIGHT_KEYS]})
        from core.models import UserProfile
        role_table = [{"role": label, "cells": [access.role_default(code, k) for k in access.RIGHT_KEYS]}
                      for code, label in UserProfile.Role.choices]
        return TemplateResponse(request, "admin/rag_assistant/knowledge_access.html", {
            **self.admin_site.each_context(request), "title": "🔐 Доступ до бази знань і RAG", "opts": self.model._meta,
            "rights": access.RIGHTS, "rows": rows, "role_table": role_table, "can_grant": can_grant})

    def overview_view(self, request):
        """«📊 Огляд бази знань»: що є, що в RAG, що чекає, що не вдалося і чому (rag_assistant/knowledge_overview.py)."""
        from django.template.response import TemplateResponse

        from . import knowledge_overview
        if not self.has_view_permission(request):
            raise PermissionDenied
        return TemplateResponse(request, "admin/rag_assistant/knowledge_overview.html", {
            **self.admin_site.each_context(request), "title": "📊 Огляд бази знань",
            "ov": knowledge_overview.build(request.user), "opts": self.model._meta,
            "settings_url": reverse("admin:rag_assistant_knowledgesettings_changelist"),
            "sources_url": reverse("admin:rag_assistant_knowledgesource_changelist"),
            "items_url": reverse("admin:rag_assistant_knowledgeitem_changelist")})

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
        key = APIKey.objects.create(name=f"RAG — база знань ({timezone.localdate():%d.%m.%Y})", scopes=FEED_SCOPES,
                                    log_mode=APIKey.LOG_CHANGES)   # RAG опитує стрічку постійно — журнал лише змін
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
class KnowledgeDocAdmin(RightsMixin, admin.ModelAdmin):
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


def card_links(customer=None, projects=(), order=None, product=None, empty="—"):
    """Прив'язки посиланнями на картки: 👤 клієнт · 📁 P-… назва · 🛒 замовлення · 📦 артикул."""
    parts = []
    if customer is not None:
        parts.append(("crm", "customer", customer.pk, f"👤 {customer}"))
    for p in projects:
        parts.append(("projects", "project", p.pk, f"📁 {p.code} {p.name}".strip()))
    if order is not None:
        parts.append(("sales", "salesorder", order.pk, f"🛒 {order.order_number}"))
    if product is not None:
        parts.append(("inventory", "product", product.pk, f"📦 {product.sku}"))
    if not parts:
        return empty
    return format_html_join(" · ", '<a href="{}" target="_blank">{}</a>', (
        (reverse(f"admin:{app}_{model}_change", args=[pk]), label) for app, model, pk, label in parts))


@admin.register(KnowledgeItem)
class KnowledgeItemAdmin(RightsMixin, admin.ModelAdmin):
    """Файли й посилання бази знань (тип document). Додавати зручніше в картці клієнта / проєкту / замовлення /
    товару («📎 Документи й посилання»); тут — увесь список, пошук, зміна прив'язок і видимості."""
    view_rights = change_rights = ("documents", "manage")
    list_display = ["title", "kind", "links_col", "visibility", "owner", "rag_col", "created_at"]
    list_filter = ["kind", "visibility", "auto", "knowledge_source"]
    search_fields = ["title", "note", "url", "file_name", "source_key", "source_path"]
    autocomplete_fields = ["customer", "project", "order", "product", "offer"]     # пошук за назвою, а не номер
    readonly_fields = ["file_name", "file_size", "content_type", "fetched_at", "fetch_error", "owner", "auto",
                       "source_key", "rag_col", "created_at", "updated_at"]
    fieldsets = [
        (None, {"fields": ["kind", "title", "note", "file", "url", "visibility"]}),
        ("Прив'язки", {"fields": ["customer", "project", "order", "product", "offer"]}),
        ("Стан", {"fields": ["rag_col", "file_name", "file_size", "content_type", "fetched_at", "fetch_error",
                             "owner", "auto", "source_key", "created_at", "updated_at"]}),
    ]

    def get_queryset(self, request):
        qs = (super().get_queryset(request).select_related("owner", "customer", "project", "order", "product",
                                                           "knowledge_source")
              .prefetch_related("knowledge_source__projects"))
        if request.user.is_superuser:
            return qs
        return qs.filter(knowledge_items.visible_q(request.user))        # як у картках: 🔒 свої + 👥 за правами

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
        return card_links(customer=obj.customer if obj.customer_id else None, projects=knowledge_items.item_projects(obj),
                          order=obj.order if obj.order_id else None, product=obj.product if obj.product_id else None)

    @admin.display(description="У RAG")
    def rag_col(self, obj):
        if not obj or not obj.pk:
            return "—"
        from .views_entity import item_json
        return item_json(obj, None)["rag"]


import logging  # noqa: E402
import os  # noqa: E402
from urllib.parse import urlparse  # noqa: E402

from django.conf import settings  # noqa: E402

from . import knowledge_sources  # noqa: E402
from .models import KnowledgeSource  # noqa: E402

logger = logging.getLogger(__name__)


class KnowledgeSourceForm(forms.ModelForm):
    file_types_text = forms.CharField(
        label="Типи файлів", required=False,
        help_text="Через кому; порожньо — pdf, docx, txt, csv, md, html, htm, xlsx. PDF / DOCX / TXT / CSV читає RAG, "
                  "Excel / HTML / Markdown — Minerva. Скани без тексту не розпізнаються.")

    class Meta:
        model = KnowledgeSource
        fields = ["name", "kind", "path", "url", "follow_links", "recursive", "max_files", "max_file_mb", "rescan_hours",
                  "enabled", "visibility", "customer", "projects", "product", "link_products_by_sku"]
        widgets = {"url": forms.Textarea(attrs={
            "rows": 4, "cols": 90,
            "placeholder": "https://docs.example.com/sitemap.xml\nhttps://example.com/produkte/antenne\n"
                           "https://example.com/datenblatt.pdf"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].required = False
        self.fields["name"].help_text = "Порожньо — назва папки або сайту"
        if "path" in self.fields:
            self.fields["path"].help_text = (
                "Порожньо — Minerva створить свою папку (MEDIA/knowledge_sources/<назва>), а файли ви завантажите з ПК "
                "тут же. Або шлях усередині Minerva, напр. /app/knowledge_sources/Dokumentation — лише в дозволених "
                "коренях (список і як підключити папку NAS — нижче).")
        if self.instance and self.instance.pk:
            self.fields["file_types_text"].initial = ", ".join(self.instance.file_types or [])

    def clean(self):
        d = super().clean()
        name = (d.get("name") or "").strip()
        if d.get("kind") == KnowledgeSource.FOLDER:
            path = (d.get("path") or "").strip()
            if "path" not in self.fields:                     # папку створила Minerva — шлях не змінюється
                pass
            elif path:
                try:
                    knowledge_sources.check_folder(path)
                except knowledge_sources.SourceError as e:
                    self.add_error("path", str(e))
                name = name or os.path.basename(path.rstrip("/\\"))
            elif not name:
                self.add_error("name", "Вкажіть назву — так Minerva назве свою папку для файлів")
            else:
                base = os.path.join(str(settings.MEDIA_ROOT), "knowledge_sources")
                try:
                    os.makedirs(base, exist_ok=True)
                except OSError:
                    pass
                if not os.access(base, os.W_OK):
                    self.add_error("path", f"Minerva не може писати в {base} — вкажіть готову папку")
        else:
            lines = list(dict.fromkeys(line.strip() for line in (d.get("url") or "").splitlines() if line.strip()))
            bad = [u for u in lines if urlparse(u).scheme not in ("http", "https") or not urlparse(u).hostname]
            if not lines:
                self.add_error("url", "Вкажіть хоча б одне посилання: sitemap.xml, сторінку сайту або файл")
            elif bad:
                self.add_error("url", "Не схоже на адресу http(s)://…: " + ", ".join(bad[:3]))
            elif len(lines) > LINKS_MAX:
                self.add_error("url", f"Не більше {LINKS_MAX} посилань в одному джерелі")
            d["url"] = "\n".join(lines)
            name = name or (urlparse(lines[0]).hostname if lines and not bad else "")
        d["name"] = name[:200]
        return d

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.path = (obj.path or "").strip()
        obj.file_types = [t.strip().lower().lstrip(".") for t in
                          (self.cleaned_data.get("file_types_text") or "").split(",") if t.strip()]
        if commit:
            obj.save()
        return obj


LINKS_MAX = 200
FILES_SHOWN = 300


@admin.register(KnowledgeSource)
class KnowledgeSourceAdmin(RightsMixin, admin.ModelAdmin):
    """Папки й сайти, які Minerva сама обходить і передає в базу знань (rag_assistant/knowledge_sources.py);
    у папку — «📤 Завантажити файли / папку» з ПК."""
    view_rights = change_rights = ("sources", "manage")
    form = KnowledgeSourceForm
    change_form_template = "admin/rag_assistant/knowledgesource/change_form.html"
    list_display = ["name", "kind", "where_col", "links_col", "enabled", "docs_col", "last_col"]
    list_filter = ["kind", "enabled"]
    autocomplete_fields = ["customer", "projects", "product"]      # пошук за назвою; проєктів — кілька
    readonly_fields = ["roots_help", "upload_panel", "links_panel", "state_panel", "files_panel"]
    actions = ["scan_action"]
    fieldsets = [
        (None, {"fields": ["name", "kind", "path", "url", "follow_links", "roots_help", "enabled"]}),
        ("📤 Файли з ПК або телефона", {"fields": ["upload_panel"], "classes": ["ks-folder-only"]}),
        ("Що брати", {"fields": ["file_types_text", "recursive", "max_files", "max_file_mb", "rescan_hours"]}),
        ("Хто бачить і до чого прив'язати", {"fields": ["visibility", "customer", "projects", "product", "links_panel",
                                                         "link_products_by_sku"]}),
        ("Стан", {"fields": ["state_panel", "files_panel"]}),
    ]

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        return fields + ["path"] if obj is not None and obj.managed else fields

    def get_queryset(self, request):
        """🔒 «лише я» — бачить лише той, хто додав (і суперкористувач)."""
        qs = super().get_queryset(request).select_related("customer", "product").prefetch_related("projects")
        if request.user.is_superuser:
            return qs
        return qs.filter(~Q(visibility=KnowledgeSource.PRIVATE) | Q(owner=request.user))

    def get_urls(self):
        view = self.admin_site.admin_view
        return [path("browse/", view(self.browse_view), name="rag_assistant_knowledgesource_browse"),
                path("<int:pk>/scan/", view(self.scan_view), name="rag_assistant_knowledgesource_scan"),
                path("<int:pk>/upload/", view(self.upload_view), name="rag_assistant_knowledgesource_upload"),
                path("<int:pk>/status/", view(self.status_view), name="rag_assistant_knowledgesource_status"),
                path("<int:pk>/delete-file/", view(self.delete_file_view),
                     name="rag_assistant_knowledgesource_delete_file")] + super().get_urls()

    def save_model(self, request, obj, form, change):
        if not change:
            obj.owner = request.user
        if obj.kind == KnowledgeSource.FOLDER and not obj.path:
            obj.path, obj.managed = knowledge_sources.new_folder(obj.name), True
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)       # проєкти (M2M) збережено
        obj = form.instance
        if change and "projects" in form.changed_data:
            knowledge_sources.touch_items(obj)                        # документи — з новими проєктами для RAG
        knowledge_sources.start_scan(obj)          # одразу після збереження — перевірити (у фоні)
        if not obj.managed or obj.items.exists():
            messages.info(request, "Перевірку джерела запущено у фоні — стан оновиться за хвилину.")

    def response_add(self, request, obj, post_url_continue=None):
        if obj.managed and "_addanother" not in request.POST:
            messages.success(request, f"Папку створено: {obj.path}. Тепер завантажте файли з ПК — «📤 Завантажити файли».")
            return redirect(reverse("admin:rag_assistant_knowledgesource_change", args=[obj.pk]))
        return super().response_add(request, obj, post_url_continue)

    def delete_model(self, request, obj):
        obj.delete()                               # документи джерела → tombstone (сигнал post_delete)

    def _source(self, request, pk, change=True):
        source = self.get_queryset(request).filter(pk=pk).first()
        allowed = source is not None and (self.has_change_permission(request, source) if change
                                          else self.has_view_permission(request, source))
        if not allowed:
            raise PermissionDenied
        return source

    def browse_view(self, request):
        """GET ?path= — папки на сервері для поля «Папка» (вибір і підказки шляху), лише в дозволених коренях."""
        if not (self.has_add_permission(request) or self.has_change_permission(request)):
            raise PermissionDenied
        try:
            return JsonResponse({"ok": True, **knowledge_sources.browse(request.GET.get("path", ""))})
        except knowledge_sources.SourceError as e:
            return JsonResponse({"ok": False, "error": str(e)})

    def scan_view(self, request, pk):
        source = self._source(request, pk)
        if request.method != "POST":
            raise PermissionDenied
        if knowledge_sources.start_scan(source):
            messages.success(request, f"«{source.name}»: перевірку запущено у фоні.")
        else:
            messages.warning(request, f"«{source.name}»: перевірка вже йде.")
        return redirect(reverse("admin:rag_assistant_knowledgesource_change", args=[pk]))

    def upload_view(self, request, pk):
        """POST (multipart: file, relpath, subfolder, replace) — один файл з ПК у папку джерела;
        {action: done} — усі файли надіслано → перевірити джерело (зараз або одразу після поточної перевірки)."""
        source = self._source(request, pk)
        if request.method != "POST":
            return JsonResponse({"ok": False, "error": "Потрібен POST"}, status=405)
        if request.POST.get("action") == "done":
            return JsonResponse({"ok": True, "scan": knowledge_sources.request_scan(source)})
        upload = request.FILES.get("file")
        if upload is None:
            return JsonResponse({"ok": False, "error": "Виберіть файл"}, status=400)
        try:
            result = knowledge_sources.save_upload(
                source, upload, relpath=request.POST.get("relpath", ""), subfolder=request.POST.get("subfolder", ""),
                replace=request.POST.get("replace") == "1")
        except knowledge_sources.SourceError as e:
            return JsonResponse({"ok": False, "name": upload.name, "error": str(e)})
        logger.info("knowledge source %s: %s uploaded %s (%s)", source.pk, request.user, result["path"], result["status"])
        return JsonResponse({"ok": True, **result})

    def status_view(self, request, pk):
        source = self._source(request, pk, change=False)
        return JsonResponse({"running": knowledge_sources.running(source), "documents": source.items.count(),
                             "last": source.last_scan_result, "error": source.last_error})

    def delete_file_view(self, request, pk):
        source = self._source(request, pk)
        if request.method != "POST":
            return JsonResponse({"ok": False, "error": "Потрібен POST"}, status=405)
        rel = request.POST.get("path", "")
        try:
            removed = knowledge_sources.delete_file(source, rel)
        except knowledge_sources.SourceError as e:
            return JsonResponse({"ok": False, "error": str(e)})
        logger.info("knowledge source %s: %s deleted %s", source.pk, request.user, rel)
        return JsonResponse({"ok": True, "removed": removed})

    @admin.action(description="🔄 Перевірити зараз")
    def scan_action(self, request, queryset):
        n = sum(1 for s in queryset if knowledge_sources.start_scan(s))
        self.message_user(request, f"Запущено перевірок: {n} (у фоні).")

    @admin.display(description="Де")
    def where_col(self, obj):
        if obj.kind == KnowledgeSource.FOLDER:
            return format_html("📤 {}", obj.path) if obj.managed else obj.path
        links = knowledge_sources.links_of(obj)
        return (links[0] + (f" + ще {len(links) - 1}" if len(links) > 1 else "")) if links else "—"

    @admin.display(description="Прив'язки")
    def links_col(self, obj):
        return card_links(customer=obj.customer, projects=list(obj.projects.all()), product=obj.product,
                          empty="загальні")

    @admin.display(description="Картки")
    def links_panel(self, obj):
        if not obj or not obj.pk:
            return "Після збереження тут будуть посилання на картки клієнта, проєктів і товару."
        return format_html(
            "{}<div class='help'>Документи джерела видно в «📎 Документи й посилання» цих карток; у RAG вони прив'язані "
            "до всіх вибраних проєктів. Без прив'язок — загальні документи.</div>",
            card_links(customer=obj.customer, projects=list(obj.projects.all()), product=obj.product,
                       empty="загальні документи — без клієнта, проєкту й товару"))

    @admin.display(description="Документів")
    def docs_col(self, obj):
        n = obj.items.count()
        url = reverse("admin:rag_assistant_knowledgeitem_changelist") + f"?knowledge_source__id__exact={obj.pk}"
        return format_html('<a href="{}">{}</a>', url, n)

    @admin.display(description="Остання перевірка")
    def last_col(self, obj):
        if knowledge_sources.running(obj):
            return "⏳ іде перевірка…"
        if obj.last_error:
            return format_html('<span style="color:#e57373">⚠️ {}</span>', obj.last_error)
        return obj.last_scan_result or "ще не було"

    @admin.display(description="Де можна брати папки")
    def roots_help(self, obj):
        roots = format_html_join("", "<li><code>{}</code></li>", ((r,) for r in knowledge_sources.allowed_roots()))
        return format_html(
            '<div style="max-width:760px">Найпростіше — залишити «Папка» порожньою: Minerva створить свою папку, а '
            'файли (і цілі папки з ПК) ви виберете кнопками «📤» нижче.<br>Готову папку на сервері — кнопкою '
            '«📂 Вибрати папку на сервері…»; Minerva читає лише ці корені (файли не копіює):<ul>{}</ul>'
            'Можна покласти документи в <code>knowledge_sources</code> у теці '
            'Minerva на NAS (<code>/volume1/docker/tabele_mvp/knowledge_sources/…</code>): у Docker вона вже видна як '
            '<code>/app/knowledge_sources/…</code>.<br>Іншу папку NAS — змонтувати в <code>docker-compose.yml</code> '
            '(сервіси web і cron, напр. <code>- /volume1/Office/Dokumentation:/knowledge/dokumentation:ro</code>) '
            'і додати <code>MINERVA_KNOWLEDGE_ROOTS: "/knowledge"</code> у environment.</div>', roots)

    @admin.display(description="Завантажити")
    def upload_panel(self, obj):
        """Кнопки вибору файлів / папки на ПК (телефоні). На сторінці «Додати» вибране чекає «Зберегти»: JS створює
        джерело (той самий POST форми) і завантажує файли в нього."""
        if obj is not None and obj.pk and obj.kind != KnowledgeSource.FOLDER:
            return "—"
        new = obj is None or not obj.pk
        types = sorted(knowledge_sources._types(obj) if not new else knowledge_sources.DEFAULT_TYPES)
        return render_to_string("admin/rag_assistant/knowledgesource/upload_panel.html", {
            "source": obj, "new": new, "types": types, "accept": ",".join("." + t for t in types),
            "max_mb": KnowledgeSource._meta.get_field("max_file_mb").default if new else obj.max_file_mb,
            "base_url": reverse("admin:rag_assistant_knowledgesource_changelist"),
            "upload_url": "" if new else reverse("admin:rag_assistant_knowledgesource_upload", args=[obj.pk]),
            "status_url": "" if new else reverse("admin:rag_assistant_knowledgesource_status", args=[obj.pk]),
            "change_url": "" if new else reverse("admin:rag_assistant_knowledgesource_change", args=[obj.pk])})

    @admin.display(description="Стан")
    def state_panel(self, obj):
        if not obj or not obj.pk:
            return "Після збереження Minerva одразу перевірить джерело."
        from django.db.models import Count
        from .models import KnowledgeDoc
        ids = list(obj.items.values_list("pk", flat=True))
        states = dict(KnowledgeDoc.objects.filter(doc_type="document", obj_id__in=ids, fed=True)
                      .values_list("state").annotate(n=Count("pk")))
        scan_url = reverse("admin:rag_assistant_knowledgesource_scan", args=[obj.pk])
        return format_html(
            '<div>{}</div><div style="margin-top:4px">Документів: <b>{}</b> · передано в базу знань: <b>{}</b> · '
            'у RAG: <b>{}</b> · помилки / пропущено: {} / {}</div>'
            '<div style="margin-top:8px"><button type="submit" class="button" formaction="{}" formmethod="post">'
            '🔄 Перевірити зараз</button> <a class="button" href="{}">📊 Огляд бази знань</a></div>',
            self.last_col(obj), len(ids), sum(states.values()), states.get("indexed", 0), states.get("failed", 0),
            states.get("skipped", 0), scan_url, reverse("admin:rag_assistant_knowledgesettings_overview"))

    @admin.display(description="Документи джерела")
    def files_panel(self, obj):
        if not obj or not obj.pk:
            return "—"
        from . import retrieval
        from .models import KnowledgeDoc
        from .views_entity import rag_text
        items = list(obj.items.order_by("source_path")[:FILES_SHOWN])
        docs = {d.obj_id: d for d in KnowledgeDoc.objects.filter(doc_type="document", obj_id__in=[i.pk for i in items])}
        supported = retrieval.documents_supported()
        rows = [{"item": i, "rag": rag_text(i, docs.get(i.pk), supported),
                 "ok": getattr(docs.get(i.pk), "current_state", "") == "indexed" and supported,
                 "size_kb": max(1, round(i.file_size / 1024)) if i.file_size else 0} for i in items]
        return render_to_string("admin/rag_assistant/knowledgesource/files_panel.html", {
            "source": obj, "rows": rows, "total": obj.items.count(), "shown": FILES_SHOWN,
            "can_delete": obj.managed and obj.kind == KnowledgeSource.FOLDER,
            "delete_url": reverse("admin:rag_assistant_knowledgesource_delete_file", args=[obj.pk]),
            "all_url": reverse("admin:rag_assistant_knowledgeitem_changelist") + f"?knowledge_source__id__exact={obj.pk}"})
