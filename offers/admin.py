from decimal import Decimal

from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from . import services
from .models import Offer, OfferLine, OfferRevision, OfferSettings, QuoteRequest


class OfferLineInline(admin.TabularInline):
    model = OfferLine
    extra = 1
    fields = ("position", "product", "sku", "description", "qty", "unit", "unit_price", "discount_percent",
              "lead_time", "line_total", "price_info")
    readonly_fields = ("price_info", "line_total")
    autocomplete_fields = ("product",)
    verbose_name_plural = "Позиції (ціна порожня — з каталогу Minerva: магазин → ціна продажу → базова ціна)"

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        field = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == "description":
            field.widget = forms.Textarea(attrs={"rows": 2, "cols": 34})
        if db_field.name in ("qty", "unit_price", "discount_percent"):
            field.widget.attrs["style"] = "width:90px"
        if db_field.name in ("position",):
            field.widget.attrs["style"] = "width:40px"
        if db_field.name in ("unit",):
            field.widget.attrs["style"] = "width:50px"
        return field

    @admin.display(description="Ціна з / маржа")
    def price_info(self, obj):
        """Звідки ціна (повне пояснення — у підказці) і маржа — лише для вас, у документ не потрапляє."""
        if not obj or not obj.pk:
            return "—"
        if obj.unit_price is None:
            return format_html('<span style="color:#e57373" title="{}">⚠ на запит</span>', obj.price_note)
        return format_html('<span title="{}">{}</span><br><span style="opacity:.85">маржа {}</span>',
                           obj.price_note, obj.get_price_source_display(), self.margin(obj))

    def margin(self, obj):
        if not obj or not obj.product_id or obj.unit_price is None or not obj.qty:
            return "—"
        purchase = obj.product.purchase_price
        if not purchase:
            return "—"
        net_unit = obj.line_total / obj.qty
        if not net_unit:
            return "—"
        pct = ((net_unit - Decimal(purchase)) / net_unit * 100).quantize(Decimal("0.1"))
        color = "#e57373" if pct < 10 else ("#d97706" if pct < 25 else "inherit")
        return format_html('<span style="color:{}">{} %</span>', color, pct)

    def has_add_permission(self, request, obj=None):
        return not (obj and obj.is_locked)

    def has_change_permission(self, request, obj=None):
        return not (obj and obj.is_locked)

    def has_delete_permission(self, request, obj=None):
        return not (obj and obj.is_locked)


@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    list_display = ("number_col", "customer", "project", "status_col", "gross_col", "valid_until", "owner",
                    "updated_at")
    list_filter = ("status", "owner", "currency")
    search_fields = ("number", "customer__name", "customer__company", "contact_email", "lines__sku")
    autocomplete_fields = ("customer", "project")
    readonly_fields = ("number_rev", "totals_panel", "actions_panel", "documents_panel", "sources_panel")
    inlines = [OfferLineInline]
    fieldsets = [
        (None, {"fields": ["number_rev", "actions_panel", "status", "customer", "contact_name", "contact_email",
                           "billing_address", "project", "owner"]}),
        ("Умови", {"fields": ["language", "currency", "vat_rate", "shipping_cost", "valid_until"]}),
        ("Тексти документа", {"fields": ["intro_text", "payment_terms", "delivery_terms", "outro_text"],
                              "classes": ["collapse"]}),
        ("Підсумок", {"fields": ["totals_panel"]}),
        ("Документи й джерела", {"fields": ["documents_panel", "sources_panel", "internal_notes"]}),
    ]

    class Media:
        css = {"all": []}

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("customer", "project", "owner")

    def changelist_view(self, request, extra_context=None):
        services.expire_overdue()
        return super().changelist_view(request, extra_context)

    def get_changeform_initial_data(self, request):
        """Нова пропозиція: умови й тексти з налаштувань; ?customer=&project= — з CRM / проєкту."""
        from crm.models import Customer
        initial = super().get_changeform_initial_data(request)
        customer = Customer.objects.filter(pk=initial.get("customer") or 0).first()
        if customer is None and initial.get("project"):
            from projects.models import Project
            project = Project.objects.filter(pk=initial["project"]).select_related("customer").first()
            customer = project.customer if project else None
            if customer:
                initial["customer"] = customer.pk
        d = services.defaults(customer)
        d["owner"] = request.user.pk
        return {**d, **initial}

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        if obj and obj.is_locked:   # надіслана версія незмінна — лише статус і нотатки
            ro += [f.name for f in Offer._meta.fields if f.name not in ("status", "internal_notes", "id")]
        return ro

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
            obj.owner = obj.owner or request.user
        if change and "status" in form.changed_data:
            old = Offer.objects.get(pk=obj.pk).status
            if obj.status in (Offer.SENT, Offer.ACCEPTED) and old not in Offer.LOCKED:
                # «Надіслано» / «Прийнято» — лише через дії (документ, лист, замовлення)
                obj.status = old
                messages.warning(request, "Статус «Надіслано» ставиться після відправки листа, «Прийнято» — "
                                          "кнопкою «Прийнято → замовлення».")
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for obj in formset.deleted_objects:
            obj.delete()
        for line in instances:
            line.offer = form.instance
            services.fill_line(line)
            line.save()
        formset.save_m2m()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        services.recalculate(form.instance)

    # ── колонки ──
    @admin.display(description="Номер", ordering="number")
    def number_col(self, obj):
        return obj.display_number

    @admin.display(description="Статус", ordering="status")
    def status_col(self, obj):
        return obj.get_status_display()

    @admin.display(description="Сума брутто", ordering="gross_total")
    def gross_col(self, obj):
        return f"{obj.gross_total:,.2f} {obj.currency}".replace(",", " ")

    @admin.display(description="Номер / версія")
    def number_rev(self, obj):
        if not obj or not obj.pk:
            return "видається при збереженні"
        lock = " · 🔒 надіслана версія незмінна — для змін «Нова версія»" if obj.is_locked else ""
        return f"{obj.number} · версія {obj.revision}{lock}"

    @admin.display(description="Підсумок")
    def totals_panel(self, obj):
        if not obj or not obj.pk:
            return "—"
        rows = [("Нетто (з доставкою)", obj.net_total), (f"ПДВ {obj.vat_rate} %", obj.vat_total),
                ("Брутто", obj.gross_total)]
        table = format_html_join("", "<tr><td style='padding:1px 14px 1px 0'>{}</td><td style='text-align:right'>"
                                     "<b>{}</b> {}</td></tr>", ((k, f"{v:,.2f}".replace(",", " "), obj.currency)
                                                                 for k, v in rows))
        warns = services.warnings(obj)
        warn = format_html_join("", '<div style="color:#d97706">⚠ {}</div>', ((w,) for w in warns))
        return format_html("<table>{}</table>{}<div class='help'>Суми рахує Minerva (Decimal); "
                           "маржа в позиціях — лише для вас.</div>", table, warn)

    @admin.display(description="Дії")
    def actions_panel(self, obj):
        if not obj or not obj.pk:
            return "Спершу збережіть пропозицію."
        u = lambda name: reverse(f"admin:offers_offer_{name}", args=[obj.pk])  # noqa: E731
        btn = '<button type="submit" class="button" formaction="{}" formnovalidate style="margin:0 6px 4px 0">{}</button>'
        parts = []
        if not obj.is_locked:
            parts.append(format_html(btn, u("generate"), "📄 Сформувати документ"))
            if obj.status == Offer.DRAFT:
                parts.append(format_html(btn, u("review"), "👀 На перевірку"))
            if obj.status in (Offer.DRAFT, Offer.REVIEW):
                parts.append(format_html(btn, u("approve"), "✅ Погоджено"))
            parts.append(format_html('<a class="button" href="{}" style="margin:0 6px 4px 0">✉️ Надіслати клієнту</a>',
                                     f"/email/compose/?offer={obj.pk}"))
        if obj.status in (Offer.SENT, Offer.APPROVED, Offer.EXPIRED) and not obj.order_id:
            parts.append(format_html(btn, u("accept"), "🤝 Прийнято → замовлення"))
        if obj.status in (Offer.SENT, Offer.EXPIRED):
            parts.append(format_html(btn, u("reject"), "✖ Відхилено"))
        if obj.status in (Offer.SENT, Offer.REJECTED, Offer.EXPIRED, Offer.CANCELLED):
            parts.append(format_html(btn, u("new_version"), "♻ Нова версія"))
        if obj.order_id:
            parts.append(format_html('<a class="button" href="{}" style="margin:0 6px 4px 0">🛒 Замовлення {}</a>',
                                     reverse("admin:sales_salesorder_change", args=[obj.order_id]), obj.order))
        return format_html("".join(["{}"] * len(parts)), *parts) if parts else "—"

    @admin.display(description="Документи")
    def documents_panel(self, obj):
        if not obj or not obj.pk:
            return "—"
        revs = list(obj.revisions.all())
        if not revs:
            return "Ще не сформовано — «📄 Сформувати документ»."

        def link(f, label):
            return format_html('<a href="{}" target="_blank">{}</a>', f.url, label) if f else ""
        rows = format_html_join("", "<tr><td style='padding:1px 12px 1px 0'>V{}</td><td>{} {}</td>"
                                    "<td style='padding-left:12px;color:var(--body-quiet-color)'>{}</td></tr>",
                                ((r.revision, link(r.pdf_file, "PDF"), link(r.docx_file, "DOCX"),
                                  f"надіслано {r.sent_at:%d.%m.%Y %H:%M}" if r.sent_at else "не надсилалась")
                                 for r in revs))
        return format_html("<table>{}</table>", rows)

    @admin.display(description="Джерела")
    def sources_panel(self, obj):
        if not obj or not obj.pk:
            return "—"
        items = []
        if obj.source_message_id:
            items.append(format_html('✉️ <a href="/email/message/{}/" target="_blank">{}</a>',
                                     obj.source_message_id, obj.source_message.subject or "(без теми)"))
        if obj.source_quote_id:
            items.append(format_html('🛒 запит <a href="{}">{}</a>',
                                     reverse("admin:sales_salesorder_change", args=[obj.source_quote_id]),
                                     obj.source_quote))
        if obj.project_id:
            items.append(format_html('📁 <a href="{}">{}</a>',
                                     reverse("admin:projects_project_change", args=[obj.project_id]), obj.project))
        return format_html("<br>".join(["{}"] * len(items)), *items) if items else "—"

    # ── дії ──
    def get_urls(self):
        def act(name, fn):
            return path(f"<int:pk>/{name.replace('_', '-')}/", self.admin_site.admin_view(fn),
                        name=f"offers_offer_{name}")
        return [act("generate", self.generate_view), act("review", self.review_view),
                act("approve", self.approve_view), act("accept", self.accept_view),
                act("reject", self.reject_view), act("new_version", self.new_version_view)] + super().get_urls()

    def _run(self, request, pk, fn, ok):
        offer = get_object_or_404(Offer, pk=pk)
        if request.method == "POST":
            try:
                result = fn(offer)
                self.message_user(request, ok(result) if callable(ok) else ok)
            except services.OfferError as e:
                self.message_user(request, str(e), messages.ERROR)
        return redirect(reverse("admin:offers_offer_change", args=[pk]))

    def generate_view(self, request, pk):
        return self._run(request, pk, lambda o: services.generate_document(o, request.user),
                         lambda rev: f"Документ V{rev.revision} сформовано"
                                     + ("" if rev.pdf_file else " (лише DOCX — на сервері немає LibreOffice)"))

    def review_view(self, request, pk):
        return self._run(request, pk, lambda o: services.set_status(o, Offer.REVIEW, request.user), "На перевірці.")

    def approve_view(self, request, pk):
        return self._run(request, pk, lambda o: services.set_status(o, Offer.APPROVED, request.user), "Погоджено.")

    def reject_view(self, request, pk):
        return self._run(request, pk, lambda o: services.set_status(o, Offer.REJECTED, request.user),
                         "Позначено відхиленою.")

    def new_version_view(self, request, pk):
        return self._run(request, pk, lambda o: services.new_version(o, request.user),
                         lambda o: f"Версія {o.revision}: можна редагувати; попередня лишилась незмінною.")

    def accept_view(self, request, pk):
        def ok(result):
            order, skipped = result
            tail = f" Без товару з каталогу не перенесено позиції: {', '.join(map(str, skipped))}." if skipped else ""
            return f"Створено замовлення {order}.{tail}"
        return self._run(request, pk, lambda o: services.accept(o, request.user), ok)


@admin.register(OfferSettings)
class OfferSettingsAdmin(admin.ModelAdmin):
    readonly_fields = ["quote_scan_state"]
    fieldsets = [
        ("Номер і умови", {"fields": ["number_prefix", "validity_days", "default_currency", "default_vat_rate",
                                      "default_language", "email_subject"]}),
        ("Тексти за замовчуванням", {"fields": ["intro_text", "payment_terms", "delivery_terms", "outro_text"]}),
        ("Реквізити в документі", {"fields": ["company_block", "footer_text", "template"]}),
        ("📝 Запити пропозицій у пошті", {
            "fields": ["quote_scan_depth", "quote_scan_state"],
            "description": "Нові листи перевіряються автоматично. Щоб позначити листи, отримані раніше, "
                           "виберіть глибину й збережіть — обробка йде у фоні."}),
    ]

    def save_model(self, request, obj, form, change):
        depth = obj.quote_scan_depth
        obj.quote_scan_depth = OfferSettings.SCAN_OFF   # одноразова дія: після запуску поле знову «—»
        super().save_model(request, obj, form, change)
        if depth != OfferSettings.SCAN_OFF:
            from . import detection
            OfferSettings.objects.filter(pk=obj.pk).update(quote_scan_status="⏳ Запущено…", quote_scan_at=timezone.now())
            transaction.on_commit(lambda: detection.start_scan(depth))
            self.message_user(request, f"Пошук запитів ({dict(OfferSettings.SCAN_CHOICES)[depth]}) запущено у фоні — "
                                       f"оновіть сторінку, щоб побачити прогрес.")

    @admin.display(description="Стан обробки")
    def quote_scan_state(self, obj):
        open_requests = QuoteRequest.objects.filter(status=QuoteRequest.NEW).count()
        link = format_html('<a href="{}?status__exact=new">📝 Відкриті запити: {}</a>',
                           reverse("admin:offers_quoterequest_changelist"), open_requests)
        if not obj or not obj.quote_scan_status:
            return format_html("ще не запускалось · {}", link)
        return format_html("{} · {}", obj.quote_scan_status, link)

    def has_add_permission(self, request):
        return not OfferSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        return redirect(reverse("admin:offers_offersettings_change", args=[OfferSettings.get().pk]))


@admin.register(OfferRevision)
class OfferRevisionAdmin(admin.ModelAdmin):
    list_display = ("offer", "revision", "created_at", "sent_at")
    readonly_fields = ("offer", "revision", "snapshot", "docx_file", "pdf_file", "created_by", "created_at",
                       "sent_at", "sent_message_id")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(QuoteRequest)
class QuoteRequestAdmin(admin.ModelAdmin):
    """Листи, схожі на запит пропозиції: перевірити, створити Angebot у пошті («📝 Angebot») або відхилити."""
    list_display = ("created_at", "mail_link", "sender", "reason", "status", "offer")
    list_filter = ("status",)
    search_fields = ("message__subject", "message__from_email", "reason")
    readonly_fields = ("message", "thread", "reason", "items", "offer", "decided_by", "created_at")
    fields = ("status", "message", "thread", "reason", "items", "offer", "decided_by", "created_at")

    def has_add_permission(self, request):
        return False

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("message", "offer")
        return qs if request.user.is_superuser else qs.filter(message__account__user=request.user)

    @admin.display(description="Лист")
    def mail_link(self, obj):
        return format_html('<a href="/email/message/{}/" target="_blank">{}</a>',
                           obj.message_id, obj.message.subject or "(без теми)")

    @admin.display(description="Від")
    def sender(self, obj):
        return obj.message.from_name or obj.message.from_email
