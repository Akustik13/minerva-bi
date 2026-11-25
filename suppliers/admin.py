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
from django.http import Http404, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from suppliers.models import (SupplierCategory, SupplierEvent, SupplierRequest, SupplierRequestFile,
                              SupplierRequestLetter, SupplierRequestRecipient)
from suppliers.icons import IconPickerWidget
from suppliers.services import rfq


class SupplierCategoryForm(forms.ModelForm):
    request_template = forms.ChoiceField(label="Форма запиту (RFQ)", required=False,
                                         choices=[("", "—")] + rfq.TEMPLATE_CHOICES)

    class Meta:
        model = SupplierCategory
        fields = ("name", "icon", "icon_image", "color", "parent", "order", "description", "request_template",
                  "is_active")
        widgets = {"color": forms.TextInput(attrs={"type": "color"}), "description": forms.Textarea(attrs={"rows": 3}),
                   "icon": IconPickerWidget()}
        help_texts = {"icon": "Стандартний — клік у палітрі, або будь-який свій емодзі.",
                      "icon_image": "PNG, JPG, GIF або WEBP — логотип, піктограма; зменшиться до 128 px. Є картинка — "
                                    "показується вона, емодзі — там, де картинки не буває (списки вибору, додаток)."}

    def clean_icon_image(self):
        from suppliers.icons import clean_image
        value = self.cleaned_data.get("icon_image")
        if value and hasattr(value, "content_type"):      # щойно завантажене (не те, що вже збережено)
            return clean_image(value)
        return value


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
                           obj.icon_html, obj.name, format_html(' <span style="color:var(--text-muted)">· {}</span>',
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


def _new_mail(r) -> bool:
    """📥 Після розбору AI прийшов ще лист постачальника (нова ціна, окремий лист) — розібрати знову."""
    at = (r.ai or {}).get("at")
    if not at:
        return False
    try:
        parsed = datetime.datetime.fromisoformat(str(at))
    except ValueError:
        return False
    from suppliers.services import rfq_replies
    last = rfq_replies.last_theirs_at(r)
    return bool(last and last > parsed)


def _js(value) -> str:
    """Значення для <script> (JSON без «<», «>», «&» — не закриє тег)."""
    import json
    return (json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def _letter_extras(account) -> dict:
    """Звідки й з яким підписом піде лист (для 👁): from, signature {text, html — очищений}, як налаштувати підпис."""
    from django.urls import NoReverseMatch
    sig = rfq.signature(account)
    try:
        sig_url = reverse("admin:email_assistant_emailaccount_change", args=[account.pk]) if account else ""
    except NoReverseMatch:
        sig_url = ""
    return {"from": account.email_address if account else "", "signature": sig, "signature_url": sig_url}


def _recipient_rows(suppliers) -> list:
    """Постачальники для «➕ Додати отримувачів» з тим, кому піде лист (rfq.pick_email без запиту на кожного):
    f_email, f_contact (контакт або None), f_mails (контакти з email — вибір), f_country, f_country_name."""
    from shop.countries import name as country_name
    out = []
    for s in suppliers:
        mails = sorted((c for c in s.contacts.all() if c.email and c.is_active), key=lambda c: (not c.is_primary, c.pk))
        s.f_mails = mails
        s.f_contact = mails[0] if mails else None
        s.f_email = mails[0].email if mails else (s.email or "")
        s.f_country = (s.addr_country or "").upper()
        s.f_country_name = country_name(s.f_country) if s.f_country else ""
        out.append(s)
    return out


@admin.register(SupplierRequest)
class SupplierRequestAdmin(admin.ModelAdmin):
    list_display = ("code_col", "title", "status", "group", "project", "category", "answers_col", "reply_by",
                    "created_at")
    list_filter = ("status", GroupFilter, "category", "project")
    search_fields = ("code", "title", "group", "body", "recipients__supplier__name")
    date_hierarchy = "created_at"
    actions = ["copy_selected"]
    _can_add = False

    def get_queryset(self, request):
        R = SupplierRequestRecipient
        return super().get_queryset(request).select_related("project", "category", "copied_from").annotate(
            _sent=Count("recipients", filter=~Q(recipients__state__in=[R.PLANNED, R.FAILED]), distinct=True),
            _ans=Count("recipients", filter=Q(recipients__state__in=[R.REPLIED, R.QUOTED, R.DECLINED]), distinct=True),
            _quoted=Count("recipients", filter=Q(recipients__state=R.QUOTED), distinct=True),
            _all=Count("recipients", distinct=True))

    def changelist_view(self, request, extra_context=None):
        self._can_add = self.has_add_permission(request)
        return super().changelist_view(request, extra_context)

    @admin.display(description="Номер", ordering="code")
    def code_col(self, obj):
        from suppliers.services import rfq_copy
        html = format_html('<a href="{}" translate="no"><b>{}</b></a>',
                           reverse("admin:suppliers_supplierrequest_work", args=[obj.pk]), obj.code)
        if obj.copied_from_id and obj.copied_from:
            html += format_html('<br><small class="spf-muted" title="Копія цього запиту">📋 з <span translate="no">{}'
                                '</span></small>', obj.copied_from.code)
        left = rfq_copy.pending(obj)
        if left:
            html += format_html('<br><a href="{}" class="rq-left" title="{}">⚠️ перевірити: {}</a>',
                                reverse("admin:suppliers_supplierrequest_edit", args=[obj.pk]),
                                ", ".join(x["label"] for x in left), len(left))
        if self._can_add:
            html += format_html('<br><a href="{}?copy={}" title="Новий запит на основі цього: що, параметри, кількості, '
                                'файли, кому, лист — що зазвичай треба змінити, буде підсвічено">📋 Копіювати</a>',
                                reverse("admin:suppliers_supplierrequest_new"), obj.pk)
        return html

    @admin.action(description="📋 Копіювати — новий запит на основі вибраного", permissions=["add"])
    def copy_selected(self, request, queryset):
        ids = list(queryset.values_list("pk", flat=True)[:2])
        if len(ids) != 1:
            self.message_user(request, "📋 Виберіть один запит — на його основі відкриється форма нового.",
                              messages.WARNING)
            return None
        return redirect(f"{reverse('admin:suppliers_supplierrequest_new')}?copy={ids[0]}")

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
            path("<int:pk>/letters/", view(self.letters_view), name="suppliers_supplierrequest_letters"),
            path("<int:pk>/missed/", view(self.missed_view), name="suppliers_supplierrequest_missed"),
            path("<int:pk>/recipient/<int:rid>/panel/", view(self.panel_view), name="suppliers_supplierrequest_panel"),
            path("<int:pk>/recipient/<int:rid>/chat/", view(self.chat_view), name="suppliers_supplierrequest_chat"),
            path("<int:pk>/file/<str:src>/<int:fid>/", view(self.file_view), name="suppliers_supplierrequest_file"),
            path("<int:pk>/recipient/<int:rid>/file/<int:msg>/<int:idx>/", view(self.chat_file_view),
                 name="suppliers_supplierrequest_chat_file"),
            path("preview/", view(self.preview_view), name="suppliers_supplierrequest_preview"),
            path("ai-letter/", view(self.ai_letter_view), name="suppliers_supplierrequest_ai_letter"),
            path("test-mail/", view(self.test_mail_view), name="suppliers_supplierrequest_test_mail"),
        ] + super().get_urls()

    def add_view(self, request, form_url="", extra_context=None):
        return redirect(reverse("admin:suppliers_supplierrequest_new") + (
            "?" + request.GET.urlencode() if request.GET else ""))

    def change_view(self, request, object_id, form_url="", extra_context=None):
        if str(object_id).isdigit():
            return redirect(reverse("admin:suppliers_supplierrequest_work", args=[int(object_id)]))
        return super().change_view(request, object_id, form_url, extra_context)

    # ── допоміжне ──
    @staticmethod
    def _colleagues() -> list:
        """Колеги (активні співробітники з email) — підказки в «Копія (CC)» разом з адресною книгою пошти."""
        from django.contrib.auth.models import User
        return [{"email": u.email, "name": u.get_full_name() or u.username, "source": "колега"}
                for u in User.objects.filter(is_active=True, is_staff=True).exclude(email="").order_by("first_name")[:200]]

    def _draft(self, request):
        """(запит або None, чернетка з того, що зараз у формі — ще не збережене теж, отримувач або None) — для
        👁 перегляду й ✉️ тестового листа. Отримувач — позначений постачальник (supplier) і його контакт (contact)."""
        from inventory.models import Supplier
        from suppliers.models import SupplierContact
        P = request.POST
        req = self._request(request, int(P["req"]), "change") if str(P.get("req") or "").isdigit() else None
        if req is None and not self.has_add_permission(request):
            raise Http404
        draft = SupplierRequest(code=req.code if req else "RFQ-…", title=P.get("title", "").strip()[:200],
                                language=P.get("language") if P.get("language") in dict(SupplierRequest.LANGS) else "en",
                                subject=P.get("subject", "").strip()[:250], body=P.get("body", ""),
                                cc=P.get("cc", "")[:500], cc_contacts=bool(P.get("cc_contacts")),
                                account_id=req.account_id if req else None)
        try:
            draft.reply_by = datetime.date.fromisoformat(P.get("reply_by", "")) if P.get("reply_by") else None
        except ValueError:
            draft.reply_by = None
        pk = str(P.get("supplier") or "")
        supplier = Supplier.objects.filter(pk=int(pk)).first() if pk.isdigit() else None
        if supplier is None:
            return req, draft, None
        cpk = str(P.get("contact") or "")
        contact = SupplierContact.objects.filter(supplier=supplier, pk=int(cpk)).first() if cpk.isdigit() else None
        email, contact = rfq.pick_email(supplier, contact)
        return req, draft, SupplierRequestRecipient(request=draft, supplier=supplier, contact=contact, email=email)

    def _form_files(self, request, req):
        """Файли, що зараз у формі: збереженого запиту або (📋 копія, ще не збережена) запиту copy_from — крім
        позначених «прибрати» / «не брати»."""
        P = request.POST
        if req is not None:
            files = req.files.all()
        elif str(P.get("copy_from") or "").isdigit():
            files = self._request(request, int(P["copy_from"]), "view").files.all()
        else:
            return []
        gone = set(P.getlist("del_file"))
        return [f for f in files if str(f.pk) not in gone]

    def preview_view(self, request):
        """👁 Як виглядатиме лист одному отримувачу — з того, що зараз у формі (ще не збережене теж): тема й текст
        зі змінними (rfq.render), кому, копія (rfq.cc_for), вкладення запиту. Нічого не зберігає й не надсилає."""
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        req, draft, r = self._draft(request)
        if r is None:
            return JsonResponse({"error": "Позначте постачальника в «🏭 Кому»."}, status=400)
        subject, body = rfq.render(draft, r)
        if not draft.body.strip():
            body = ""
        account = rfq.account_for(request.user, draft)
        return JsonResponse({"to": r.email or "⚠️ немає email", "cc": rfq.cc_for(draft, r), "subject": subject,
                             "body": body, "files": [f.name for f in self._form_files(request, req)],
                             **_letter_extras(account)})

    def missed_view(self, request, pk):
        """🔎 Пропущені відповіді: листи з ваших скриньок після надсилання запиту, ще не в листуванні рядків, схожі на
        відповідь (номер запиту, номер деталі з запиту, адреса чи назва фірми отримувача) — rfq_match.missed.
        Лише читання; прив'язати / «не відповідь» — action_view (link_msg / skip_msg)."""
        from suppliers.services import rfq_match
        req = self._request(request, pk, "change")
        rows = [{"msg": x["msg"].pk, "date": x["msg"].sent_at.strftime("%d.%m.%Y %H:%M") if x["msg"].sent_at else "",
                 "from": " ".join(v for v in (x["msg"].from_name, f"<{x['msg'].from_email}>") if v),
                 "subject": x["msg"].subject or "(без теми)", "reasons": x["reasons"], "rid": x["rid"],
                 "files": [str(a.get("name") or "") for a in (x["msg"].attachments or [])
                           if isinstance(a, dict) and not a.get("is_inline")][:6],
                 "url": f"/email/?msg={x['msg'].pk}"}
                for x in rfq_match.missed(req, request.user)]
        recipients = [{"rid": r.pk, "name": r.supplier.name, "email": r.email}
                      for r in req.recipients.exclude(state=SupplierRequestRecipient.PLANNED).select_related("supplier")]
        return JsonResponse({"items": rows, "recipients": recipients})

    def letters_view(self, request, pk):
        """👁 Перед «📤 Надіслати» на сторінці запиту: лист кожному, кому ще не надіслано (?rid= — лише йому), —
        тема, текст з привітанням, підпис скриньки, кому, копія, файли. Нічого не надсилає."""
        req = self._request(request, pk, "change")
        qs = req.recipients.filter(state__in=[SupplierRequestRecipient.PLANNED, SupplierRequestRecipient.FAILED]) \
            .select_related("supplier", "contact")
        if str(request.GET.get("rid") or "").isdigit():
            qs = qs.filter(pk=int(request.GET["rid"]))
        letters = []
        for r in qs:
            subject, body = rfq.render(req, r)
            letters.append({"rid": r.pk, "supplier": r.supplier.name, "to": r.email or "⚠️ немає email",
                            "contact": (r.contact.name if r.contact_id else ""), "cc": rfq.cc_for(req, r),
                            "subject": subject, "body": body, "failed": r.state == SupplierRequestRecipient.FAILED})
        account = rfq.account_for(request.user, req)
        return JsonResponse({"letters": letters, "files": [f.name for f in req.files.all()],
                             "empty_body": not (req.body or "").strip(), **_letter_extras(account)})

    def test_mail_view(self, request):
        """✉️ Тестовий лист собі / колезі: як отримає вибраний у «👁» постачальник (не вибрано — зразок
        rfq.SAMPLE_COMPANY), лише на адреси test_to (rfq.send_test) — без копії й без запису в запит і в картку
        постачальника. Вкладення — файли запиту (крім позначених «прибрати») і ще не збережені з форми."""
        from inventory.models import Supplier
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        req, draft, r = self._draft(request)
        if r is None:
            r = SupplierRequestRecipient(request=draft, supplier=Supplier(name=rfq.SAMPLE_COMPANY), email="")
        cap = rfq.ATTACH_MAX_MB * 1024 * 1024 + 1
        blobs = []
        try:
            for f in self._form_files(request, req):
                with f.file.open("rb") as fh:
                    blobs.append((f.name, fh.read(cap)))
            blobs += [(u.name, u.read(cap)) for u in request.FILES.getlist("files")[:20]]
            out = rfq.send_test(draft, r, request.user, request.POST.get("test_to", ""), rfq.as_attachments(blobs))
        except OSError as e:
            return JsonResponse({"error": f"Файл недоступний: {e}"}, status=400)
        except rfq.RFQError as e:
            return JsonResponse({"error": str(e)}, status=400)
        return JsonResponse(out)

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
        for f in reversed(self._form_files(request, req)):
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
                "subject": P.get("subject", "").strip()[:250], "body": P.get("body", "").strip(),
                "wish": "\n".join(" ".join(x.split()) for x in P.get("ai_wish", "").splitlines()
                                  if x.strip())[:ai.WISH_MAX]}
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
        req = SupplierRequest.objects.select_related("project", "category", "account", "owner", "copied_from").filter(pk=pk).first()
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
        for m in (SupplierMedia.objects.exclude(supplier=None).exclude(image="")      # 🔒 приватні — без фото
                  .filter(visibility=SupplierMedia.TEAM).order_by("-created_at")
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
        from suppliers.services import rfq_copy
        req = self._request(request, pk, "change") if pk else None
        if req is None and not self.has_add_permission(request):
            raise Http404
        if request.method == "POST":
            return self._save_form(request, req)
        # 📋 ?copy=<pk> — новий запит на основі іншого (rfq_copy.draft); нічого не створюється до «💾 Зберегти»
        src = (self._request(request, int(request.GET["copy"]), "view")
               if req is None and str(request.GET.get("copy") or "").isdigit() else None)
        copy, contact_pick = None, {}
        initial_supplier = request.GET.get("supplier")
        project = None
        if req is None and str(request.GET.get("project") or "").isdigit():
            from projects.models import Project
            project = Project.objects.filter(pk=int(request.GET["project"])).first()
        category = None
        if req is None and str(request.GET.get("category") or "").isdigit():
            category = SupplierCategory.objects.filter(pk=int(request.GET["category"])).first()
        if src is not None:
            req, copy = rfq_copy.draft(src, request.user)
            copy.update(rfq_copy.recipients(src))
            picked, contact_pick = set(copy["all"]), copy["contacts"]
        elif req is None:
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
            contact_pick = {s: (c or -1) for s, c in req.recipients.values_list("supplier_id", "contact_id")}
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
        files = list(req.files.all()) if req.pk else list(src.files.all()) if src is not None else []
        if src is not None:
            review = rfq_copy.initial(req, files)
        else:
            review = req.review if req.pk and req.status == SupplierRequest.DRAFT else {}
        review_items = rfq_copy.describe(review, req)
        rcp = self._recipients()
        for s in rcp["suppliers"]:      # кому піде лист: контакт з запиту (копії — зі старого), інакше основний
            s.f_contact = contact_pick.get(s.pk)
        copy_src = src or (req.copied_from if req.pk and req.copied_from_id else None)
        title = (f"📋 Новий запит на основі {src.code}" if src is not None else
                 f"✏️ {req.code}" if req.pk else "📨 Новий запит постачальникам")
        return TemplateResponse(request, "admin/suppliers/request_form.html", self._ctx(
            request, title, req=req, templates=templates,
            categories=SupplierCategory.objects.filter(is_active=True), projects=projects, groups=groups,
            picked=picked, sent=sent, accounts=accounts, placeholders=rfq.PLACEHOLDERS, **rcp,
            examples={k: {"subject": v[0], "body": v[1]} for k, v in rfq.EXAMPLES.items()},
            colleagues=self._colleagues(), test_to=accounts[0].email_address if accounts else request.user.email,
            example_body=rfq.EXAMPLES.get(req.language, rfq.EXAMPLES["en"])[1],
            langs=SupplierRequest.LANGS, files=files, notes=(req.specs or {}).get("_notes", ""),
            src=src, copy=copy, copy_src=copy_src, review_items=review_items,
            review_must=sum(1 for x in review_items if x["level"] == rfq_copy.MUST),
            review_data={x["key"]: {"value": x["value"], "level": x["level"], "label": x["label"], "why": x["why"]}
                         for x in review_items}))

    def _save_form(self, request, req):
        from inventory.models import Supplier
        from projects.models import Project
        from suppliers.services import rfq_copy
        new = req is None
        P = request.POST
        src = (self._request(request, int(P["copy_from"]), "view")
               if new and str(P.get("copy_from") or "").isdigit() else None)
        if new:
            req = SupplierRequest(owner=request.user, copied_from=src)
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
        if src is not None:     # 📋 файли старого запиту — крім «не брати»
            missing = rfq_copy.copy_files(src, req, request.user, skip=P.getlist("del_file"))
            if missing:
                self.message_user(request, "⚠️ Файлів старого запиту вже немає на диску (не скопійовано): "
                                  + ", ".join(missing), messages.WARNING)
        for upload in request.FILES.getlist("files")[:20]:
            try:
                rfq.add_file(req, upload, request.user)
            except rfq.RFQError as e:
                self.message_user(request, f"⚠️ {upload.name}: {e}", messages.ERROR)
        for fid in ([] if src is not None else P.getlist("del_file")):
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
        # 📋 що ще не перевірено після копіювання: значення те саме, що при копіюванні, і не «✓ так і лишити»
        if src is not None:     # файли — усі старого запиту: «не брати» щось = файли переглянуто (як у формі)
            base = rfq_copy.initial(rfq_copy.draft(src, request.user)[0], list(src.files.all()))
        else:
            base = req.review if req.status == SupplierRequest.DRAFT else {}
        reviewed = P.getlist("reviewed") + (list(base) if P.get("review_done") else [])
        req.review = rfq_copy.settle(base, req, reviewed) if base else {}
        req.save(update_fields=["body", "subject", "review"])
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
        from suppliers.models import SupplierReplyFile
        from suppliers.services import rfq_compare, rfq_copy, rfq_marks, rfq_replies, rfq_stats, rfq_usage
        req = self._request(request, pk)
        for r in req.recipients.exclude(thread=None, reply_message=None):
            try:   # 📎 вкладення відповідей, що вже на диску (з пошти — панель рядка)
                rfq_replies.collect(r)
            except Exception:  # noqa: BLE001
                pass
        cmp = rfq_compare.table(req)
        rows = cmp["rows"]
        n_files = dict(SupplierReplyFile.objects.filter(recipient__request=req).values_list("recipient")
                       .annotate(n=Count("pk")).values_list("recipient", "n"))
        for row in rows:
            r = row["recipient"]
            row["sent_link"] = r.sent_message_id if r.sent_message_id and r.sent_message and \
                r.sent_message.account.user_id == request.user.pk else None
            row["reply_link"] = r.reply_message_id if r.reply_message_id and r.reply_message and \
                r.reply_message.account.user_id == request.user.pk else None
            row["peek"] = rfq_replies.peek(r, request.user) if row["reply_link"] else None
            row["files_n"] = n_files.get(r.pk, 0)
            row["timing"] = rfq_stats.timing(r)              # ⏱ за скільки відповів, останній лист, листів
            row["ai_info"] = rfq_usage.row_info(r)           # 🧠 яка модель розібрала, скільки коштувало
            row["label"] = rfq_marks.view(r.label)          # 🏷 позначка рядка (правий клік)
            r.tiers_text = rfq_compare.tiers_text(r.tiers)          # форма ✏️
            r.one_time_text = rfq_compare.one_time_text(r.one_time)
        try:
            analysis_at = datetime.datetime.fromisoformat(str((req.analysis or {}).get("at") or ""))
        except ValueError:
            analysis_at = None
        # 🏆 порада зовнішнього агента (MCP, agent/tools_rfq.py) — той самий блок; посилання на завдання — лише власнику
        by_agent = (req.analysis or {}).get("source") == "agent"
        advice_task = None
        if by_agent and str((req.analysis or {}).get("task") or "").isdigit():
            from agent.models import AgentTask
            advice_task = AgentTask.objects.filter(pk=int(req.analysis["task"]), owner=request.user)                 .values_list("pk", flat=True).first()
        from inventory.models import Supplier
        in_req = {row["recipient"].supplier_id for row in rows}
        more = Supplier.objects.exclude(pk__in=in_req).exclude(status=Supplier.STATUS_BLOCKED)
        if req.category_id:
            more = more.filter(categories=req.category_id)
        more = _recipient_rows(more.prefetch_related("contacts", "categories").order_by("name")[:200])
        events = SupplierEvent.objects.filter(request=req).select_related("supplier", "user")[:40]
        return TemplateResponse(request, "admin/suppliers/request_work.html", self._ctx(
            request, f"📨 {req.code} · {req.title}", req=req, rows=rows, files=list(req.files.all()),
            overdue=rfq.waiting_overdue(req), can_change=self.has_change_permission(request, req),
            can_add=self.has_add_permission(request), review_left=rfq_copy.pending(req),
            review_must=", ".join(x["label"] for x in rfq_copy.pending(req) if x["level"] == rfq_copy.MUST),
            review_must_json=_js(", ".join(x["label"] for x in rfq_copy.pending(req) if x["level"] == rfq_copy.MUST)),
            letters_url_json=_js(reverse("admin:suppliers_supplierrequest_letters", args=[req.pk])),
            missed_url_json=_js(reverse("admin:suppliers_supplierrequest_missed", args=[req.pk])),
            act_json=_js(reverse("admin:suppliers_supplierrequest_action", args=[req.pk])),
            copies=list(req.copies.order_by("-created_at").values("pk", "code", "status")[:10]),
            states=[s for s in SupplierRequestRecipient.STATES if s[0] != SupplierRequestRecipient.PLANNED],
            more=more, more_mail=sum(1 for s in more if s.f_email), events=events, quantities=rfq.parse_quantities(req.quantities),
            cmp=cmp, qtys=cmp["qtys"], ncols=max(len(cmp["qtys"]), 1) + 7, stale=rfq_compare.stale(req),
            st=rfq_stats.stats(req, cmp), ai_sum=rfq_usage.summary(req, rows),
            marks_json=_js({"colors": [{"key": k, "icon": i, "name": n, "hex": h} for k, i, n, h in rfq_marks.COLORS],
                            "stickers": [{"key": k, "icon": i, "name": n} for k, i, n in rfq_marks.STICKERS],
                            "text_max": rfq_marks.TEXT_MAX}),
            letters_pending=SupplierRequestLetter.objects.filter(request=req, user=request.user, sent_at=None).count(),
            chat_url_json=_js(reverse("admin:suppliers_supplierrequest_chat", args=[req.pk, 0])),
            colleagues=self._colleagues(),
            analysis_at=analysis_at, by_agent=by_agent, advice_task=advice_task,
            reparse=len(self._to_parse(req, False)), scope_choices=rfq_compare.SCOPE,
            planned=sum(1 for row in rows if row["recipient"].state in (SupplierRequestRecipient.PLANNED,
                                                                          SupplierRequestRecipient.FAILED))))

    def _talk(self, request, req, r, do):
        """💬 / ↪ — JSON: 🧭 Коротко (у фоні), відповідь у гілку, план і надсилання пересилання колезі."""
        from suppliers.services import ai, jobs, rfq_talk
        user = request.user
        P = request.POST
        try:
            if do == "chat_brief":
                if r is None:
                    return JsonResponse({"error": "Немає постачальника."}, status=400)
                model = ai.model_of(P.get("model"))

                def run():
                    rfq_talk.brief(req, r, user, model=model)
                    return {"message": "🧭 Готово"}
                return JsonResponse({"job": jobs.start(user, f"brief {r.pk}", run)})
            if do == "chat_ai":                      # 💬 ✍️ написати / ✨ покращити — у фоні
                if r is None:
                    return JsonResponse({"error": "Немає постачальника."}, status=400)
                model, mode = ai.model_of(P.get("model")), "improve" if P.get("mode") == "improve" else "write"
                wish, text = P.get("wish", ""), P.get("text", "")
                if mode == "write" and not wish.strip():
                    return JsonResponse({"error": "Напишіть, що відповісти."}, status=400)
                if mode == "improve" and not text.strip():
                    return JsonResponse({"error": "Спершу напишіть текст."}, status=400)

                def run():
                    out = rfq_talk.ai_reply(req, r, user, wish=wish, text=text, mode=mode, model=model)
                    return {**out, "message": f"🧠 {out['model']} · ${out['usd']:.3f}"}
                return JsonResponse({"job": jobs.start(user, f"chat-ai {r.pk}", run)})
            if do == "chat_send":
                if r is None:
                    return JsonResponse({"error": "Немає постачальника."}, status=400)
                out = rfq_talk.reply(req, r, user, P.get("to", ""), P.get("cc", ""), P.get("subject", ""),
                                     P.get("body", ""), reply_pk=P.get("reply") if str(P.get("reply") or "").isdigit()
                                     else None)
                return JsonResponse(out)
            rids = P.getlist("rid") or str(P.get("rids") or "").split(",")
            if do == "each_compose":                 # ✉️ лист кожному — складаємо у фоні (AI по черзі)
                mode = "template" if P.get("mode") == "template" else "ai"
                model, wish = ai.model_of(P.get("model")), P.get("wish", "")
                if not wish.strip():
                    return JsonResponse({"error": "Напишіть, що сказати постачальникам."}, status=400)

                def run(report):
                    from suppliers.services import rfq_usage
                    out = rfq_talk.compose_each(req, rids, user, wish, mode=mode, model=model, report=report)
                    usd = sum(x.get("usd") or 0 for x in out)
                    ai_note = f" · {rfq_usage.label(model)} · ${usd:.3f}" if mode != "template" else " · без AI"
                    return {"letters": out, "message": f"✍️ Листів: {len(out)}{ai_note}", "ai_note": ai_note}
                return JsonResponse({"job": jobs.start(user, f"each {req.pk}", run, progress=True)})
            if do in ("each_save", "each_load"):     # 📝 збережені листи: автозбереження правок / відкрити знову
                import json as _json
                if do == "each_save":
                    try:
                        items = _json.loads(P.get("letters") or "[]")
                    except ValueError:
                        items = []
                    return JsonResponse({"saved": rfq_talk.update_letters(req, user, items if isinstance(items, list)
                                                                          else [])})
                return JsonResponse(rfq_talk.saved_letters(req, user))
            if do == "each_send":                    # 📤 кожному окремо, у його гілку
                import json as _json
                try:
                    items = _json.loads(P.get("letters") or "[]")
                except ValueError:
                    return JsonResponse({"error": "Незрозумілі листи — складіть знову."}, status=400)
                if not isinstance(items, list) or not items:
                    return JsonResponse({"error": "Немає листів для надсилання."}, status=400)

                def run(report):
                    out = rfq_talk.send_each(req, user, items, report=report)
                    return {**out, "message": f"📤 Надіслано: {len(out['sent'])}"}
                return JsonResponse({"job": jobs.start(user, f"each-send {req.pk}", run, progress=True)})
            if do == "forward_plan":
                return JsonResponse(rfq_talk.forward_plan(req, rids, user))
            out = rfq_talk.forward(req, rids, user, P.get("to", ""), P.get("cc", ""), P.get("subject", ""),
                                   P.get("note", ""), P.getlist("part"))
            return JsonResponse(out)
        except (rfq_talk.TalkError, rfq.RFQError, ai.AIError) as e:
            return JsonResponse({"error": str(e)}, status=400)

    @staticmethod
    def _to_parse(req, overwrite) -> list:
        """🧠 Кого розібрати разом: є відповідь, ще не розібрано (або розібрано до разових витрат і «що входить»);
        overwrite — усіх, хто відповів."""
        from suppliers.services import ai
        R = SupplierRequestRecipient
        out = []
        for r in req.recipients.exclude(thread=None, reply_message=None).filter(state__in=[R.REPLIED, R.QUOTED]):
            if overwrite or not (r.ai or {}).get("summary") or (r.ai or {}).get("v") != ai.QUOTE_VERSION or _new_mail(r):
                out.append(r.pk)
        return out

    def panel_view(self, request, pk, rid):
        """Панель праворуч: усе про пропозицію одного постачальника — ціни, вкладення, листування, історія.
        ?fetch=1 — вкладення, яких ще немає на диску, забрати з пошти (IMAP)."""
        from suppliers.services import rfq_replies
        req = self._request(request, pk)
        r = (req.recipients.select_related("supplier", "contact", "reply_message__account", "sent_message__account")
             .filter(pk=rid).first())
        if r is None:
            raise Http404
        try:
            out = rfq_replies.collect(r, fetch=request.GET.get("fetch") == "1")
        except Exception as e:  # noqa: BLE001
            out = {"saved": 0, "waiting": 0, "error": str(e)[:200]}
        row = next((x for x in rfq.comparison(req) if x["recipient"].pk == r.pk), {})
        order = list(req.recipients.order_by("pk").values_list("pk", flat=True))
        return TemplateResponse(request, "admin/suppliers/_request_panel.html", {
            **rfq_replies.panel(req, r, request.user, row), "req": req, "out": out, "order": order,
            "can_change": self.has_change_permission(request, req),
            "quantities": rfq.parse_quantities(req.quantities),
            "act": reverse("admin:suppliers_supplierrequest_action", args=[req.pk])})

    def chat_view(self, request, pk, rid):
        """💬 Вікно листування з одним постачальником: листи рядка (текст — лише з власних скриньок), кому
        відповідати, «🧭 Коротко» (збережене), коротка історія."""
        from suppliers.services import rfq_stats, rfq_talk
        req = self._request(request, pk)
        r = req.recipients.select_related("supplier", "contact", "reply_message__account", "sent_message__account") \
            .filter(pk=rid).first()
        if r is None:
            raise Http404
        items = rfq_talk.letters(r, request.user)
        tm = rfq_stats.timing(r, [x["m"] for x in items])
        draft = SupplierRequestLetter.objects.filter(recipient=r, user=request.user, sent_at=None).first()
        target = rfq_talk.target(req, r, items, request.user)
        if draft is not None and draft.body.strip():        # 📝 збережений лист «Написати кожному» — у поле відповіді
            target.update(to=draft.to or target["to"], cc=draft.cc, subject=draft.subject or target["subject"],
                          reply_pk=draft.reply_message_id or target["reply_pk"])
        return TemplateResponse(request, "admin/suppliers/_request_chat.html", {
            "req": req, "r": r, "letters": items, "target": target, "draft": draft,
            "history": rfq_talk.history(req, r, tm), "tm": tm, "brief": rfq_talk.brief_cached(request.user, r, items),
            "hidden": sum(1 for x in items if not x["readable"]), "readable": sum(1 for x in items if x["readable"]),
            "can_change": self.has_change_permission(request, req)})

    def chat_file_view(self, request, pk, rid, msg, idx):
        """📎 Вкладення листа з листування рядка: лише лист цього рядка й лише з власної скриньки людини (файл
        постачальника, уже збережений у запиті, — посилання на нього дає сам рядок). Сторінка «👁 Перегляд»
        (core/file_preview.py): PDF, картинки, Word, Excel, текст, архіви; ?raw=1 — сам файл, ?download=1 — завантажити."""
        from email_assistant.attachments import attachment_bytes
        from email_assistant.models import EmailMessage
        from suppliers.services import rfq_replies
        req = self._request(request, pk)
        r = req.recipients.filter(pk=rid).first()
        if r is None or msg not in {m.pk for m in rfq_replies.mail(r)}:
            raise Http404
        m = EmailMessage.objects.select_related("account").filter(pk=msg, account__user=request.user).first()
        atts = (m.attachments or []) if m is not None else []
        if m is None or idx >= len(atts) or not isinstance(atts[idx], dict):
            raise Http404
        try:
            data = attachment_bytes(m.account, m, idx)
        except Exception:  # noqa: BLE001 — пошта недоступна
            data = None
        if not data:
            return HttpResponse("Файл не знайдено — пошта не віддала вкладення. Відкрийте лист у пошті.", status=404)
        return self._file_page(request, req, atts[idx].get("name") or "attachment", data)

    def file_view(self, request, pk, src, fid):
        """👁 Файл запиту в Minerva (core/file_preview.py): src reply — вкладення постачальника, збережене в рядку цього
        запиту (SupplierReplyFile), req — файл нашого запиту (SupplierRequestFile). Право — перегляд запиту."""
        from suppliers.models import SupplierReplyFile
        req = self._request(request, pk)
        f = (SupplierReplyFile.objects.filter(pk=fid, recipient__request=req).first() if src == "reply" else
             req.files.filter(pk=fid).first() if src == "req" else None)
        if f is None or not f.file:
            raise Http404
        try:
            with f.file.open("rb") as fh:
                data = fh.read()
        except OSError:
            return self._file_page(request, req, f.name, b"",
                                   error="Файлу немає на сервері — можливо, його видалили. Відкрийте лист у пошті.")
        return self._file_page(request, req, f.name, data)

    def _file_page(self, request, req, name, data, error=""):
        from core import file_preview
        return file_preview.respond(request, name, data, context=self.admin_site.each_context(request),
                                    back=reverse("admin:suppliers_supplierrequest_work", args=[req.pk]),
                                    back_label=req.code, error=error)

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
                    from suppliers.services import rfq_usage
                    data = ai.quote(r, user=user, model=model)
                    rfq_usage.add(req, "parse", data.get("model"), data.get("cost_usd"))
                    rfq.apply_ai(r, data, overwrite=bool(request.POST.get("overwrite")))
                    return {"url": back, "message": f"🧠 Розібрано · {rfq_usage.label(data.get('model'))} · "
                                                    f"${float(data.get('cost_usd') or 0):.3f}"}
                return JsonResponse({"job": jobs.start(user, f"quote {r.pk}", run)})
            elif do == "ai_quote_all":      # 🧠 усі відповіді разом — по черзі, у фоні
                from suppliers.services import ai as ai_mod
                model, user = ai.model_of(request.POST.get("model")), request.user
                over = bool(request.POST.get("overwrite"))
                ids = self._to_parse(req, over)
                if not ids:
                    return JsonResponse({"error": "Нема що розбирати — усі відповіді вже прочитано."}, status=400)

                def run(report):
                    done, priced, failed, spent = 0, 0, [], [0.0]
                    for i, rid in enumerate(ids, 1):
                        rr = SupplierRequestRecipient.objects.select_related("supplier", "request__account").get(pk=rid)
                        report(f"🧠 {i} / {len(ids)} · {rr.supplier.name}")
                        try:
                            data = ai_mod.quote(rr, user=user, model=model)
                            from suppliers.services import rfq_usage
                            rfq_usage.add(req, "parse", data.get("model"), data.get("cost_usd"))
                            spent[0] += float(data.get("cost_usd") or 0)
                            rfq.apply_ai(rr, data, overwrite=over)
                        except Exception as e:  # noqa: BLE001 — один не вийшов, решта йдуть
                            failed.append(f"{rr.supplier.name}: {str(e)[:120]}")
                            continue
                        done += 1
                        priced += bool(data.get("tiers"))
                    from suppliers.services import rfq_usage
                    msg = (f"🧠 Розібрано {done}: з цінами {priced}, без цін {done - priced} · "
                           f"{rfq_usage.label(model)} · ${spent[0]:.3f}")
                    if failed:
                        msg += " · не вдалося: " + "; ".join(failed)
                    return {"url": back, "message": msg}
                return JsonResponse({"job": jobs.start(user, f"quotes {req.pk}", run, progress=True)})
            elif do == "mark":                    # 🏷 позначка рядків: колір «№», наліпки, свій текст
                from suppliers.services import rfq_marks
                op = {}
                if "color" in request.POST:
                    op["color"] = request.POST.get("color", "")
                if request.POST.get("sticker"):
                    op.update(sticker=request.POST["sticker"], on=request.POST.get("on") == "1")
                if "text" in request.POST:
                    op["text"] = request.POST.get("text", "")
                if request.POST.get("clear"):
                    op["clear"] = True
                rids = request.POST.getlist("rid") or str(request.POST.get("rids") or "").split(",")
                return JsonResponse({"labels": {str(k): v for k, v in
                                                rfq_marks.apply(req, rids, request.user, op).items()}})
            elif do in ("chat_brief", "chat_send", "chat_ai", "forward_plan", "forward_send", "each_compose",
                        "each_send", "each_save", "each_load"):
                return self._talk(request, req, r, do)
            elif do == "compare_ai":
                from suppliers.services import rfq_compare
                model, user = ai.model_of(request.POST.get("model")), request.user

                def run():
                    from suppliers.services import rfq_usage
                    data = ai.compare(req, user=user, model=model)
                    rfq_usage.add(req, "compare", model, data.get("cost_usd"))
                    SupplierRequest.objects.filter(pk=req.pk).update(analysis={
                        **data, "at": timezone.now().isoformat(), "fp": rfq_compare.fingerprint(req)})
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
            elif do in ("link_msg", "skip_msg"):          # 🔎 пропущена відповідь: прив'язати / «не відповідь»
                from email_assistant.models import EmailMessage
                from suppliers.services import rfq_match
                mid = request.POST.get("msg")
                msg = EmailMessage.objects.filter(pk=int(mid) if str(mid or "").isdigit() else 0,
                                                  account__user=request.user).first()   # лише зі своїх скриньок
                if msg is None:
                    return JsonResponse({"error": "Листа немає у ваших скриньках."}, status=404)
                if do == "skip_msg":
                    rfq_match.skip(req, msg.pk)
                    return JsonResponse({"ok": True})
                rr = req.recipients.select_related("supplier", "request").filter(
                    pk=int(request.POST.get("rid")) if str(request.POST.get("rid") or "").isdigit() else 0).first()
                if rr is None:
                    return JsonResponse({"error": "Виберіть постачальника."}, status=400)
                done = rfq_match.link(rr, msg, request.user)
                return JsonResponse({"ok": True, "linked": done, "supplier": rr.supplier.name})
            elif do == "add":
                from inventory.models import Supplier
                ids = [int(x) for x in request.POST.getlist("rcp") if str(x).isdigit()]
                sups = list(Supplier.objects.filter(pk__in=ids).prefetch_related("contacts"))
                contacts = {}                 # кому саме з контактів фірми (вибір у рядку)
                for s in sups:
                    cid = request.POST.get(f"contact_{s.pk}")
                    if str(cid or "").isdigit():
                        c = next((c for c in s.contacts.all() if c.pk == int(cid) and c.email), None)
                        if c is not None:
                            contacts[s.pk] = c
                out = rfq.add_recipients(req, sups, contacts)
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
            elif do == "review_done":       # 📋 після копіювання: усе перевірено, підсвітку зняти
                SupplierRequest.objects.filter(pk=req.pk).update(review={})
                self.message_user(request, "✓ Усе перевірено", messages.SUCCESS)
            elif do == "decision":
                SupplierRequest.objects.filter(pk=req.pk).update(decision=request.POST.get("decision", "")[:4000])
        except rfq.RFQError as e:
            self.message_user(request, f"⚠️ {e}", messages.ERROR)
        return redirect(back)
