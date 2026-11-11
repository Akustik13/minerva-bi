"""🎙 Наради — сторінки в адмінці (усе — лише власнику наради; суперкористувач і адміністратор Minerva чужих нарад
не бачать: список, сторінка й кожна дія відповідають 404).

Адреси (MeetingAdmin.get_urls; жодна не закінчується на add / change / delete — Layer 4 ModuleAccessMiddleware):
  new/                       підготовка «🎙 Записати нараду» (GET форма, POST — services.create)
  <pk>/page/                 сторінка наради (change_view веде сюди)
  <pk>/record/               POST → одноразове посилання → 302 на вікно запису (RAG-ПК) з #minerva=<квиток>
  <pk>/status/               GET JSON для опитування (без розшифровки)
  <pk>/item/<item>/          POST JSON ✓ / ✕ / ↺ пункту → свіжий фрагмент
  <pk>/kb/                   POST «📚 Додати в мою базу знань»
  <pk>/save/                 POST «✏️ Змінити»
  <pk>/retry/                POST «🔄 Спробувати ще раз»
  <pk>/continue/             GET підготовка продовження («… (продовження)»)
  <pk>/remove/               POST «🗑 Видалити нараду»
  protocols/                 «🧾 Протоколи згоди» — лише адміністратор Minerva, лише метадані
Права (правило 16 CLAUDE.md): зміни — лише POST; item / save / kb / retry — ще й has_perm('meetings.change_meeting'),
record / continue / new — services.can_record (право add + записування ввімкнено); remove — власнику завжди (прибрати
свій запис і розшифровку людина може й тоді, коли їй лишили лише перегляд). Кнопок без права сторінка не показує.
Логіка — meetings/services.py, що показати — meetings/admin_pages.py.
"""
import contextvars
import json

from django import forms
from django.contrib import admin, messages
from django.db.models import Count, Q
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.http import urlencode

from . import admin_pages, consent, knowledge, services
from .models import Meeting, MeetingItem, MeetingSettings
from .services import MeetingError


def _json_body(request) -> dict:
    if (request.content_type or "").startswith("application/json"):
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {k: request.POST.get(k) for k in request.POST}


def _post_only(request):
    """Не POST → 405 (JSON для fetch, сторінка — для звичайного переходу)."""
    if request.method == "POST":
        return None
    response = JsonResponse({"ok": False, "error": "Лише POST"}, status=405)
    response["Allow"] = "POST"
    return response


def _digits(value):
    value = str(value or "").strip()
    return value if value.isdigit() else None


class PendingFilter(admin.SimpleListFilter):
    title = "Чекають ✓ / ✕"
    parameter_name = "pending"

    def lookups(self, request, model_admin):
        return (("yes", "Є пункти, що чекають ✓ / ✕"), ("no", "Усе перевірено"))

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(pending_n__gt=0)
        if self.value() == "no":
            return queryset.filter(pending_n=0)
        return queryset


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    list_display = ("date_col", "title_col", "project_col", "status_col", "pending_col")
    list_display_links = None
    list_filter = ("status", PendingFilter)
    search_fields = ("title",)
    actions = None
    list_per_page = 50
    change_list_template = "admin/meetings/meeting/change_list.html"

    # ── список: лише свої наради (і для суперкористувача) ──
    def get_queryset(self, request):
        return (super().get_queryset(request).filter(owner=request.user).select_related("project")
                .annotate(pending_n=Count("items", filter=Q(items__status=MeetingItem.PENDING))))

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.owner_id != request.user.pk:
            return False
        return super().has_change_permission(request, obj)

    def changelist_view(self, request, extra_context=None):
        extra = {"title": "🎙 Мої наради", "can_record": services.can_record(request.user),
                 "new_url": reverse("admin:meetings_meeting_new"),
                 "refusal": services.DISABLED_TEXT if not services.enabled()
                 and request.user.has_perm("meetings.add_meeting") else "",
                 "is_admin": services._is_admin(request.user),
                 "protocols_url": reverse("admin:meetings_meeting_protocols")}
        return super().changelist_view(request, {**(extra_context or {}), **extra})

    def add_view(self, request, form_url="", extra_context=None):
        return redirect(reverse("admin:meetings_meeting_new") + (f"?{request.GET.urlencode()}" if request.GET else ""))

    def change_view(self, request, object_id, form_url="", extra_context=None):
        m = services.get_own(request.user, _digits(object_id))
        return redirect(services.page_path(m))

    @admin.display(description="Коли", ordering="created_at")
    def date_col(self, obj):
        start = admin_pages.local(obj.start)
        return f"{start:%d.%m.%Y %H:%M}" if start else "—"

    @admin.display(description="Нарада", ordering="title")
    def title_col(self, obj):
        return format_html('<a href="{}"><span translate="no">{}</span></a>', services.page_path(obj), obj.title)

    @admin.display(description="Проєкт", ordering="project__code")
    def project_col(self, obj):
        return format_html('<span translate="no">{}</span>', obj.project.code) if obj.project_id else "—"

    @admin.display(description="Стан", ordering="status")
    def status_col(self, obj):
        return format_html('<span class="mt-chip mt-chip-{}">{}</span>', admin_pages.STATUS_TONE.get(obj.status, "idle"),
                           obj.get_status_display())

    @admin.display(description="Чекають ✓ / ✕", ordering="pending_n")
    def pending_col(self, obj):
        n = getattr(obj, "pending_n", 0) or 0
        return format_html('<b>{}</b>', n) if n else "—"

    # ── адреси ──
    def get_urls(self):
        w = self.admin_site.admin_view
        own = [
            path("new/", w(self.new_view), name="meetings_meeting_new"),
            path("protocols/", w(self.protocols_view), name="meetings_meeting_protocols"),
            path("<int:pk>/page/", w(self.page_view), name="meetings_meeting_page"),
            path("<int:pk>/record/", w(self.record_view), name="meetings_meeting_record"),
            path("<int:pk>/status/", w(self.status_view), name="meetings_meeting_status"),
            path("<int:pk>/item/<int:item_pk>/", w(self.item_view), name="meetings_meeting_item"),
            path("<int:pk>/kb/", w(self.kb_view), name="meetings_meeting_kb"),
            path("<int:pk>/save/", w(self.save_view), name="meetings_meeting_save"),
            path("<int:pk>/retry/", w(self.retry_view), name="meetings_meeting_retry"),
            path("<int:pk>/continue/", w(self.continue_view), name="meetings_meeting_continue"),
            path("<int:pk>/remove/", w(self.remove_view), name="meetings_meeting_remove"),
        ]
        return own + super().get_urls()

    def _ctx(self, request, title, **extra):
        return {**self.admin_site.each_context(request), "title": title, "opts": self.model._meta,
                "list_url": reverse("admin:meetings_meeting_changelist"), **extra}

    def _page(self, m, anchor=""):
        return redirect(services.page_path(m) + anchor)

    # ── підготовка ──
    def _prepare(self, request, state, *, error="", status=200, continues=None):
        refusal = services.record_refusal(request.user)
        ctx = self._ctx(request, "🎙 Записати нараду", refusal=refusal, error=error, continues=continues,
                        form_url=reverse("admin:meetings_meeting_new"))
        if not refusal and state is not None:
            ctx.update(admin_pages.prepare_context(request.user, state))
        return TemplateResponse(request, "admin/meetings/meeting/prepare.html", ctx, status=status)

    def new_view(self, request):
        user = request.user
        if services.record_refusal(user):
            return self._prepare(request, None, status=403 if request.method == "POST" else 200)
        if request.method == "POST":
            try:
                m = services.create(user, admin_pages.data_from_post(request.POST))
            except MeetingError as e:
                return self._prepare(request, admin_pages.state_from_post(user, request.POST), error=str(e),
                                     status=400)
            messages.success(request, "🎙 Нараду підготовлено. Коли всі зберуться — «🎙 Відкрити вікно запису»; "
                                      "згоду підтвердите там перед ▶.")
            return self._page(m)
        data = services.prefill(user, project=_digits(request.GET.get("project")),
                                event=_digits(request.GET.get("event")),
                                customer=_digits(request.GET.get("customer")))
        return self._prepare(request, admin_pages.state_from_prefill(data))

    def continue_view(self, request, pk):
        m = services.get_own(request.user, pk)
        if services.record_refusal(request.user):
            return self._prepare(request, None)
        state = admin_pages.state_from_prefill(services.continue_prefill(request.user, m))
        return self._prepare(request, state, continues=m)

    # ── сторінка наради ──
    def page_view(self, request, pk):
        m = services.get_own(request.user, pk)
        ctx = self._ctx(request, f"🎙 Нарада від {m.day:%d.%m.%Y}", **admin_pages.page_context(request.user, m))
        return TemplateResponse(request, "admin/meetings/meeting/page.html", ctx)

    def status_view(self, request, pk):
        m = services.get_own(request.user, pk)
        return JsonResponse(admin_pages.status_json(m))

    def _change_refused(self, request, m, *, as_json=False):
        """Дії, що змінюють нараду (✓ / ✕, «✏️ Змінити», база знань, «🔄 Спробувати ще раз»), — лише власнику з
        правом змінювати наради (роль або «✏️ Змінити доступ»). «🗑 Видалити нараду» — власнику завжди (приватність:
        прибрати свій запис людина може й без права змінювати)."""
        if admin_pages.can_change(request.user):
            return None
        if as_json:
            return JsonResponse({"ok": False, "error": admin_pages.NO_CHANGE_TEXT}, status=403)
        messages.error(request, f"❌ {admin_pages.NO_CHANGE_TEXT}")
        return self._page(m)

    def record_view(self, request, pk):
        refused = _post_only(request)
        if refused:
            return refused
        m = services.get_own(request.user, pk)
        if not services.can_record(request.user):
            messages.error(request, f"❌ {services.record_refusal(request.user) or services.NO_RIGHT_TEXT}")
            return self._page(m)
        try:
            ticket = services.issue_ticket(m, request.user)
        except MeetingError as e:
            messages.error(request, f"❌ {e}")
            return self._page(m)
        response = HttpResponseRedirect(services.record_url(ticket))
        response["Referrer-Policy"] = "no-referrer"
        return response

    def item_view(self, request, pk, item_pk):
        refused = _post_only(request)
        if refused:
            return refused
        m = services.get_own(request.user, pk)
        item = services.item_for(request.user, m, item_pk)
        refused = self._change_refused(request, m, as_json=True)
        if refused:
            return refused
        data = _json_body(request)
        action = str(data.get("action") or "")
        try:
            if action == "accept":
                fields = {k: data[k] for k in ("title", "kind", "category", "probability", "impact", "owner", "date",
                                               "side") if data.get(k) not in (None, "")}
                if "assignee" in data:
                    fields["assignee"] = data.get("assignee") or None
                services.accept(request.user, item, str(data.get("target") or ""), fields)
            elif action == "reject":
                services.reject(request.user, item)
            elif action == "restore":
                services.restore(request.user, item)
            else:
                return JsonResponse({"ok": False, "error": "Невідома дія"}, status=400)
        except MeetingError as e:
            return JsonResponse({"ok": False, "error": str(e)}, status=400)
        item = MeetingItem.objects.select_related("highlight", "risk", "step", "event").get(pk=item.pk)
        return JsonResponse({"ok": True, "status": item.status, "html": admin_pages.render_item(request.user, m, item),
                             "pending": m.items.filter(status=MeetingItem.PENDING).count()})

    def kb_view(self, request, pk):
        refused = _post_only(request)
        if refused:
            return refused
        m = services.get_own(request.user, pk)
        refused = self._change_refused(request, m)
        if refused:
            return refused
        try:
            pks = knowledge.publish(m)
        except MeetingError as e:
            messages.error(request, f"❌ {e}")
        else:
            n = len(pks)
            messages.success(request, f"📚 Розшифровку додано у вашу базу знань: {n} "
                                      f"{admin_pages.plural(n, 'документ', 'документи', 'документів')} (бачите лише "
                                      f"ви).")
        return self._page(m, "#kb")

    def save_view(self, request, pk):
        refused = _post_only(request)
        if refused:
            return refused
        m = services.get_own(request.user, pk)
        refused = self._change_refused(request, m)
        if refused:
            return refused
        post = request.POST
        data = {"title": post.get("title", ""), "consent_note": post.get("consent_note", "")}
        if post.get("participants_form"):
            data["participants"] = admin_pages.participants_from_post(post)
        for key in ("project", "customer"):
            if key in post:
                data[key] = post.get(key) or None
        if m.status == Meeting.READY:
            for key in ("lang_l", "lang_r", "retention", "consent_lang"):
                if key in post:
                    data[key] = post.get(key)
        try:
            services.edit(request.user, m, data)
        except MeetingError as e:
            messages.error(request, f"❌ {e}")
        else:
            messages.success(request, "✏️ Зміни збережено")
        return self._page(m)

    def retry_view(self, request, pk):
        refused = _post_only(request)
        if refused:
            return refused
        m = services.get_own(request.user, pk)
        refused = self._change_refused(request, m)
        if refused:
            return refused
        try:
            services.retry(request.user, m)
        except MeetingError as e:
            messages.error(request, f"❌ {e}")
        else:
            messages.info(request, services.RETRY_TEXT)
        return self._page(m)

    def remove_view(self, request, pk):
        refused = _post_only(request)
        if refused:
            return refused
        m = services.get_own(request.user, pk)
        # без перевірки права змінювати: видалити свій запис (і розшифровку) власник може завжди — приватність
        out = services.delete_meeting(request.user, m, scrub=request.POST.get("scrub") == "1", request=request)
        text = "🗑 Нараду, розшифровку й документи бази знань видалено."
        if out["accepted"]:
            text += (f" Пунктів, уже доданих у проєкт або календар, лишилось: {out['accepted']}"
                     + (" (без цитат)." if out["scrubbed"] else "."))
        messages.success(request, text)
        return redirect(reverse("admin:meetings_meeting_changelist"))

    # ── «🧾 Протоколи згоди» (адміністратор Minerva; лише метадані) ──
    def protocols_view(self, request):
        user = request.user
        if not services._is_admin(user):
            raise Http404("Сторінку не знайдено")
        if request.method == "POST":
            if request.POST.get("action") == "delete":
                m = Meeting.objects.filter(pk=_digits(request.POST.get("pk")) or 0).first()
                if m is None:
                    raise Http404("Нараду не знайдено")
                n = m.pk
                services.admin_delete(user, m, request=request)
                messages.success(request, f"🗑 Нараду #{n} видалено (у журналі аудиту — лише номер і час згоди).")
            q = request.POST.get("q", "")
            return redirect(reverse("admin:meetings_meeting_protocols") + (f"?{urlencode({'q': q})}" if q else ""))
        q = request.GET.get("q", "")
        rows = [dict(r, lang_label=admin_pages.CONSENT_LANG_LABELS.get(r["consent_lang"], ""))
                for r in services.protocols(user, q)]
        ctx = self._ctx(request, "🧾 Протоколи згоди", rows=rows, q=q, total=len(rows),
                        max_rows=services.PROTOCOLS_MAX)
        return TemplateResponse(request, "admin/meetings/meeting/protocols.html", ctx)


# ── ⚙️ Налаштування нарад ────────────────────────────────────────────────────

_REQUEST = contextvars.ContextVar("meetings_settings_request", default=None)


class MeetingSettingsForm(forms.ModelForm):
    texts_reviewed = forms.BooleanField(
        required=False, label="Тексти згоди перевірив юрист / DSB",
        help_text="Відмітьте, коли тексти для читання вголос і примітку для запрошення перевірили. Minerva запам’ятає, "
                  "хто й коли.")

    class Meta:
        model = MeetingSettings
        exclude = ("bench_seen_at", "bench_info", "cleanup_at", "texts_reviewed_at", "texts_reviewed_by")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["texts_reviewed"].initial = bool(getattr(self.instance, "texts_reviewed_at", None))
        for lang in consent.LANGS:
            name = f"consent_text_{lang}"
            if name in self.fields:
                built_in = "\n---\n".join(x for x in consent._split(consent._read(lang)) if x)
                self.fields[name].widget = forms.Textarea(attrs={
                    "rows": 6, "cols": 90, "translate": "no", "placeholder": built_in})


@admin.register(MeetingSettings)
class MeetingSettingsAdmin(admin.ModelAdmin):
    form = MeetingSettingsForm
    readonly_fields = ("checklist_panel", "reviewed_info", "bench_panel", "keys_panel", "backup_note",
                       "protocols_panel")
    fieldsets = (
        ("🚦 Запуск", {
            "fields": ("checklist_panel", "enabled", "texts_reviewed", "reviewed_info", "privacy_url",
                       "works_council_informed", "protocols_panel"),
            "description": "Поки «Записування нарад увімкнено» вимкнено, кнопок «🎙 Записати нараду» ніде немає. "
                           "Вмикайте після перевірки текстів згоди й посилання на інформацію за ст. 13 DSGVO."}),
        ("🎙 Вікно запису (RAG-ПК)", {"fields": ("bench_url", "ticket_ttl_seconds", "bench_panel")}),
        ("🔑 Ключі для RAG-ПК", {"fields": ("keys_panel",)}),
        ("🗣 Згода — тексти для читання вголос", {
            "fields": ("consent_text_de", "consent_text_en", "consent_text_uk"),
            "description": "Порожньо — вбудований текст (сірим у полі). Спершу питання, яке читають перед ▶, потім "
                           "рядок «---» і фраза, яку кажуть, щойно запис почався. Що прочитали — те й лягає в "
                           "протокол згоди наради."}),
        ("🗄 Зберігання", {"fields": ("default_retention", "transcript_retention_days", "audio_retention_days",
                                     "protocol_retention_days", "inactive_owner_days", "backup_note")}),
        ("🔔 Сповіщення", {"fields": ("notify_email", "notify_telegram"),
                          "description": "Лист і Telegram тому, хто записав: лише дата, код проєкту й скільки пунктів "
                                         "чекають ✓ / ✕ — без назви й тексту наради."}),
    )

    def has_add_permission(self, request):
        return (services._is_admin(request.user) and not MeetingSettings.objects.exists())

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return services._is_admin(request.user)

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_or_change_permission(request):
            raise Http404
        return redirect(reverse("admin:meetings_meetingsettings_change", args=[MeetingSettings.load().pk]))

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        token = _REQUEST.set(request)
        try:
            response = super().changeform_view(request, object_id, form_url, extra_context)
            if hasattr(response, "render") and not getattr(response, "is_rendered", True):
                response.render()          # поля «тільки читання» малюються зараз — поки відомо, хто дивиться
            return response
        finally:
            _REQUEST.reset(token)

    def save_model(self, request, obj, form, change):
        reviewed = bool(form.cleaned_data.get("texts_reviewed"))
        if reviewed != bool(obj.texts_reviewed_at):
            services.mark_texts_reviewed(obj, request.user, reviewed)
        super().save_model(request, obj, form, change)

    def get_urls(self):
        own = [path("key/", self.admin_site.admin_view(self.key_view), name="meetings_meetingsettings_key")]
        return own + super().get_urls()

    def key_view(self, request):
        """«🔑 Створити ключ для RAG-ПК (наради)» — лише POST і з правом створювати ключі API → сторінка ключа."""
        from api.models import APIKey
        back = redirect(reverse("admin:meetings_meetingsettings_change", args=[MeetingSettings.load().pk]))
        if request.method != "POST" or not request.user.has_perm("api.add_apikey"):
            messages.error(request, "❌ Немає права створювати ключі API")
            return back
        key = APIKey.objects.create(name=f"RAG-ПК — наради ({timezone.localdate():%d.%m.%Y})",
                                    scopes=["meetings:write"], log_mode=APIKey.LOG_ERRORS)
        messages.success(request, "🔑 Створено ключ для RAG-ПК (наради). Скопіюйте його з цієї сторінки й вставте на "
                                  "RAG-ПК: «RAG — керування» → «🎙 Наради» → «Ключ Minerva (наради)». Старий ключ, "
                                  "якщо він більше не потрібен, вимкніть.")
        return redirect(reverse("admin:api_apikey_change", args=[key.pk]))

    @admin.display(description="Тексти згоди перевірив юрист / DSB", boolean=True)
    def texts_reviewed(self, obj):
        """Для перегляду без права змінювати (у формі — галочка MeetingSettingsForm.texts_reviewed)."""
        return bool(obj and obj.texts_reviewed_at)

    @admin.display(description="Що ще треба до першого запису")
    def checklist_panel(self, obj):
        rows = services.rollout_checklist(obj)
        return format_html('<ul class="mt-check">{}</ul>', format_html_join(
            "", '<li class="{}">{} {}</li>', (("ok" if r["ok"] else "todo", "✅" if r["ok"] else "⬜", r["label"])
                                              for r in rows)))

    @admin.display(description="Хто відмітив перевірку текстів")
    def reviewed_info(self, obj):
        if not obj or not obj.texts_reviewed_at:
            return "ще ніхто"
        who = services.user_name(obj.texts_reviewed_by) if obj.texts_reviewed_by_id else "—"
        return format_html('<span translate="no">{}</span> · {}', who,
                           f"{timezone.localtime(obj.texts_reviewed_at):%d.%m.%Y %H:%M}")

    @admin.display(description="RAG-ПК на зв'язку")
    def bench_panel(self, obj):
        return format_html('{}<div class="help">Стан надсилає сам RAG-ПК (кожні ~2 хв). Перевірити, чи відкривається '
                           'вікно запису з цього ПК: <a href="{}" target="_blank" rel="noopener">🔌 Перевірити вікно '
                           'запису</a>.</div>', admin_pages.bench_text(obj), services.bench_url(obj) + "/#check")

    @admin.display(description="Ключі для RAG-ПК (наради)")
    def keys_panel(self, obj):
        from core.secrets import mask
        keys = services.bench_keys()
        rows = format_html_join("", "<li><a href='{}'>{}</a> · <span translate='no'>{}</span> · {}</li>", (
            (reverse("admin:api_apikey_change", args=[k.pk]), k.name, mask(k.key),
             f"востаннє {timezone.localtime(k.last_used):%d.%m.%Y %H:%M}" if k.last_used else "ще не використовувався")
            for k in keys))
        listing = format_html("<ul style='margin:0;padding-left:18px'>{}</ul>", rows) if keys else format_html(
            '<span class="mt-warn">{}</span>', "ще немає — RAG-ПК не зможе передавати розшифровки")
        request = _REQUEST.get()
        button = ""
        if request is not None and request.user.has_perm("api.add_apikey"):
            button = format_html('<button type="submit" class="button" formaction="{}" formnovalidate>'
                                 '🔑 Створити ключ для RAG-ПК (наради)</button>',
                                 reverse("admin:meetings_meetingsettings_key"))
        return format_html(
            "{}<div style='margin-top:6px'>{}</div><div class='help'>Службовий ключ лише з правом "
            "<code>meetings:write</code>: ним RAG-ПК обмінює посилання на вікно запису й передає розшифровку та "
            "ключові моменти. Повний ключ — на його сторінці; його вставляють на RAG-ПК («RAG — керування» → "
            "«🎙 Наради»). Журнал запитів ключа — лише помилки.</div>", listing, button)

    @admin.display(description="Резервні копії й інші винятки")
    def backup_note(self, obj):
        return ("ℹ️ Строки вище діють у робочій базі. Резервні копії Minerva містять розшифровки, доки копію не "
                "перезаписано (зберігаються останні копії з «Резервного копіювання»); адміністратор сервера технічно "
                "має доступ до бази й копій. На RAG-ПК вектори бази знань можуть лишатися в знімках до їх "
                "перезапису. Вкажіть це в інформації за ст. 13 DSGVO.")

    @admin.display(description="Протоколи згоди")
    def protocols_panel(self, obj):
        return format_html('<a href="{}">🧾 Протоколи згоди</a> <span class="help">— лише метадані (дата, хто '
                           'записав, час і галочки згоди, адреси учасників) для запитів за ст. 15 / 17 DSGVO; '
                           'бачить лише адміністратор Minerva</span>', reverse("admin:meetings_meeting_protocols"))
