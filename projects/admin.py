import logging

from django import forms
from django.contrib import admin, messages
from django.db import models as dj_models
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from . import services
from .models import DismissedThread, Project, ProjectHighlight, ProjectLink, ProjectParticipant

logger = logging.getLogger(__name__)


class ParticipantInline(admin.TabularInline):
    model = ProjectParticipant
    extra = 0
    fields = ("email", "name", "role", "customer")
    autocomplete_fields = ("customer",)
    verbose_name_plural = "👥 Учасники (з листування — автоматично; роль можна виправити)"


class MailLinkInline(admin.TabularInline):
    """Гілки й листи: статус можна підтвердити / відхилити; додаються з пошти або «Розбору пошти».
    Тема й посилання — лише власнику скриньки (як «📬 Листи зі скриньки»)."""
    model = ProjectLink
    fk_name = "project"
    extra = 0
    fields = ("target", "status", "source", "reason", "created_at")
    readonly_fields = ("target", "source", "reason", "created_at")
    verbose_name_plural = "✉️ Листування (додати — у пошті: «📁 До проєкту» або «Розбір пошти»)"

    def get_queryset(self, request):
        self._uid = request.user.pk
        return (super().get_queryset(request).filter(Q(thread__isnull=False) | Q(message__isnull=False))
                .select_related("thread__account", "message__account"))

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Гілка / лист")
    def target(self, obj):
        from .cockpit import link_label
        label, url = link_label(obj, getattr(self, "_uid", None))
        return format_html('<a href="{}" target="_blank">{}</a>', url, label) if url else label


class _ObjectLinkInline(admin.TabularInline):
    """Прив'язка замовлення, закупівлі, JLC-замовлення чи відправлення: ціль обов'язкова; пропозиції Minerva
    приходять зі статусом «Запропоновано» — підтвердьте або відхиліть (відхилене не пропонується знову)."""
    model = ProjectLink
    fk_name = "project"
    extra = 0
    target_field = ""
    readonly_fields = ("source",)

    def get_queryset(self, request):
        return (super().get_queryset(request).filter(**{f"{self.target_field}__isnull": False})
                .select_related(self.target_field))

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == self.target_field:
            kwargs["required"] = True
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class OrderLinkInline(_ObjectLinkInline):
    target_field = "order"
    fields = ("order", "status", "reason")
    readonly_fields = ()
    raw_id_fields = ("order",)
    verbose_name = "Замовлення"
    verbose_name_plural = "🛒 Замовлення проєкту"


class PurchaseLinkInline(_ObjectLinkInline):
    target_field = "purchase_order"
    fields = ("purchase_order", "status", "reason", "source")
    raw_id_fields = ("purchase_order",)
    verbose_name = "Закупівля"
    verbose_name_plural = "🏭 Закупівлі проєкту (PO) — JLC-замовлення й посилки закупівлі видно на шкалі самі"


class JLCLinkInline(_ObjectLinkInline):
    target_field = "jlc_order"
    fields = ("jlc_order", "status", "reason", "source")
    raw_id_fields = ("jlc_order",)
    verbose_name = "JLC-замовлення"
    verbose_name_plural = "🔧 JLC-замовлення проєкту (без закупівлі в Minerva)"


class ShipmentLinkInline(_ObjectLinkInline):
    target_field = "shipment"
    fields = ("shipment", "status", "reason", "source")
    raw_id_fields = ("shipment",)
    verbose_name = "Відправлення"
    verbose_name_plural = "🚚 Відправлення й посилки без замовлення (зразки) — відправлення замовлень видно самі"


class HighlightInline(admin.TabularInline):
    model = ProjectHighlight
    extra = 0
    fields = ("date", "kind", "title", "quote", "source", "created_by")
    readonly_fields = ("source", "created_by")
    formfield_overrides = {dj_models.TextField: {"widget": forms.Textarea(attrs={"rows": 2, "cols": 40})}}
    verbose_name_plural = "📌 Важливі моменти (додати — у «🧭 Пульті», у прев'ю листа або тут)"


class AttentionFilter(admin.SimpleListFilter):
    """«Потребує уваги» — відкриті проєкти з терміновим (🔴) або будь-яким важливим (🔴/🟡) пунктом огляду.
    Береться кеш стану (projects/state.py), застаріле рахується наново."""
    title = "терміновістю"          # адмінка показує «За терміновістю»
    parameter_name = "attention"

    def lookups(self, request, model_admin):
        return [("high", "🔴 Термінове"), ("any", "⚠️ Термінове або важливе")]

    def queryset(self, request, queryset):
        if self.value() not in ("high", "any"):
            return queryset
        from . import state
        states = state.states(queryset.filter(status__in=Project.OPEN_STATUSES))
        ids = [pk for pk, st in states.items() if st.high or (self.value() == "any" and st.medium)]
        return queryset.filter(pk__in=ids)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "customer", "status", "attention_col", "owner", "due_date", "threads_col",
                    "updated_at")
    list_display_links = ("code", "name")
    list_filter = (AttentionFilter, "status", "owner")
    search_fields = ("code", "name", "customer__name", "participants__email")
    autocomplete_fields = ("customer", "products")
    readonly_fields = ("code", "overview_panel", "mail_link", "timeline_html", "offers_panel", "dante_hours",
                       "source_thread_link", "created_by", "created_at", "updated_at")
    fieldsets = [
        (None, {"fields": ["code", "name", "icon", "status", "customer", "owner", "start_date", "due_date",
                           "next_action", "products"]}),
        ("🧠 AI-огляд проєкту", {"fields": ["overview_panel"]}),
        ("Опис і вимоги", {"fields": ["description"]}),
        ("Листування й хронологія", {"fields": ["mail_link", "timeline_html"]}),
        ("📝 Angebote", {"fields": ["offers_panel"]}),
        ("⏱ Час askDANTE", {"fields": ["dante_hours"]}),
        ("Службове", {"fields": ["source_thread_link", "created_by", "created_at", "updated_at"],
                      "classes": ["collapse"]}),
    ]
    inlines = [ParticipantInline, MailLinkInline, OrderLinkInline, PurchaseLinkInline, JLCLinkInline,
               ShipmentLinkInline, HighlightInline]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("customer", "owner").annotate(
            _threads=Count("links", filter=Q(links__status=ProjectLink.CONFIRMED, links__thread__isnull=False)))

    def get_changelist_instance(self, request):
        """Стан рядків сторінки — з кешу (projects/state.py), застаріле рахується лише для цих рядків."""
        cl = super().get_changelist_instance(request)
        from . import state
        self._viewer = request.user
        try:
            self._states = state.states(cl.result_list)
        except Exception:  # noqa: BLE001 — список без колонки «Увага» краще, ніж помилка сторінки
            logger.exception("project states for list")
            self._states = {}
        return cl

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        """«🧭 Пульт проєкту» над формою (projects/cockpit.py) — для наявного проєкту, на GET."""
        extra_context = extra_context or {}
        if object_id and request.method == "GET":
            obj = self.get_object(request, object_id)
            if obj is not None and self.has_view_permission(request, obj):
                from . import cockpit
                try:
                    extra_context["pj_cockpit"] = cockpit.render(obj, request.user)
                except Exception:  # noqa: BLE001 — картка без пульта краще, ніж помилка сторінки
                    logger.exception("project cockpit %s", object_id)
                    extra_context["pj_cockpit_error"] = True
        return super().changeform_view(request, object_id, form_url, extra_context)

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for obj in formset.deleted_objects:
            obj.delete()
        for obj in instances:
            if isinstance(obj, ProjectHighlight) and not obj.created_by_id:
                obj.created_by = request.user
            if isinstance(obj, ProjectLink) and not obj.pk:
                obj.created_by = request.user
            obj.save()
        formset.save_m2m()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if "products" in form.changed_data:          # нові товари проєкту → закупівлі й JLC з ними — пропозиції
            n = services.scan(form.instance)
            if n:
                messages.info(request, f"🔎 Minerva знайшла закупівлі чи JLC-замовлення з товарами проєкту: {n}. "
                                       f"Перевірте пропозиції у «🧭 Пульті» (✓ / ✗).")

    def get_object(self, request, object_id, from_field=None):
        from worktime.panels import remember_viewer   # «⏱ Час askDANTE»: свій опис, вартість — від того, хто дивиться
        return remember_viewer(super().get_object(request, object_id, from_field), request.user)

    def get_fieldsets(self, request, obj=None):
        """«⏱ Час askDANTE» — лише з розділом «Робочий час» (worktime.panels.can_see)."""
        from worktime.panels import can_see
        fieldsets = super().get_fieldsets(request, obj)
        if can_see(request.user):
            return fieldsets
        return [(name, options) for name, options in fieldsets if "dante_hours" not in options.get("fields", ())]

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
            obj.owner = obj.owner or request.user
        super().save_model(request, obj, form, change)

    @admin.display(description="Гілок", ordering="_threads")
    def threads_col(self, obj):
        return obj._threads or "—"

    @admin.display(description="Увага")
    def attention_col(self, obj):
        """🔴 термінове / 🟡 важливе з «Потребує уваги» (без AI, кеш стану); підказка — самі пункти,
        тема чужого листа — без тексту."""
        if not obj.is_open:
            return "—"
        from . import state
        st = getattr(self, "_states", {}).get(obj.pk)
        if st is None:
            st = state.states([obj])[obj.pk]
        items = state.items_for(st, getattr(self, "_viewer", None))
        if not items:
            return "✅"
        high = sum(1 for a in items if a["level"] == "high")
        label = " ".join(x for x in [f"🔴 {high}" if high else "", f"🟡 {len(items) - high}" if len(items) > high else ""] if x)
        return format_html('<a href="{}#pj-cockpit" title="{}">{}</a>',
                           reverse("admin:projects_project_change", args=[obj.pk]),
                           "\n".join(f"{a['icon']} {a['text']}" for a in items), label)

    @admin.display(description="Стан")
    def overview_panel(self, obj):
        """Останній AI-огляд: обіцянки, виконано, питання, наступна дія («Потребує уваги» — у «🧭 Пульті» вгорі)."""
        if not obj or not obj.pk:
            return "Огляд з'явиться після збереження проєкту."
        from . import overview
        facts = overview.collect(obj)
        saved, stale = overview.saved(obj, facts)
        return render_to_string("admin/projects/overview_panel.html", {
            "counts": facts["counts"], "ov": saved, "stale": stale,
            "d": overview.for_display(saved.data) if saved else {},
            "generate_url": reverse("projects:overview", args=[obj.pk]),
            "next_action_url": reverse("projects:overview_next_action", args=[obj.pk]),
            "project_id": obj.pk,   # «❓ Запитати про проєкт» (rag_assistant/entity_help.py)
        })

    @admin.display(description="Пошта")
    def mail_link(self, obj):
        if not obj or not obj.pk:
            return "—"
        n = services.project_messages(obj).count()
        return format_html('<a class="button" href="/email/?project={}" target="_blank">📧 Листи проєкту в пошті ({})</a>'
                           ' <span class="help" style="margin-left:8px">Підказка: код <b>{}</b> у темі листа '
                           'прив\'язує лист до проєкту автоматично.</span>', obj.pk, n, obj.code)

    @admin.display(description="Хронологія")
    def timeline_html(self, obj):
        if not obj or not obj.pk:
            return "—"
        from worktime.panels import viewer_of   # хто дивиться (get_object → remember_viewer)
        events = services.timeline(obj, limit=150, viewer=viewer_of(obj))
        if not events:
            return "Поки порожньо — додайте листування з пошти («📁 До проєкту») або на сторінці «Розбір пошти»."
        rows = format_html_join(
            "", '<tr><td style="white-space:nowrap;color:var(--body-quiet-color)">{}</td><td>{}</td>'
                '<td>{}</td><td style="color:var(--body-quiet-color)">{}</td></tr>',
            ((timezone.localtime(e["date"]).strftime("%d.%m.%Y %H:%M") if e["date"] else "—", e["icon"],
              format_html('<a href="{}" target="_blank">{}</a>', e["url"], e["title"]) if e["url"] else e["title"],
              e["who"]) for e in events))
        return format_html('<table style="width:100%">{}</table>', rows)

    @admin.display(description="Пропозиції")
    def offers_panel(self, obj):
        if not obj or not obj.pk:
            return "—"
        try:
            offers = list(obj.offers.order_by("-created_at"))
        except AttributeError:
            return "—"
        rows = format_html_join("", '<tr><td style="padding:1px 12px 1px 0"><a href="{}">{}</a></td><td>{}</td>'
                                    '<td style="text-align:right;padding-left:12px">{} {}</td></tr>',
                                ((reverse("admin:offers_offer_change", args=[o.pk]), o.display_number,
                                  o.get_status_display(), f"{o.gross_total:,.2f}".replace(",", " "), o.currency)
                                 for o in offers))
        add = reverse("admin:offers_offer_add") + f"?project={obj.pk}" + \
            (f"&customer={obj.customer_id}" if obj.customer_id else "")
        return format_html('<table>{}</table><a class="button" href="{}">📝 Новий Angebot</a>', rows, add)

    @admin.display(description="Години")
    def dante_hours(self, obj):
        if not obj or not obj.pk:
            return "—"
        from worktime.panels import project_panel, viewer_of   # askDANTE: лише читає копію записів
        return project_panel(obj, viewer_of(obj)) or "—"

    @admin.display(description="Створено з листування")
    def source_thread_link(self, obj):
        if obj and obj.source_thread_id:
            return format_html('<a href="/email/thread/{}/" target="_blank">✉️ {}</a>',
                               obj.source_thread_id, obj.source_thread.subject or "(без теми)")
        return "—"

    # ── «Розбір пошти» ──────────────────────────────────────────────────────
    def get_urls(self):
        return [path("triage/", self.admin_site.admin_view(self.triage_view), name="projects_project_triage")] \
            + super().get_urls()

    def triage_view(self, request):
        from email_assistant.models import EmailThread
        from projects.triage import build_groups, user_accounts

        days = request.GET.get("days") or request.POST.get("days") or "90"
        days = int(days) if str(days).isdigit() and 1 <= int(days) <= 3650 else 90
        accounts = user_accounts(request.user)

        if request.method == "POST":
            ids = [int(i) for i in request.POST.getlist("thread") if str(i).isdigit()]
            threads = list(EmailThread.objects.filter(pk__in=ids, account__in=accounts))
            action = request.POST.get("action")
            if not threads:
                messages.warning(request, "Не вибрано жодної гілки.")
            elif action == "create":
                from crm.models import Customer
                cid = request.POST.get("customer_id")
                customer = Customer.objects.filter(pk=cid).first() if str(cid).isdigit() else None
                project = services.create_project(name=request.POST.get("name", ""), user=request.user,
                                                  customer=customer, threads=threads)
                url = reverse("admin:projects_project_change", args=[project.pk])
                messages.success(request, format_html('Створено проєкт <a href="{}">{}</a> з {} гілок.',
                                                      url, project, len(threads)))
            elif action == "add":
                project = Project.objects.filter(pk=request.POST.get("project_id") or 0).first()
                if project is None:
                    messages.warning(request, "Виберіть проєкт.")
                else:
                    for t in threads:
                        services.link_thread(project, t, user=request.user, reason="додано в «Розборі пошти»")
                    messages.success(request, f"Додано {len(threads)} гілок до {project}.")
            elif action == "dismiss":
                n = services.dismiss_threads(threads, user=request.user)
                messages.info(request, f"Позначено «не стосується проєктів»: {n}. Скасувати — «Гілки поза проєктами».")
            return redirect(f"{reverse('admin:projects_project_triage')}?days={days}")

        groups, total = build_groups(accounts, days=days, q=request.GET.get("q", "").strip())
        ctx = {
            **self.admin_site.each_context(request),
            "title": "📥 Розбір пошти → проєкти",
            "groups": groups, "total": total, "days": days, "q": request.GET.get("q", ""),
            "day_choices": [30, 90, 180, 365],
            "open_projects": Project.objects.filter(status__in=Project.OPEN_STATUSES).order_by("-updated_at")[:300],
            "has_accounts": bool(accounts),
            "opts": self.model._meta,
        }
        return render(request, "admin/projects/triage.html", ctx)


@admin.register(DismissedThread)
class DismissedThreadAdmin(admin.ModelAdmin):
    list_display = ("thread", "dismissed_by", "dismissed_at")
    search_fields = ("thread__subject",)
    readonly_fields = ("thread", "dismissed_by", "dismissed_at")

    def has_add_permission(self, request):
        return False
