"""
suppliers/admin.py — 🗂 Категорії постачальників і 📨 Запити постачальникам (RFQ).

Запит: «＋ Новий запит» (форма: що, параметри за видом — плати / кабелі / компоненти…, кількості, файли, кому —
постачальники за категорією, текст листа) → сторінка запиту (кому пішло, відповіді, ціни, AI-розбір, порівняння,
вибір, чернетка закупівлі). Права — розділ «Постачальники»: перегляд — view, створити — add, надіслати й вести —
change. Логіка — suppliers/services/rfq.py.
"""
import datetime

from django import forms
from django.contrib import admin, messages
from django.db.models import Count, Q
from django.http import Http404, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from suppliers.models import (SupplierCategory, SupplierEvent, SupplierRequest, SupplierRequestFile,
                              SupplierRequestRecipient)
from suppliers.services import rfq


class SupplierCategoryForm(forms.ModelForm):
    request_template = forms.ChoiceField(label="Форма запиту (RFQ)", required=False,
                                         choices=[("", "—")] + rfq.TEMPLATE_CHOICES)

    class Meta:
        model = SupplierCategory
        fields = ("name", "icon", "color", "parent", "order", "description", "request_template", "is_active")
        widgets = {"color": forms.TextInput(attrs={"type": "color"}), "description": forms.Textarea(attrs={"rows": 3})}


@admin.register(SupplierCategory)
class SupplierCategoryAdmin(admin.ModelAdmin):
    form = SupplierCategoryForm
    list_display = ("label", "suppliers_col", "requests_col", "request_template", "order", "is_active")
    list_editable = ("order", "is_active")
    search_fields = ("name", "description")
    list_filter = ("is_active",)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("parent").annotate(
            _n=Count("suppliers", distinct=True), _r=Count("requests", distinct=True))

    @admin.display(description="Категорія", ordering="name")
    def label(self, obj):
        return format_html('<span style="border-left:4px solid {};padding-left:6px">{} {}</span>{}', obj.color or "#888",
                           obj.icon, obj.name, format_html(' <span style="color:var(--text-muted)">· {}</span>',
                                                           obj.parent.name) if obj.parent_id else "")

    @admin.display(description="Постачальників", ordering="_n")
    def suppliers_col(self, obj):
        return format_html('<a href="{}?cat={}">{}</a>', reverse("admin:inventory_supplier_changelist"), obj.pk,
                           obj._n)

    @admin.display(description="Запитів", ordering="_r")
    def requests_col(self, obj):
        return format_html('<a href="{}?category__id__exact={}">{}</a>',
                           reverse("admin:suppliers_supplierrequest_changelist"), obj.pk, obj._r) if obj._r else "—"


class GroupFilter(admin.SimpleListFilter):
    title = "група / тема"
    parameter_name = "group"

    def lookups(self, request, model_admin):
        groups = (SupplierRequest.objects.exclude(group="").values_list("group", flat=True)
                  .order_by("-created_at").distinct()[:40])
        seen = []
        for g in groups:
            if g not in seen:
                seen.append(g)
        return [(g, g) for g in seen]

    def queryset(self, request, queryset):
        return queryset.filter(group=self.value()) if self.value() else queryset


@admin.register(SupplierRequest)
class SupplierRequestAdmin(admin.ModelAdmin):
    list_display = ("code_col", "title", "status", "group", "project", "category", "answers_col", "reply_by",
                    "created_at")
    list_filter = ("status", GroupFilter, "category", "project")
    search_fields = ("code", "title", "group", "body", "recipients__supplier__name")
    date_hierarchy = "created_at"

    def get_queryset(self, request):
        R = SupplierRequestRecipient
        return super().get_queryset(request).select_related("project", "category").annotate(
            _sent=Count("recipients", filter=~Q(recipients__state__in=[R.PLANNED, R.FAILED]), distinct=True),
            _ans=Count("recipients", filter=Q(recipients__state__in=[R.REPLIED, R.QUOTED, R.DECLINED]), distinct=True),
            _quoted=Count("recipients", filter=Q(recipients__state=R.QUOTED), distinct=True),
            _all=Count("recipients", distinct=True))

    @admin.display(description="Номер", ordering="code")
    def code_col(self, obj):
        return format_html('<a href="{}" translate="no"><b>{}</b></a>',
                           reverse("admin:suppliers_supplierrequest_work", args=[obj.pk]), obj.code)

    @admin.display(description="Відповіли")
    def answers_col(self, obj):
        if not obj._all:
            return "—"
        return f"{obj._ans} з {obj._sent or obj._all}" + (f" · 💶 {obj._quoted}" if obj._quoted else "")

    def get_urls(self):
        view = self.admin_site.admin_view
        return [
            path("new/", view(self.form_view), name="suppliers_supplierrequest_new"),
            path("<int:pk>/edit/", view(self.form_view), name="suppliers_supplierrequest_edit"),
            path("<int:pk>/work/", view(self.work_view), name="suppliers_supplierrequest_work"),
            path("<int:pk>/action/", view(self.action_view), name="suppliers_supplierrequest_action"),
            path("preview/", view(self.preview_view), name="suppliers_supplierrequest_preview"),
            path("ai-letter/", view(self.ai_letter_view), name="suppliers_supplierrequest_ai_letter"),
        ] + super().get_urls()

    def add_view(self, request, form_url="", extra_context=None):
        return redirect(reverse("admin:suppliers_supplierrequest_new") + (
            "?" + request.GET.urlencode() if request.GET else ""))

    def change_view(self, request, object_id, form_url="", extra_context=None):
        if str(object_id).isdigit():
            return redirect(reverse("admin:suppliers_supplierrequest_work", args=[int(object_id)]))
        return super().change_view(request, object_id, form_url, extra_context)

    # ── допоміжне ──
    def preview_view(self, request):
        """👁 Як виглядатиме лист одному отримувачу — з того, що зараз у формі (ще не збережене теж): тема й текст
        зі змінними (rfq.render), кому, копія (rfq.cc_for), вкладення запиту. Нічого не зберігає й не надсилає."""
        from inventory.models import Supplier
        from suppliers.models import SupplierContact
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        P = request.POST
        req = self._request(request, int(P["req"]), "change") if str(P.get("req") or "").isdigit() else None
        if req is None and not self.has_add_permission(request):
            raise Http404
        draft = SupplierRequest(code=req.code if req else "", title=P.get("title", "").strip()[:200],
                                language=P.get("language") if P.get("language") in dict(SupplierRequest.LANGS) else "en",
                                subject=P.get("subject", "").strip()[:250], body=P.get("body", ""),
                                cc=P.get("cc", "")[:500], cc_contacts=bool(P.get("cc_contacts")))
        try:
            draft.reply_by = datetime.date.fromisoformat(P.get("reply_by", "")) if P.get("reply_by") else None
        except ValueError:
            draft.reply_by = None
        supplier = Supplier.objects.filter(pk=P.get("supplier") or 0).first()
        if supplier is None:
            return JsonResponse({"error": "Позначте постачальника в «🏭 Кому»."}, status=400)
        contact = SupplierContact.objects.filter(supplier=supplier, pk=P.get("contact") or 0).first()
        email, contact = rfq.pick_email(supplier, contact)
        r = SupplierRequestRecipient(request=draft, supplier=supplier, contact=contact, email=email)
        subject, body = rfq.render(draft, r)
        if not draft.body.strip():
            body = ""
        return JsonResponse({"to": email or "⚠️ немає email", "cc": rfq.cc_for(draft, r), "subject": subject,
                             "body": body, "files": [f.name for f in req.files.all()] if req else []})

    def ai_letter_view(self, request):
        """🧠 AI складає тему й текст запиту з того, що зараз у формі (поля, параметри, файли, чернетка людини). Фоном
        (suppliers/services/jobs.py) → {subject, body}; нічого не зберігає й не надсилає."""
        from config.models import SystemSettings
        from suppliers.services import ai, jobs
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        P = request.POST
        req = self._request(request, int(P["req"]), "change") if str(P.get("req") or "").isdigit() else None
        if req is None and not self.has_add_permission(request):
            raise Http404
        template = P.get("template") if P.get("template") in rfq.TEMPLATES else "generic"
        category = SupplierCategory.objects.filter(pk=P.get("category") or 0).first()
        # файли — і ще не збережені (браузер шле їх сюди), і вже прикріплені; вміст читаємо тут, не у фоні
        from suppliers.services import rfq_files
        blobs = [(u.name, u.read(rfq_files.FILE_MAX + 1)) for u in request.FILES.getlist("files")[:20]]
        if req is not None:
            gone = set(P.getlist("del_file"))
            for f in req.files.all():
                if str(f.pk) in gone:
                    continue
                try:
                    with f.file.open("rb") as fh:
                        blobs.insert(0, (f.name, fh.read(rfq_files.FILE_MAX + 1)))
                except OSError:
                    blobs.insert(0, (f.name, b""))
        files = [n for n, _ in blobs] + [n[:120] for n in P.getlist("file_names") if n.strip()][:30]
        file_texts = rfq_files.texts(blobs)
        try:
            company = SystemSettings.objects.first().company_name
        except AttributeError:
            company = ""
        form = {"language": P.get("language"), "template": template, "title": P.get("title", "").strip()[:200],
                "category": str(category) if category else "",
                "specs": {f[0]: P.get(f"spec_{f[0]}", "").strip()[:2000] for f in rfq.fields_of(template)},
                "notes": P.get("notes", "").strip()[:4000], "quantities": P.get("quantities", "")[:120],
                "files": files, "reply_by": P.get("reply_by", ""), "company": company,
                "subject": P.get("subject", "").strip()[:250], "body": P.get("body", "").strip()}
        model, user = ai.model_of(P.get("model")), request.user

        def run():
            out = ai.letter(form, user=user, model=model, files=file_texts)
            return {**out, "message": f"🧠 Готово · ${out['cost_usd']:.3f} — перевірте й за потреби виправте"}
        return JsonResponse({"job": jobs.start(user, "rfq letter", run)})

    def _ctx(self, request, title, **extra):
        from suppliers.admin_supplier import _JOB_JS, _STYLE
        from suppliers.services.ai import MODELS
        return {**self.admin_site.each_context(request), "title": title, "opts": self.model._meta,
                "models": MODELS, "style": _STYLE, "job_js": _JOB_JS, **extra}

    def _request(self, request, pk, need="view"):
        req = SupplierRequest.objects.select_related("project", "category", "account", "owner").filter(pk=pk).first()
        if req is None:
            raise Http404
        ok = (self.has_view_permission(request, req) if need == "view" else
              self.has_change_permission(request, req))
        if not ok:
            raise Http404
        return req

    @staticmethod
    def _suppliers():
        from inventory.models import Supplier
        return (Supplier.objects.exclude(status=Supplier.STATUS_BLOCKED).prefetch_related("categories", "contacts")
                .order_by("name"))

    @classmethod
    def _recipients(cls) -> dict:
        """«🏭 Кому»: постачальники (без ⛔ «Не працювати») з ознаками для фільтрів, як у списку постачальників:
        стан, категорії, звідки (🎪 з виставки — візитка з позначкою, інакше source), де взяли (події всіх візиток),
        країна; і варіанти фільтрів — лише ті, що трапляються."""
        from inventory.models import Supplier
        from shop.countries import name as country_name
        from suppliers.admin_supplier import FAIR_LABEL, SOURCE_LABELS
        from suppliers.models import SupplierMedia
        from suppliers.services.cards import event_key
        sups = list(cls._suppliers())
        events, names = {}, {}
        for sid, ev in (SupplierMedia.objects.exclude(supplier=None).exclude(event="").order_by("created_at")
                        .values_list("supplier_id", "event")):
            key = event_key(ev)
            events.setdefault(sid, set()).add(key)
            names.setdefault(key, " ".join(ev.split()))
        fair = set(SupplierMedia.objects.exclude(supplier=None).filter(marks__icontains='"fair"')
                   .values_list("supplier_id", flat=True))
        photos = {}                         # найновіша візитка з фото — показ при наведенні
        for m in (SupplierMedia.objects.exclude(supplier=None).exclude(image="").order_by("-created_at")
                  .only("pk", "supplier_id", "image", "event")):
            photos.setdefault(m.supplier_id, m)
        for s in sups:
            card = photos.get(s.pk)
            s.f_photo = card.image.url if card else ""
            s.f_photo_note = (card.event or "") if card else ""
            s.f_src = "fair" if s.pk in fair and s.source == "card" else (s.source or "manual")   # як «Звідки» в списку
            s.f_fair = s.pk in fair
            s.f_where = "|".join(sorted(events.get(s.pk, ())))
            s.f_where_label = ", ".join(names[k] for k in sorted(events.get(s.pk, ())))[:80]
            s.f_country = (s.addr_country or "").upper()
        used_src = {s.f_src for s in sups}
        return {
            "suppliers": sups,
            "f_statuses": [(k, v) for k, v in Supplier.STATUS_CHOICES if k != Supplier.STATUS_BLOCKED],
            "f_sources": [(k, v) for k, v in [("fair", FAIR_LABEL)] + list(SOURCE_LABELS.items()) if k in used_src],
            "f_events": sorted(((k, names[k]) for k in names if any(k in events.get(s.pk, ()) for s in sups)),
                               key=lambda x: x[1].lower()),
            "f_countries": sorted({(s.f_country, country_name(s.f_country)) for s in sups if s.f_country},
                                  key=lambda x: x[1]),
        }

    # ── ＋ Новий / ✏️ запит ──
    def form_view(self, request, pk=None):
        req = self._request(request, pk, "change") if pk else None
        if req is None and not self.has_add_permission(request):
            raise Http404
        if request.method == "POST":
            return self._save_form(request, req)
        initial_supplier = request.GET.get("supplier")
        project = None
        if req is None and str(request.GET.get("project") or "").isdigit():
            from projects.models import Project
            project = Project.objects.filter(pk=int(request.GET["project"])).first()
        category = None
        if req is None and str(request.GET.get("category") or "").isdigit():
            category = SupplierCategory.objects.filter(pk=int(request.GET["category"])).first()
        if req is None:
            req = SupplierRequest(language="en", project=project, category=category,
                                  template=(category.request_template if category and category.request_template
                                            else "generic"),
                                  reply_by=timezone.localdate() + datetime.timedelta(days=7))
            picked = {int(initial_supplier)} if str(initial_supplier or "").isdigit() else set()
            # «📨 Запит (RFQ) вибраним» зі списку постачальників — ?suppliers=1,2,3
            picked |= {int(x) for x in str(request.GET.get("suppliers") or "").split(",") if x.isdigit()}
            if category is not None and not picked:
                picked = set(category.suppliers.values_list("pk", flat=True))
        else:
            picked = set(req.recipients.values_list("supplier_id", flat=True))
        from projects.models import Project
        projects = Project.objects.filter(Q(status__in=Project.OPEN_STATUSES) | Q(pk=req.project_id)).order_by("-pk")[:300]
        groups = list(SupplierRequest.objects.exclude(group="").values_list("group", flat=True)
                      .order_by("-created_at").distinct()[:60])
        accounts = []
        from email_assistant.models import EmailAccount
        accounts = list(EmailAccount.objects.filter(user=request.user, is_active=True).order_by("-is_primary", "pk"))
        templates = [{"key": k, "label": v[0], "fields": [{"key": f[0], "label": f[1], "hint": f[4],
                                                            "value": (req.specs or {}).get(f[0], ""),
                                                            "multi": f[0] in ("parts", "stackup", "spec", "consigned")}
                                                           for f in v[1]]}
                     for k, v in rfq.TEMPLATES.items()]
        sent = {r.supplier_id: r for r in req.recipients.all()} if req.pk else {}
        return TemplateResponse(request, "admin/suppliers/request_form.html", self._ctx(
            request, f"✏️ {req.code}" if req.pk else "📨 Новий запит постачальникам", req=req, templates=templates,
            categories=SupplierCategory.objects.filter(is_active=True), projects=projects, groups=groups,
            picked=picked, sent=sent, accounts=accounts, placeholders=rfq.PLACEHOLDERS, **self._recipients(),
            examples={k: {"subject": v[0], "body": v[1]} for k, v in rfq.EXAMPLES.items()},
            example_body=rfq.EXAMPLES.get(req.language, rfq.EXAMPLES["en"])[1],
            langs=SupplierRequest.LANGS, files=list(req.files.all()) if req.pk else [],
            notes=(req.specs or {}).get("_notes", "")))

    def _save_form(self, request, req):
        from inventory.models import Supplier
        from projects.models import Project
        new = req is None
        if new:
            req = SupplierRequest(owner=request.user)
        P = request.POST
        req.title = P.get("title", "").strip()[:200] or "Запит"
        req.template = P.get("template") if P.get("template") in rfq.TEMPLATES else "generic"
        req.category = SupplierCategory.objects.filter(pk=P.get("category") or 0).first()
        req.project = Project.objects.filter(pk=P.get("project") or 0).first()
        req.group = " ".join(P.get("group", "").split())[:120]
        req.quantities = P.get("quantities", "").strip()[:120]
        req.language = P.get("language") if P.get("language") in dict(SupplierRequest.LANGS) else "en"
        try:
            req.reply_by = datetime.date.fromisoformat(P.get("reply_by", "")) if P.get("reply_by") else None
        except ValueError:
            req.reply_by = None
        specs = {f[0]: P.get(f"spec_{f[0]}", "").strip()[:2000] for f in rfq.fields_of(req.template)}
        specs = {k: v for k, v in specs.items() if v}
        if P.get("notes", "").strip():
            specs["_notes"] = P["notes"].strip()[:4000]
        req.specs = specs
        req.cc = ", ".join(rfq.addresses(P.get("cc", "")))[:500]
        req.cc_contacts = bool(P.get("cc_contacts"))
        from email_assistant.models import EmailAccount
        acc = EmailAccount.objects.filter(pk=P.get("account") or 0, user=request.user).first()
        if acc is not None:
            req.account = acc
        req.save()
        for upload in request.FILES.getlist("files")[:20]:
            try:
                rfq.add_file(req, upload, request.user)
            except rfq.RFQError as e:
                self.message_user(request, f"⚠️ {upload.name}: {e}", messages.ERROR)
        for fid in P.getlist("del_file"):
            f = req.files.filter(pk=fid).first()
            if f is not None:
                f.file.delete(save=False)
                f.delete()
        # отримувачі: нові — додати, зняті галочки — прибрати, лише ще не надіслані
        ids = {int(x) for x in P.getlist("rcp") if str(x).isdigit()}
        req.recipients.filter(state__in=[SupplierRequestRecipient.PLANNED, SupplierRequestRecipient.FAILED]) \
            .exclude(supplier_id__in=ids).delete()
        suppliers = list(Supplier.objects.filter(pk__in=ids).prefetch_related("contacts"))
        contacts = {}
        for s in suppliers:
            cid = P.get(f"contact_{s.pk}")
            if str(cid or "").isdigit():
                c = s.contacts.filter(pk=int(cid)).first()
                if c is not None:
                    contacts[s.pk] = c
        out = rfq.add_recipients(req, suppliers, contacts)
        for s in suppliers:   # змінили контакт ще не надісланого
            c = contacts.get(s.pk)
            if c is not None and c.email:
                req.recipients.filter(supplier=s, state=SupplierRequestRecipient.PLANNED).update(contact=c, email=c.email)
        if out["skipped"]:
            self.message_user(request, "⚠️ Без email (не додано): " + ", ".join(out["skipped"]), messages.WARNING)
        body = P.get("body", "").strip()
        req.body = body if body and not P.get("regen") else rfq.build_body(req)
        req.subject = P.get("subject", "").strip()[:250] or rfq.subject_for(req)
        if P.get("regen"):
            req.subject = rfq.subject_for(req)
        req.save(update_fields=["body", "subject"])
        work =reverse("admin:suppliers_supplierrequest_work", args=[req.pk])
        if P.get("go") == "send":
            return self._send(request, req, work)
        if P.get("go") == "stay" or P.get("regen"):
            return redirect(reverse("admin:suppliers_supplierrequest_edit", args=[req.pk]))
        self.message_user(request, f"💾 {req.code} збережено", messages.SUCCESS)
        return redirect(work)

    def _send(self, request, req, back, only=None):
        if not self.has_change_permission(request, req):
            raise Http404
        try:
            out = rfq.send(req, request.user, only=only)
        except rfq.RFQError as e:
            self.message_user(request, f"⚠️ {e}", messages.ERROR)
            return redirect(back)
        if out["sent"]:
            self.message_user(request, f"📤 Надіслано: {out['sent']}", messages.SUCCESS)
        for name, err in out["failed"]:
            self.message_user(request, f"⚠️ {name}: {err}", messages.ERROR)
        if not out["sent"] and not out["failed"]:
            self.message_user(request, "Нікому надсилати — усім уже надіслано або отримувачів немає.", messages.INFO)
        return redirect(back)

    # ── сторінка запиту ──
    def work_view(self, request, pk):
        req = self._request(request, pk)
        rows = rfq.comparison(req)
        for row in rows:
            r = row["recipient"]
            row["sent_link"] = r.sent_message_id if r.sent_message_id and r.sent_message and \
                r.sent_message.account.user_id == request.user.pk else None
            row["reply_link"] = r.reply_message_id if r.reply_message_id and r.reply_message and \
                r.reply_message.account.user_id == request.user.pk else None
        from inventory.models import Supplier
        in_req = {row["recipient"].supplier_id for row in rows}
        more = Supplier.objects.exclude(pk__in=in_req).exclude(status=Supplier.STATUS_BLOCKED)
        if req.category_id:
            more = more.filter(categories=req.category_id)
        events = SupplierEvent.objects.filter(request=req).select_related("supplier", "user")[:40]
        return TemplateResponse(request, "admin/suppliers/request_work.html", self._ctx(
            request, f"📨 {req.code} · {req.title}", req=req, rows=rows, files=list(req.files.all()),
            overdue=rfq.waiting_overdue(req), can_change=self.has_change_permission(request, req),
            states=[s for s in SupplierRequestRecipient.STATES if s[0] != SupplierRequestRecipient.PLANNED],
            more=more.order_by("name")[:200], events=events, quantities=rfq.parse_quantities(req.quantities),
            planned=sum(1 for row in rows if row["recipient"].state in (SupplierRequestRecipient.PLANNED,
                                                                          SupplierRequestRecipient.FAILED))))

    def action_view(self, request, pk):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        req = self._request(request, pk, "change")
        from suppliers.services import ai, jobs
        do = request.POST.get("do", "")
        back = reverse("admin:suppliers_supplierrequest_work", args=[req.pk])
        r = None
        if str(request.POST.get("rid") or "").isdigit():
            r = req.recipients.select_related("supplier", "contact", "sent_message", "request__account") \
                .filter(pk=int(request.POST["rid"])).first()
        try:
            if do == "send":
                return self._send(request, req, back)
            if do == "send_one" and r is not None:
                return self._send(request, req, back, only=[r.pk])
            if do == "remind" and r is not None:
                rfq.remind(r, request.user)
                self.message_user(request, f"⏰ Нагадування → {r.email}", messages.SUCCESS)
            elif do == "quote" and r is not None:
                rfq.set_quote(r, request.POST)
                self.message_user(request, f"💾 {r.supplier.name}: пропозицію збережено", messages.SUCCESS)
            elif do == "state" and r is not None:
                st = request.POST.get("state")
                if st in dict(SupplierRequestRecipient.STATES) and st != SupplierRequestRecipient.PLANNED:
                    SupplierRequestRecipient.objects.filter(pk=r.pk).update(state=st)
            elif do == "ai_quote" and r is not None:
                model, user = ai.model_of(request.POST.get("model")), request.user

                def run():
                    data = ai.quote(r, user=user, model=model)
                    rfq.apply_ai(r, data, overwrite=bool(request.POST.get("overwrite")))
                    return {"url": back, "message": "🧠 Розібрано"}
                return JsonResponse({"job": jobs.start(user, f"quote {r.pk}", run)})
            elif do == "compare_ai":
                model, user = ai.model_of(request.POST.get("model")), request.user

                def run():
                    data = ai.compare(req, user=user, model=model)
                    SupplierRequest.objects.filter(pk=req.pk).update(analysis={**data, "at": timezone.now().isoformat()})
                    return {"url": back, "message": "🏆 Готово"}
                return JsonResponse({"job": jobs.start(user, f"compare {req.pk}", run)})
            elif do == "choose" and r is not None:
                rfq.choose(r, request.user)
                self.message_user(request, f"🏆 Вибрано: {r.supplier.name}", messages.SUCCESS)
            elif do == "make_po" and r is not None:
                if not request.user.has_perm("inventory.add_purchaseorder"):
                    raise rfq.RFQError("Немає права створювати закупівлі.")
                po = rfq.make_purchase(r, request.user)
                self.message_user(request, f"🛒 Чернетка закупівлі {po.code} — перевірте позиції й ціни", messages.SUCCESS)
                return redirect(reverse("admin:inventory_purchaseorder_change", args=[po.pk]))
            elif do == "remove" and r is not None:
                if r.state in (SupplierRequestRecipient.PLANNED, SupplierRequestRecipient.FAILED):
                    r.delete()
            elif do == "add":
                from inventory.models import Supplier
                ids = [int(x) for x in request.POST.getlist("rcp") if str(x).isdigit()]
                out = rfq.add_recipients(req, Supplier.objects.filter(pk__in=ids).prefetch_related("contacts"))
                msg = f"➕ Додано: {out['added']}"
                if out["skipped"]:
                    msg += " · без email: " + ", ".join(out["skipped"])
                self.message_user(request, msg, messages.SUCCESS if out["added"] else messages.WARNING)
            elif do == "close":
                rfq.close(req, request.POST.get("decision", ""), request.user)
                self.message_user(request, "✅ Запит закрито", messages.SUCCESS)
            elif do == "reopen":
                SupplierRequest.objects.filter(pk=req.pk).update(
                    status=SupplierRequest.ANSWERED if req.recipients.exclude(replied_at=None).exists()
                    else SupplierRequest.SENT if req.sent_at else SupplierRequest.DRAFT, closed_at=None)
            elif do == "cancel":
                SupplierRequest.objects.filter(pk=req.pk).update(status=SupplierRequest.CANCELLED,
                                                                 closed_at=timezone.now())
            elif do == "decision":
                SupplierRequest.objects.filter(pk=req.pk).update(decision=request.POST.get("decision", "")[:4000])
        except rfq.RFQError as e:
            self.message_user(request, f"⚠️ {e}", messages.ERROR)
        return redirect(back)
