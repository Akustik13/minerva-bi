from django.contrib import admin, messages
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from . import services
from .models import DismissedThread, Project, ProjectLink, ProjectParticipant


class ParticipantInline(admin.TabularInline):
    model = ProjectParticipant
    extra = 0
    fields = ("email", "name", "role", "customer")
    autocomplete_fields = ("customer",)
    verbose_name_plural = "👥 Учасники (з листування — автоматично; роль можна виправити)"


class MailLinkInline(admin.TabularInline):
    """Гілки й листи: статус можна підтвердити / відхилити; додаються з пошти або «Розбору пошти»."""
    model = ProjectLink
    fk_name = "project"
    extra = 0
    fields = ("target", "status", "source", "reason", "created_at")
    readonly_fields = ("target", "source", "reason", "created_at")
    verbose_name_plural = "✉️ Листування (додати — у пошті: «📁 До проєкту» або «Розбір пошти»)"

    def get_queryset(self, request):
        return super().get_queryset(request).filter(order__isnull=True).select_related("thread", "message")

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Гілка / лист")
    def target(self, obj):
        if obj.thread_id:
            return format_html('<a href="/email/thread/{}/" target="_blank">✉️ {}</a>',
                               obj.thread_id, obj.thread.subject or "(без теми)")
        if obj.message_id:
            return format_html('<a href="/email/message/{}/" target="_blank">📨 {}</a>',
                               obj.message_id, obj.message.subject or "(без теми)")
        return "—"


class OrderLinkInline(admin.TabularInline):
    model = ProjectLink
    fk_name = "project"
    extra = 0
    fields = ("order", "status", "reason")
    raw_id_fields = ("order",)
    verbose_name = "Замовлення"
    verbose_name_plural = "🛒 Замовлення проєкту"

    def get_queryset(self, request):
        return super().get_queryset(request).filter(order__isnull=False).select_related("order")


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "customer", "status", "owner", "due_date", "threads_col", "updated_at")
    list_display_links = ("code", "name")
    list_filter = ("status", "owner")
    search_fields = ("code", "name", "customer__name", "participants__email")
    autocomplete_fields = ("customer",)
    readonly_fields = ("code", "mail_link", "timeline_html", "offers_panel", "source_thread_link", "created_by",
                       "created_at", "updated_at")
    fieldsets = [
        (None, {"fields": ["code", "name", "status", "customer", "owner", "due_date", "next_action"]}),
        ("Опис і вимоги", {"fields": ["description"]}),
        ("Листування й хронологія", {"fields": ["mail_link", "timeline_html"]}),
        ("📝 Angebote", {"fields": ["offers_panel"]}),
        ("Службове", {"fields": ["source_thread_link", "created_by", "created_at", "updated_at"],
                      "classes": ["collapse"]}),
    ]
    inlines = [ParticipantInline, MailLinkInline, OrderLinkInline]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("customer", "owner").annotate(
            _threads=Count("links", filter=Q(links__status=ProjectLink.CONFIRMED, links__thread__isnull=False)))

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
            obj.owner = obj.owner or request.user
        super().save_model(request, obj, form, change)

    @admin.display(description="Гілок", ordering="_threads")
    def threads_col(self, obj):
        return obj._threads or "—"

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
        events = services.timeline(obj, limit=150)
        if not events:
            return "Поки порожньо — додайте листування з пошти («📁 До проєкту») або на сторінці «Розбір пошти»."
        rows = format_html_join(
            "", '<tr><td style="white-space:nowrap;color:var(--body-quiet-color)">{}</td><td>{}</td>'
                '<td><a href="{}" target="_blank">{}</a></td><td style="color:var(--body-quiet-color)">{}</td></tr>',
            ((timezone.localtime(e["date"]).strftime("%d.%m.%Y %H:%M") if e["date"] else "—",
              e["icon"], e["url"], e["title"], e["who"]) for e in events))
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
