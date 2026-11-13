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
from .models import (DismissedThread, Project, ProjectHighlight, ProjectLink, ProjectParticipant, ProjectRisk,
                     ProjectSettings, ProjectTemplate)

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
                           "ai_mail_since", "next_action", "products"]}),
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

    # ── «Розбір пошти», «📊 Портфель» ───────────────────────────────────────
    def get_urls(self):
        return [path("triage/", self.admin_site.admin_view(self.triage_view), name="projects_project_triage"),
                path("portfolio/", self.admin_site.admin_view(self.portfolio_view),
                     name="projects_project_portfolio"),
                path("<path:object_id>/workspace/", self.admin_site.admin_view(self.workspace_view),
                     name="projects_project_workspace")] + super().get_urls()

    def workspace_view(self, request, object_id):
        """🧭 Робочий стіл проєкту: той самий пульт, що в картці (projects/cockpit.py), на всю ширину й без полів
        картки — план і діаграми Ганта, шкала, ризики, статус, AI. Право — перегляд проєкту; змінює — хто може."""
        from django.core.exceptions import PermissionDenied
        from django.http import Http404
        from django.template.response import TemplateResponse
        from . import cockpit
        obj = self.get_object(request, object_id)
        if obj is None:
            raise Http404
        if not self.has_view_permission(request, obj):
            raise PermissionDenied
        html = ""
        try:
            html = cockpit.render(obj, request.user)
        except Exception:  # noqa: BLE001 — сторінка з поясненням краще, ніж помилка
            logger.exception("project workspace %s", object_id)
        customer = obj.customer if obj.customer_id else None
        ctx = {**self.admin_site.each_context(request), "title": f"🧭 {obj.code} · {obj.name}", "opts": self.model._meta,
               "project": obj, "pj_cockpit": html,
               "customer": (customer.company or customer.name or customer.email) if customer else "",
               "owner": (obj.owner.get_full_name() or obj.owner.username) if obj.owner_id else ""}
        return TemplateResponse(request, "admin/projects/project/workspace.html", ctx)

    def portfolio_view(self, request):
        """📊 Портфель: таблиця з міні-шкалами, шкала всіх проєктів, дошка фаз, карта зв'язків і «⚡ Впливи»
        (projects/portfolio.py, impact.py, portfolio_draw.py) — лише читає; право — перегляд проєктів."""
        from django.core.exceptions import PermissionDenied
        from . import impact, portfolio, portfolio_draw as draw
        if not self.has_view_permission(request):
            raise PermissionDenied
        today = timezone.localdate()
        params = portfolio.Params.parse(request.GET)
        rows, counts, _all = portfolio.select(params, request.user, today)
        groups = portfolio.grouped(rows, params.group)
        shown = {r.project.pk for r in rows}
        try:
            impacts = [i for i in impact.compute(today) if i.project_ids & shown]
        except Exception:  # noqa: BLE001 — портфель без «Впливів» краще, ніж помилка сторінки
            logger.exception("project impacts")
            impacts = []
        rng = portfolio.span(rows, today)
        ctx = {
            **self.admin_site.each_context(request),
            "title": "📊 Портфель проєктів", "opts": self.model._meta, "params": params, "counts": counts,
            "rows": rows, "impacts": impacts, "today": today, "limit": portfolio.LIMIT,
            "views": [(key, label, params.query(view=key), key == params.view)
                      for key, label in (("table", "📋 Таблиця"), ("timeline", "🗓 Шкала"),
                                         ("board", "🗂 Дошка фаз"), ("map", "🔗 Карта зв’язків"))],
            "groups_menu": [(label, params.query(group=key), key == params.group)
                            for key, label in portfolio.GROUPS.items()],
            "filters": [(label, n, params.query(f=f, h=h), (params.mine, params.health) == (f == "mine", h))
                        for label, n, f, h in (("Усі", counts["all"], "", ""), ("Мої", counts["mine"], "mine", ""),
                                               ("🔴", counts["bad"], "", "bad"), ("🟡", counts["warn"], "", "warn"),
                                               ("🟢", counts["ok"], "", "ok"),
                                               ("⚪ без плану", counts["noplan"], "", "noplan"))],
            "closed_url": params.query(closed="" if params.closed else "1"),
        }
        if params.view == "table":
            ctx["groups"] = [(title, [(r, draw.mini(r, rng, today)) for r in items]) for title, items in groups]
            ctx["axis"] = draw.axis_head(rng, 300, 2, 298)
        elif params.view == "timeline":
            ctx["svg"] = draw.timeline(groups, rng, today, portfolio.cross_deps(rows))
        elif params.view == "board":
            ctx["columns"] = portfolio.board(rows)
        else:
            rel = portfolio.relations(rows, impacts)
            ctx["svg"] = draw.relation_map(rel)
            ctx["map_info"] = rel["info"]
        return render(request, "admin/projects/portfolio.html", ctx)

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


class ProjectTemplateForm(forms.ModelForm):
    class Meta:
        model = ProjectTemplate
        fields = ("name", "description", "is_active", "order", "plan")

    def clean_plan(self):
        """Те саме, що читає plan.apply_template: [{name, icon, gate, steps: [{title, days, side, kind, trigger, after}]}]."""
        from . import schedule
        from .models import ProjectStep
        data = self.cleaned_data.get("plan")
        if not isinstance(data, list) or not data:
            raise forms.ValidationError('Потрібен список фаз: [{"name": "…", "steps": [{"title": "…", "days": 3}]}]')
        sides, total = dict(ProjectStep.SIDE_CHOICES), 0
        for i, ph in enumerate(data, 1):
            if not isinstance(ph, dict) or not str(ph.get("name") or "").strip():
                raise forms.ValidationError(f"Фаза {i}: потрібна назва (name)")
            steps = ph.get("steps") or []
            if not isinstance(steps, list):
                raise forms.ValidationError(f"Фаза {i}: steps — список кроків")
            for j, st in enumerate(steps, 1):
                where = f"Фаза {i}, крок {j}"
                if not isinstance(st, dict) or not str(st.get("title") or "").strip():
                    raise forms.ValidationError(f"{where}: потрібна назва (title)")
                if st.get("side") and st["side"] not in sides:
                    raise forms.ValidationError(f"{where}: side — одне з {', '.join(sides)}")
                if st.get("trigger") and st["trigger"] not in schedule.TRIGGER_TYPES:
                    raise forms.ValidationError(f"{where}: невідома умова trigger «{st['trigger']}»")
                try:
                    int(st.get("days") or 0)
                except (TypeError, ValueError):
                    raise forms.ValidationError(f"{where}: days — ціле число днів")
                total += 1
        if not total:
            raise forms.ValidationError("У шаблоні немає жодного кроку")
        return data


@admin.register(ProjectTemplate)
class ProjectTemplateAdmin(admin.ModelAdmin):
    """🧩 Шаблони планів: вбудовані (plan.ensure_builtin) можна сховати, не видалити; свої — «💾 Зберегти як шаблон»."""
    form = ProjectTemplateForm
    list_display = ("name", "counts_col", "is_active", "order", "builtin_col", "created_by", "created_at")
    list_editable = ("is_active", "order")
    list_filter = ("is_active", "builtin")
    search_fields = ("name", "description")
    readonly_fields = ("preview", "builtin", "created_by", "created_at")
    fieldsets = (
        (None, {"fields": ("name", "description", "is_active", "order", "preview")}),
        ("Для досвідчених: фази й кроки в JSON", {
            "classes": ("collapse",),
            "description": "Фаза: name, icon, gate, steps. Крок: title, days, side (we / customer / supplier / "
                           "colleague), kind (task / milestone), trigger, after (null — після попереднього, "
                           "\"parallel\" — разом з попереднім, [0, 2] — після цих кроків шаблону за номером від 0).",
            "fields": ("plan",)}),
        ("Службове", {"classes": ("collapse",), "fields": ("builtin", "created_by", "created_at")}),
    )

    def changelist_view(self, request, extra_context=None):
        from . import plan
        plan.ensure_builtin()
        return super().changelist_view(request, extra_context)

    @admin.display(description="Фаз / кроків")
    def counts_col(self, obj):
        from . import plan
        phases, steps = plan.template_counts(obj)
        return f"{phases} / {steps}"

    @admin.display(description="Вбудований", boolean=True, ordering="builtin")
    def builtin_col(self, obj):
        return obj.builtin

    @admin.display(description="Що в шаблоні")
    def preview(self, obj):
        from .models import TRIGGERS, ProjectStep
        if not obj or not obj.plan:
            return "—"
        sides, triggers = dict(ProjectStep.SIDE_CHOICES), dict(TRIGGERS)
        blocks = []
        for ph in obj.plan:
            if not isinstance(ph, dict):
                continue
            rows = format_html_join("", "<li>{}{} · {} · {}{}</li>", (
                ("◆ " if st.get("kind") == ProjectStep.MILESTONE else "", st.get("title", ""),
                 sides.get(st.get("side") or ProjectStep.WE, ""),
                 "віха" if st.get("kind") == ProjectStep.MILESTONE else f"{st.get('days') or 1} дн.",
                 f" · 🔗 {triggers[st['trigger']]}" if st.get("trigger") in triggers and st.get("trigger") else "")
                for st in ph.get("steps") or [] if isinstance(st, dict)))
            gate = format_html(' <span style="opacity:.75">· умова переходу: {}</span>', ph["gate"]) \
                if ph.get("gate") else ""
            blocks.append(format_html('<div style="margin-bottom:8px"><b>{} {}</b>{}<ol style="margin:4px 0 0 18px">'
                                      '{}</ol></div>', ph.get("icon", ""), ph.get("name", ""), gate, rows))
        return format_html_join("", "{}", ((b,) for b in blocks))

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.builtin:
            return False            # вбудований повернувся б сам — його можна лише сховати
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)      # серед вибраних можуть бути вбудовані — видаляти по одному з картки
        return actions

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


# ── ⚠️ Реєстр ризиків усіх проєктів, ⚙️ налаштування проєктів ─────────────────────────────

class RiskStateFilter(admin.SimpleListFilter):
    """Стан ризику; без вибору — відкриті й ті, за якими стежимо (закриті — окремо)."""
    title = "станом"
    parameter_name = "state"
    LIVE = "live"

    def lookups(self, request, model_admin):
        return [(self.LIVE, "⚠️ Відкриті й «стежимо»"), ("open", "⚠️ Відкриті"), ("watch", "👀 Стежимо"),
                ("closed", "✓ Закриті"), ("all", "Усі")]

    def value(self):
        return super().value() or self.LIVE

    def choices(self, changelist):
        for key, label in self.lookup_choices:
            yield {"selected": self.value() == key, "display": label,
                   "query_string": changelist.get_query_string({self.parameter_name: key})}

    def queryset(self, request, queryset):
        from .models import ProjectRisk
        v = self.value()
        if v == self.LIVE:
            return queryset.exclude(status=ProjectRisk.CLOSED)
        if v in dict(ProjectRisk.STATUS_CHOICES):
            return queryset.filter(status=v)
        return queryset


class RiskLevelFilter(admin.SimpleListFilter):
    title = "балом"
    parameter_name = "level"

    def lookups(self, request, model_admin):
        return [("red", "🔴 від 6"), ("yellow", "🟡 3–4"), ("green", "🟢 до 2")]

    def queryset(self, request, queryset):
        from django.db.models import F
        qs = queryset.annotate(_score=F("probability") * F("impact"))
        return {"red": qs.filter(_score__gte=6), "yellow": qs.filter(_score__gte=3, _score__lt=6),
                "green": qs.filter(_score__lt=3)}.get(self.value(), queryset)


@admin.register(ProjectRisk)
class ProjectRiskAdmin(admin.ModelAdmin):
    """⚠️ Ризики всіх проєктів: матриця 3 × 3 над списком (за поточними фільтрами), реєстр — власник, заходи, дата
    перегляду. Ризики правил (projects/risk_rules.py) видаляти не можна — лише закрити; назву й підставу пише правило."""
    change_list_template = "admin/projects/projectrisk/change_list.html"
    list_display = ("number", "score_col", "title_col", "project_col", "source_col", "owner", "review_col", "status")
    list_display_links = ("number", "title_col")
    list_filter = (RiskStateFilter, RiskLevelFilter, "source", "category", "owner")
    search_fields = ("title", "mitigation", "description", "project__code", "project__name")
    list_select_related = ("project", "owner")
    autocomplete_fields = ("project",)
    actions = ["close_selected", "watch_selected", "reopen_selected"]
    fieldsets = (
        (None, {"fields": ("project", "title", "description", ("category", "status"), ("probability", "impact"),
                           "owner", "mitigation", "review_on", "steps")}),
        ("Звідки", {"fields": ("source", "rule_key", "evidence", "message", "opened_at", "closed_at", "closed_auto",
                               "edited", "created_by", "updated_at")}),
    )
    readonly_fields = ("source", "rule_key", "evidence", "message", "opened_at", "closed_at", "closed_auto", "edited",
                       "created_by", "updated_at")

    def get_queryset(self, request):
        from django.db.models import F
        return super().get_queryset(request).annotate(_score=F("probability") * F("impact"))

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.source == ProjectRisk.RULE:
            ro += ["project", "title", "description"]
        return ro

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        steps = form.base_fields.get("steps")
        if steps is not None:
            from .models import ProjectStep
            steps.queryset = (ProjectStep.objects.filter(project=obj.project) if obj is not None
                              else ProjectStep.objects.none())
            steps.help_text = "Кроки цього проєкту (з’являться після збереження)" if obj is None else ""
        return form

    def save_model(self, request, obj, form, change):
        from . import risks
        if change:
            old = ProjectRisk.objects.get(pk=obj.pk)
            if obj.source == ProjectRisk.RULE and (old.probability, old.impact) != (obj.probability, obj.impact):
                obj.edited = True
            new_status, obj.status = obj.status, old.status
            risks.set_status(obj, new_status)
        else:
            obj.created_by = request.user
            obj.source = ProjectRisk.MANUAL
            obj.owner = obj.owner or obj.project.owner
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.source == ProjectRisk.RULE:
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def changelist_view(self, request, extra_context=None):
        response = super().changelist_view(request, extra_context)
        try:
            cl = response.context_data["cl"]
        except (AttributeError, KeyError, TypeError):
            return response
        from . import risk_rules
        risks = sorted(cl.queryset.select_related("project")[:500], key=lambda r: (-r.score, r.pk))
        response.context_data["risk_matrix"] = risk_rules.matrix(risks)
        response.context_data["risk_numbers"] = {r.pk: n for n, r in enumerate(risks, 1)}
        return response

    def _set_status(self, request, queryset, status, text):
        from . import risks
        n = 0
        for r in queryset:
            risks.set_status(r, status)
            r.save()
            n += 1
        self.message_user(request, f"{text}: {n}")

    @admin.action(description="✓ Закрити вибрані", permissions=["change"])
    def close_selected(self, request, queryset):
        self._set_status(request, queryset, ProjectRisk.CLOSED, "Закрито")

    @admin.action(description="👀 Стежимо", permissions=["change"])
    def watch_selected(self, request, queryset):
        self._set_status(request, queryset, ProjectRisk.WATCH, "Стежимо")

    @admin.action(description="⚠️ Відкрити знову", permissions=["change"])
    def reopen_selected(self, request, queryset):
        self._set_status(request, queryset, ProjectRisk.OPEN, "Відкрито знову")

    @admin.display(description="№", ordering="pk")
    def number(self, obj):
        return f"#{obj.pk}"

    @admin.display(description="Бал", ordering="_score")
    def score_col(self, obj):
        return format_html('<span class="pjr-score pjr-{}" title="ймовірність {} × вплив {}">{}</span>', obj.level,
                           obj.probability, obj.impact, obj.score)

    @admin.display(description="Ризик", ordering="title")
    def title_col(self, obj):
        mitig = format_html('<div style="opacity:.75;font-size:12px">➡️ {}</div>', obj.mitigation[:140]) \
            if obj.mitigation else ""
        return format_html('<span translate="no">{}</span>{}', obj.title, mitig)

    @admin.display(description="Проєкт", ordering="project__code")
    def project_col(self, obj):
        return format_html('<a href="{}#pj-risks" translate="no">{}</a>',
                           reverse("admin:projects_project_change", args=[obj.project_id]), obj.project.code)

    @admin.display(description="Звідки", ordering="source")
    def source_col(self, obj):
        from .risk_rules import rule_name
        rule = rule_name(obj.rule_key)
        return f"{obj.get_source_display()}" + (f" «{rule}»" if rule else "")

    @admin.display(description="Переглянути", ordering="review_on")
    def review_col(self, obj):
        if not obj.review_on:
            return "—"
        late = obj.is_open and obj.review_on < timezone.localdate()
        return format_html('<span style="{}">{}</span>', "color:var(--err,#f85149);font-weight:600" if late else "",
                           obj.review_on.strftime("%d.%m.%Y"))


@admin.register(ProjectSettings)
class ProjectSettingsAdmin(admin.ModelAdmin):
    """⚙️ Налаштування проєктів (один запис): тижневий звіт відповідальним (projects/weekly.py), історія кольорів,
    нагадування про статус. «▶ Надіслати зараз» / «📨 Лише мені» — POST, право змінювати налаштування."""
    change_form_template = "admin/projects/projectsettings/change_form.html"
    fieldsets = (
        ("📨 Щотижневий звіт відповідальним", {
            "fields": ("weekly_enabled", ("weekday", "hour"), ("send_email", "send_telegram")),
            "description": "Кожному відповідальному — його відкриті проєкти: колір і причина, фаза, прогноз проти "
                           "строку клієнту, нові й закриті ризики, коли оновлювали статус, кроки наступного тижня. "
                           "Лист — через SMTP «Налаштування → Сповіщення» (не задано — скринька людини), Telegram — "
                           "якщо в профілі є Telegram ID."}),
        ("📝 Тижневий статус і історія", {"fields": ("auto_snapshot", "stale_days")}),
        ("Стан", {"fields": ("last_run_at", "last_result")}),
    )
    readonly_fields = ("last_run_at", "last_result")

    def has_add_permission(self, request):
        return not ProjectSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = ProjectSettings.get()
        return redirect(reverse("admin:projects_projectsettings_change", args=[obj.pk]))

    def get_urls(self):
        return [path("send-now/", self.admin_site.admin_view(self.send_now_view), name="projects_projectsettings_send")] \
            + super().get_urls()

    def send_now_view(self, request):
        """▶ Надіслати звіт зараз усім (who=all) або лише собі (who=me) — незалежно від розкладу."""
        from . import weekly
        back = reverse("admin:projects_projectsettings_change", args=[ProjectSettings.get().pk])
        if request.method != "POST" or not self.has_change_permission(request):
            messages.error(request, "Немає права надсилати звіт")
            return redirect(back)
        if request.POST.get("who") == "me":
            out = weekly.run(only=request.user)
            if not out["people"]:
                messages.info(request, "У вас немає відкритих проєктів, де ви відповідальний — звіту немає.")
            else:
                messages.success(request, f"📨 Звіт надіслано собі: {out['result']}")
        else:
            out = weekly.run()
            messages.success(request, f"📨 {out['result']}")
        return redirect(back)


@admin.register(DismissedThread)
class DismissedThreadAdmin(admin.ModelAdmin):
    list_display = ("thread", "dismissed_by", "dismissed_at")
    search_fields = ("thread__subject",)
    readonly_fields = ("thread", "dismissed_by", "dismissed_at")

    def has_add_permission(self, request):
        return False
