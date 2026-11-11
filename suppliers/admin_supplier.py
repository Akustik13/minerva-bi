"""
suppliers/admin_supplier.py — 🏭 картка постачальника (inventory.SupplierAdmin) розширена:

  👥 контактні особи (inline), 🗂 категорії й стан, 📊 огляд (закупівлі, строки, документи по закупівлях, товари,
  запити, оцінка Minerva), 📷 візитки й флаєри, 📨 запити (RFQ), 🧠 AI / 🌐 з інтернету, 🕘 історія «звідки все пішло»;
  сторінки: ✉️ аналіз пошти за період, 🏭 постачальник з листа, 🔎 постачальники з пошти, 📷 візитка / флаєр (камера).

Права — як у картки постачальника (розділ «Склад»): перегляд — view_supplier, створення — add_supplier, зміни,
AI й візитки — change_supplier. Пошта — лише власні скриньки людини.
"""
import datetime
import json

from django import forms
from django.contrib import admin, messages
from django.db.models import Count, Max, Q
from django.http import Http404, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from suppliers.models import (SupplierAnalysis, SupplierCardPreset, SupplierCategory, SupplierContact, SupplierEvent,
                              SupplierMedia)

VIEWER = "_sup_viewer"
PANELS = ("sup_overview", "sup_mail", "sup_media", "sup_requests", "sup_ai", "sup_history")


def _money(m) -> str:
    from sales.revenue import fmt
    parts = [fmt(m.total, m.home)] if m.total else []
    parts += [f"{v:,.0f} {k} без курсу".replace(",", " ") for k, v in m.missing.items()]
    return " + ".join(parts) or "—"


def _qty(d) -> str:
    """Decimal('100.000') → «100», Decimal('2.500') → «2.5»."""
    return str(int(d)) if d == d.to_integral_value() else format(d.normalize(), "f")


def _qr_kind(kind) -> str:
    names = {"vcard": "vCard", "mecard": "MeCard", "url": "посилання", "whatsapp": "WhatsApp", "email": "email",
             "phone": "телефон", "wechat": "WeChat", "telegram": "Telegram", "linkedin": "LinkedIn", "skype": "Skype",
             "line": "LINE", "social": "соцмережа"}
    return names.get(kind, "посилання" if kind else "")


def _stars(n) -> str:
    return "★" * n + "☆" * (5 - n) if n else ""


_PO_OPTS_JS = mark_safe(
    "<script>(function(){var K='sp-po-opts',s={};try{s=JSON.parse(localStorage.getItem(K)||'{}')}catch(e){}"
    "document.querySelectorAll('input.sp-po-opt').forEach(function(el){if(el.dataset.spDone)return;"
    "el.dataset.spDone='1';var k=el.getAttribute('data-k');"
    "if(Object.prototype.hasOwnProperty.call(s,k))el.checked=!!s[k];"
    "el.addEventListener('change',function(){s[k]=el.checked;"
    "try{localStorage.setItem(K,JSON.stringify(s))}catch(e){}});});})();</script>")


# у формі картки Django ставить кожному <label> ширину 160 px і float — галочкам це не потрібно
_LABEL_CSS = "width:auto;min-width:0;float:none;display:inline;padding:0;font-weight:normal"


def po_options(count=None):
    """Галочки «🛒 одразу завести закупівлі з тем з PI» і «📜 як історію» для збору історії з пошти (картка
    постачальника, «🏭 Постачальник з листа»); вибір людини браузер пам'ятає (localStorage sp-po-opts)."""
    return format_html(
        '<span class="sp-po-opts" style="display:flex;flex-wrap:wrap;gap:2px 14px;flex-basis:100%">'
        '<label class="sp-muted" style="{}" title="{}"><input type="checkbox" name="make_po" value="1" '
        'class="sp-po-opt" data-k="make"> 🛒 {}{}</label> <label class="sp-muted" style="{}" title="{}">'
        '<input type="checkbox" name="po_history" value="1" class="sp-po-opt" data-k="hist" checked> 📜 {}</label>'
        '</span>{}', _LABEL_CSS,
        "Для кожної теми, де від них є PI / рахунок або підтвердження, а закупівлі ще немає",
        "одразу завести закупівлі з тем з PI / рахунком", f" (зараз {count})" if count else "", _LABEL_CSS,
        "Закупівля в стані «Отримано» без позицій: склад і «Очікується» не змінюються",
        "як історію — уже отримано, без складу", _PO_OPTS_JS)


def _can(request, perm):
    return request.user.has_perm(f"inventory.{perm}_supplier")


class SupplierContactInline(admin.TabularInline):
    model = SupplierContact
    extra = 0
    fields = ("is_primary", "name", "position", "email", "phone", "mobile", "notes", "is_active", "source")
    readonly_fields = ("source",)
    verbose_name = "контактна особа"
    verbose_name_plural = "👥 Контактні особи"
    formfield_overrides = {}

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "notes":
            kwargs["widget"] = forms.Textarea(attrs={"rows": 1, "cols": 28})
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    # права — як у самої картки постачальника (розділ «Склад»), а не окремого розділу «Постачальники»
    def has_view_permission(self, request, obj=None):
        return _can(request, "view") or _can(request, "change")

    def has_add_permission(self, request, obj=None):
        return _can(request, "change")

    def has_change_permission(self, request, obj=None):
        return _can(request, "change")

    def has_delete_permission(self, request, obj=None):
        return _can(request, "change")


class CategoryFilter(admin.SimpleListFilter):
    title = "категорія"
    parameter_name = "cat"

    def lookups(self, request, model_admin):
        return [("none", "— без категорії")] + [(str(c.pk), c.path if c.parent_id else str(c))
                                                 for c in SupplierCategory.objects.filter(is_active=True)]

    def queryset(self, request, queryset):
        v = self.value()
        if v == "none":
            return queryset.filter(categories=None)
        if v and v.isdigit():
            ids = {int(v)} | set(SupplierCategory.objects.filter(parent_id=int(v)).values_list("pk", flat=True))
            return queryset.filter(categories__in=ids).distinct()
        return queryset


class SupplierCardMixin:
    """Підмішується в inventory.admin.SupplierAdmin."""
    inlines = (SupplierContactInline,)

    # ── список ──
    def get_queryset(self, request):
        return (super().get_queryset(request).prefetch_related("categories")
                .annotate(_po_count=Count("purchase_orders", filter=~Q(purchase_orders__status="cancelled"),
                                          distinct=True),
                          _po_last=Max("purchase_orders__order_date")))

    @admin.display(description="Категорії")
    def categories_col(self, obj):
        cats = list(obj.categories.all())
        if not cats:
            return "—"
        return format_html_join(" ", '<span style="white-space:nowrap;border:1px solid {};border-radius:10px;'
                                     'padding:0 6px;font-size:11px">{} {}</span>',
                                ((c.color or "#888", c.icon, c.name) for c in cats))

    @admin.display(description="Закупівель", ordering="_po_count")
    def po_col(self, obj):
        n = getattr(obj, "_po_count", 0)
        if not n:
            return "—"
        url = reverse("admin:inventory_purchaseorder_changelist") + f"?supplier__id__exact={obj.pk}"
        last = getattr(obj, "_po_last", None)
        return format_html('<a href="{}">{}</a>{}', url, n,
                           format_html(' <span style="color:var(--text-muted);font-size:11px">· {}</span>',
                                       last.strftime("%d.%m.%Y")) if last else "")

    @admin.display(description="Стан", ordering="status")
    def status_col(self, obj):
        return obj.get_status_display()

    @admin.display(description="Оцінка", ordering="rating")
    def rating_col(self, obj):
        return _stars(obj.rating) if obj.rating else ""

    def changelist_view(self, request, extra_context=None):
        extra = {"sup_tools": [
            (reverse("admin:inventory_supplier_find_in_mail"), "🔎 Знайти постачальників у пошті"),
            (reverse("admin:inventory_supplier_card"), "📷 Візитка / флаєр"),
            (reverse("admin:inventory_supplier_cards"), "🪪 Усі візитки"),
        ]}
        if request.user.has_perm("suppliers.view_supplierrequest"):
            extra["sup_tools"].append((reverse("admin:suppliers_supplierrequest_changelist"), "📨 Запити (RFQ)"))
        if request.user.has_perm("suppliers.view_suppliercategory"):
            extra["sup_tools"].append((reverse("admin:suppliers_suppliercategory_changelist"), "🗂 Категорії"))
        return super().changelist_view(request, {**(extra_context or {}), **extra})

    # ── картка ──
    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "categories":
            kwargs["widget"] = forms.CheckboxSelectMultiple
            kwargs["queryset"] = SupplierCategory.objects.filter(is_active=True)
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def get_object(self, request, object_id, from_field=None):
        obj = super().get_object(request, object_id, from_field)
        if obj is not None:
            setattr(obj, VIEWER, request.user)
        return obj

    def get_readonly_fields(self, request, obj=None):
        fields = tuple(super().get_readonly_fields(request, obj))
        return fields + tuple(f for f in PANELS + ("source", "created_at") if f not in fields)

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj is not None and obj.pk:
            return fieldsets
        out = []   # нова картка — без огляду, візиток, історії (ще порожні)
        for name, opts in fieldsets:
            fields = [f for f in opts.get("fields", ()) if not (isinstance(f, str) and f in PANELS)
                      and not (isinstance(f, (tuple, list)) and set(f) <= {"source", "created_at"})]
            if fields:
                out.append((name, {**opts, "fields": fields}))
        return out

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            SupplierEvent.objects.create(supplier=obj, kind=SupplierEvent.CREATED, user=request.user,
                                         text="Створено вручну")

    def _viewer(self, obj):
        return getattr(obj, VIEWER, None)

    @admin.display(description="Огляд")
    def sup_overview(self, obj):
        if not obj or not obj.pk:
            return "—"
        from suppliers.services.overview import purchases, requests_stats, score
        viewer = self._viewer(obj)
        p = purchases(obj)
        rfq = requests_stats(obj)
        mail = None
        last_mail = SupplierAnalysis.objects.filter(supplier=obj, kind=SupplierAnalysis.MAIL, owner=viewer).first() \
            if viewer else None
        if last_mail:
            mail = last_mail.result
        sc = score(p, mail, rfq)
        tiles = [
            ("Закупівель", f"{p['count']}" + (f" · відкритих {p['open']}" if p["open"] else "")),
            ("Витрачено", _money(p["spend"])),
            ("Перша / остання", f"{p['first']:%d.%m.%Y} – {p['last']:%d.%m.%Y}" if p["last"] else "—"),
            ("Від замовлення до отримання", f"медіана {p['median_days']} дн." if p["median_days"] is not None else "—"),
            ("Вчасно", f"{p['on_time']} з {p['on_time'] + p['late']}" if (p['on_time'] + p['late']) else "—"),
            ("Документів закупівель", str(p["documents"])),
            ("Запити (RFQ)", f"{rfq['total']} · відповіли {rfq['replied']} · вибрано {rfq['chosen']}"
             if rfq["total"] else "—"),
        ]
        if mail:
            tiles.append(("Відповідають на листи", f"медіана {mail.get('their_reply_hours')} год"
                          if mail.get("their_reply_hours") is not None else "—"))
        tiles_html = format_html_join("", '<div class="sp-tile"><div class="sp-k">{}</div><div class="sp-v">{}</div></div>',
                                      tiles)
        if sc["value"] is not None:
            parts = format_html_join(" · ", "{} {}% ({})", ((n, v, w) for n, v, w in sc["parts"]))
            score_html = format_html('<div class="sp-score"><b>Оцінка Minerva: {}/100</b> <span title="{}">{}</span>'
                                     '<div class="sp-muted">{}</div></div>', sc["value"],
                                     "Рахує Minerva з закупівель, запитів і аналізу вашої пошти", _stars(sc["stars"]),
                                     parts)
        else:
            score_html = format_html('<div class="sp-muted">{}</div>',
                                     "Оцінки Minerva ще немає: немає отриманих закупівель з датами, запитів і аналізу пошти.")
        orders = ""
        if p["orders"]:
            rows = []
            for po in p["orders"]:
                docs = format_html_join(" ", '<a href="{}" target="_blank" title="{}">📎 {}</a>',
                                        ((d.file.url, d.get_category_display(), (d.title or d.file.name.rsplit("/", 1)[-1])[:40])
                                         for d in po.documents.all() if d.file))
                rows.append(format_html(
                    '<tr><td><a href="{}" translate="no">{}</a></td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>',
                    reverse("admin:inventory_purchaseorder_change", args=[po.pk]), po.code or f"#{po.pk}",
                    po.order_date.strftime("%d.%m.%Y") if po.order_date else "", po.get_status_display(),
                    f"{po.total_price:,.2f} {po.currency}".replace(",", " ") if po.total_price is not None else "",
                    docs or "—"))
            orders = format_html('<h4 class="sp-h">🗒️ Закупівлі й документи по них</h4><table class="sp-table"><tr>'
                                 '<th>Закупівля</th><th>Дата</th><th>Стан</th><th>Сума</th><th>Документи</th></tr>{}</table>',
                                 mark_safe("".join(rows)))
        products = ""
        if p["products"]:
            prow = format_html_join("", '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>', (
                (format_html('<a href="{}" translate="no">{}</a>', reverse("admin:inventory_product_change",
                                                                          args=[r["product"].pk]), r["product"].sku)
                 if r["product"] else r["name"][:60], _qty(r["qty"]), r["orders"],
                 f"{r['last_price']} {r['currency']}" if r["last_price"] is not None else "—") for r in p["products"]))
            products = format_html('<h4 class="sp-h">🔩 Що в них купували ({} позицій)</h4><table class="sp-table"><tr>'
                                   '<th>Товар</th><th>Штук разом</th><th>Закупівель</th><th>Остання ціна</th></tr>{}</table>',
                                   p["products_total"], prow)
        late = ""
        if p["late_list"]:
            late = format_html('<div class="sp-warn">⚠️ Із запізненням: {}</div>', format_html_join(
                ", ", '<a href="{}" translate="no">{}</a> +{} дн.{}',
                ((reverse("admin:inventory_purchaseorder_change", args=[x["po"].pk]), x["po"].code, x["days"],
                  " (ще не отримано)" if x.get("open") else "") for x in p["late_list"])))
        buttons = [(reverse("admin:inventory_supplier_analysis", args=[obj.pk]), "✉️ Аналіз пошти з цим постачальником")]
        buttons.append((reverse("admin:inventory_purchaseorder_add") + f"?supplier={obj.pk}", "🛒 Нова закупівля"))
        if viewer is not None and viewer.has_perm("suppliers.add_supplierrequest"):
            buttons.append((reverse("admin:suppliers_supplierrequest_new") + f"?supplier={obj.pk}", "📨 Запит цьому постачальнику"))
        btn = format_html_join(" ", '<a class="button" href="{}">{}</a>', buttons)
        return format_html('{}<div class="sp-tiles">{}</div>{}<div class="sp-row">{}</div>{}{}',
                           _STYLE, tiles_html, score_html, btn, late, format_html("{}{}", orders, products))

    @admin.display(description="Листування й файли")
    def sup_mail(self, obj):
        """🧵 Теми листування з вашої пошти: етап, період, файли (без картинок з підписів), 🛒 закупівля з PI."""
        if not obj or not obj.pk:
            return "—"
        from suppliers.services import history
        viewer = self._viewer(obj)
        data = history.topics_for(obj, viewer) if viewer is not None else None
        url = reverse("admin:inventory_supplier_history", args=[obj.pk])
        can_po = viewer is not None and viewer.has_perm("inventory.add_purchaseorder")
        opts = po_options(sum(1 for t in (data or {}).get("topics", []) if history.can_purchase(t))) if can_po else ""
        rebuild = format_html('<button type="submit" class="button" name="sup_action" value="rebuild" formaction="{}" '
                              'formmethod="post" formnovalidate>🔄 {}</button>', url,
                              "Оновити з пошти" if data else f"Зібрати історію з пошти (за {history.YEARS} років)")
        if not data:
            return format_html('<div class="sp-muted">{}</div><div class="sp-row">{}{}</div>',
                               "Minerva перечитає ваші скриньки й складе листування з постачальником за темами: запити, "
                               "пропозиції, PI / рахунки, файли (Gerber, BOM, Quotation…) — і запише віхи в «🕘 Історію».",
                               rebuild, opts)
        topics = data["topics"]
        work = [t for t in topics if t["stage"] != "marketing"]
        ads = [t for t in topics if t["stage"] == "marketing"]
        rows = []
        for t in work:
            seen, chips = set(), []
            for d in t["docs"]:
                if d["name"] in seen:
                    continue
                seen.add(d["name"])
                chips.append(format_html(
                    '<a class="sp-chip" href="{}" target="_blank" title="{} · {}" translate="no">{} {}</a>',
                    reverse("admin:inventory_supplier_attachment", args=[d["msg"], d["i"]]),
                    history.KIND_NAMES.get(d["kind"], ""), "від них" if d.get("theirs") else "наш",
                    history.KIND_NAMES.get(d["kind"], "📎").split(" ")[0], d["name"][:42]))
            action = ""
            if t.get("po"):
                action = format_html('<a href="{}" translate="no">🗒️ {}</a>{}',
                                     reverse("admin:inventory_purchaseorder_change", args=[t["po"]]), t.get("po_code"),
                                     format_html(' <span title="{}">📜</span>', "Заведено як історію — без складу")
                                     if t.get("po_history") else "")
            elif history.can_purchase(t) and can_po:
                action = format_html(
                    '<button type="submit" class="button" name="topic" value="{}" formaction="{}?action=po" '
                    'formmethod="post" formnovalidate title="{}">🛒 Завести закупівлю</button>'
                    '<label class="sp-muted" style="{};display:block;white-space:nowrap" title="{}">'
                    '<input type="checkbox" name="hist" value="{}"{}> 📜 як історію</label>', t["key"], url,
                    "Закупівля з датою й номером з PI, документи теми — у документи закупівлі", _LABEL_CSS,
                    "Уже отримано: закупівля в стані «Отримано» без позицій — склад і «Очікується» не змінюються",
                    t["key"], mark_safe(" checked") if history.history_default(t) else "")
            first, last = (t["first"] or "")[:10], (t["last"] or "")[:10]
            period = ".".join(reversed(first.split("-"))) + (
                " – " + ".".join(reversed(last.split("-"))) if last and last != first else "")
            rows.append(format_html(
                '<tr><td>{}</td><td><a href="/email/?msg={}" target="_blank" translate="no">{}</a>{}'
                '<div class="sp-chips">{}</div></td><td style="white-space:nowrap">{}<div class="sp-muted">↓ {} · ↑ {}</div>'
                '</td><td>{}</td></tr>',
                history.STAGES.get(t["stage"], ""), t["last_msg"], t["title"][:110],
                format_html(' <span class="sp-warn" title="{}">⏳</span>', "Останній лист — від них, ми ще не відповіли")
                if t.get("waiting_us") else "", mark_safe(" ".join(str(c) for c in chips)) or "—",
                period, t["incoming"], t["outgoing"], action))
        ads_html = format_html('<div class="sp-muted">📣 Розсилки від них (без наших відповідей): {}</div>',
                               ", ".join(f"«{t['title'][:50]}»" for t in ads[:6])) if ads else ""
        return format_html(
            '<div class="sp-muted">З вашої пошти · {} · оновлено {} · тем {}</div>'
            '<table class="sp-table"><tr><th>Етап</th><th>Тема й файли</th><th>Період</th><th></th></tr>{}</table>{}'
            '<div class="sp-row">{} <a class="button" href="{}">✉️ Детально й 🧠 AI-оцінка</a>{}</div>',
            f"{data['date_from']:%d.%m.%Y} – {data['date_to']:%d.%m.%Y}" if data.get("date_from") else "",
            timezone.localtime(data["at"]).strftime("%d.%m.%Y %H:%M"), len(work),
            mark_safe("".join(str(r) for r in rows)) or format_html('<tr><td colspan="4">{}</td></tr>', "—"), ads_html,
            rebuild, reverse("admin:inventory_supplier_analysis", args=[obj.pk]), opts)

    @admin.display(description="Візитки й флаєри")
    def sup_media(self, obj):
        if not obj or not obj.pk:
            return "—"
        items = list(obj.media.all()[:24])
        add = format_html('<a class="button" href="{}">📷 Додати фото / візитку</a>',
                          reverse("admin:inventory_supplier_card") + f"?supplier={obj.pk}")
        if not items:
            return format_html('<div class="sp-muted">Ще немає. Сфотографуйте візитку, флаєр чи стенд — Minerva '
                               'збереже фото тут з вашими примітками.</div><div class="sp-row">{}</div>', add)
        cards = format_html_join("", (
            '<a class="sp-card" href="{}"><img src="{}" alt="" loading="lazy"><div><b>{}</b> {}</div>'
            '<div class="sp-muted">{}{}</div><div>{}</div><div class="sp-note">{}</div></a>'),
            ((reverse("admin:inventory_supplier_card_review", args=[m.pk]), m.image.url, m.get_kind_display(),
              m.title, m.created_at.strftime("%d.%m.%Y"), f" · {m.event}" if m.event else "", m.marks_text(),
              m.note[:160]) for m in items if m.image))
        return format_html('<div class="sp-cards">{}</div><div class="sp-row">{}</div>', cards, add)

    @admin.display(description="Запити (RFQ)")
    def sup_requests(self, obj):
        if not obj or not obj.pk:
            return "—"
        viewer = self._viewer(obj)
        can = viewer is not None and viewer.has_perm("suppliers.view_supplierrequest")
        rows = list(obj.request_recipients.select_related("request").order_by("-pk")[:20])
        if not rows:
            return format_html('<div class="sp-muted">{}</div>', "Запитів цьому постачальнику ще не було.")
        body = format_html_join("", '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>', (
            (format_html('<a href="{}" translate="no">{}</a>', reverse("admin:suppliers_supplierrequest_work",
                                                                      args=[r.request_id]), r.request.code)
             if can else r.request.code, r.request.title[:70], r.get_state_display(),
             r.sent_at.strftime("%d.%m.%Y") if r.sent_at else "—",
             (f"{r.price} {r.currency}" if r.price is not None else "—") + (f" · {r.lead_days} дн." if r.lead_days else ""),
             "🏆" if r.chosen else "") for r in rows))
        return format_html('<table class="sp-table"><tr><th>Запит</th><th>Що</th><th>Стан</th><th>Надіслано</th>'
                           '<th>Пропозиція</th><th></th></tr>{}</table>', body)

    @admin.display(description="AI та інтернет")
    def sup_ai(self, obj):
        if not obj or not obj.pk:
            return "—"
        from suppliers.services.ai import MODELS
        viewer = self._viewer(obj)
        web = SupplierAnalysis.objects.filter(supplier=obj, kind=SupplierAnalysis.WEB).first()
        own = SupplierAnalysis.objects.filter(supplier=obj, kind=SupplierAnalysis.AI, owner=viewer).first() \
            if viewer else None
        parts = []
        if web:
            r = web.result or {}
            facts = [(k, r.get(f)) for k, f in (("📍 Де", "location"), ("🏭 Виробник?", "is_manufacturer"),
                                                ("📅 Засновано", "founded"), ("👷 Розмір", "size"),
                                                ("🌍 Ринки", "markets")) if r.get(f)]
            lists = [(k, r.get(f) or []) for k, f in (("Продукція", "products"), ("Сертифікати", "certifications"),
                                                      ("Сильні сторони", "strengths"), ("⚠️ Ризики", "risks"))]
            parts.append(format_html(
                '<div class="sp-box"><div class="sp-h">🌐 З інтернету · {}</div><div>{}</div>{}{}<div class="sp-muted">'
                'Джерела: {}</div></div>', web.created_at.strftime("%d.%m.%Y"), r.get("summary", ""),
                format_html_join("", "<div><b>{}:</b> {}</div>", facts),
                format_html_join("", "<div><b>{}:</b> {}</div>", ((k, ", ".join(v)) for k, v in lists if v)),
                format_html_join(" · ", '<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>',
                                 ((s["url"], s.get("title") or s["url"][:50]) for s in r.get("sources", [])
                                  if str(s.get("url", "")).startswith("http"))) or "—"))
        if own:
            r = own.result or {}
            probs = format_html_join("", '<li>{} — «{}» <a href="/email/?msg={}" target="_blank">✉️ {}</a></li>',
                                     ((p["text"], p["quote"][:200], p["message"], p["date"]) for p in r.get("problems", [])))
            parts.append(format_html(
                '<div class="sp-box"><div class="sp-h">🧠 AI-оцінка з вашої пошти · {} · {} листів {}</div><div>{}</div>'
                '<div><b>Зв\'язок:</b> {}</div><div><b>Надійність:</b> {}</div>{}{}</div>',
                own.created_at.strftime("%d.%m.%Y"), r.get("mails", 0), _stars(r.get("rating") or 0),
                r.get("summary", ""), r.get("communication", "—"), r.get("reliability", "—"),
                format_html("<div><b>Проблеми:</b><ul>{}</ul></div>", probs) if probs else "",
                format_html("<div><b>Поради:</b> {}</div>", "; ".join(r.get("advice") or [])) if r.get("advice") else ""))
        options = format_html_join("", '<option value="{}">{}</option>', MODELS)
        can_change = viewer is not None and viewer.has_perm("inventory.change_supplier")
        controls = ""
        if can_change:
            controls = format_html(
                '<div class="sp-row"><select class="sp-model" title="Модель AI">{}</select>'
                '<button type="button" class="button" data-sp-job="{}">🌐 Знайти про них в інтернеті</button>'
                '<a class="button" href="{}">🧠 Оцінка з листування…</a><span class="sp-job"></span></div>',
                options, reverse("admin:inventory_supplier_research", args=[obj.pk]),
                reverse("admin:inventory_supplier_analysis", args=[obj.pk]) + "#ai")
        if not parts:
            parts.append(format_html('<div class="sp-muted">{}</div>', "AI ще не запускали. «🌐 Знайти в інтернеті» — "
                                     "хто вони, що роблять, сертифікати й ризики з джерелами; «🧠 Оцінка» — з вашого "
                                     "листування й закупівель."))
        return format_html("{}{}{}", mark_safe("".join(str(x) for x in parts)), controls, _JOB_JS)

    @admin.display(description="Історія")
    def sup_history(self, obj):
        if not obj or not obj.pk:
            return "—"
        viewer = self._viewer(obj)
        events = list(obj.events.select_related("user", "message__account", "media")[:40])
        if not events:
            return "—"
        rows = []
        for e in events:
            link = ""
            if e.message_id and viewer is not None and e.message and e.message.account.user_id == viewer.pk:
                link = format_html(' <a href="/email/?msg={}" target="_blank">✉️ лист</a>', e.message_id)
            if e.media_id:
                link += format_html(' <a href="{}">📷 фото</a>', reverse("admin:inventory_supplier_card_review",
                                                                       args=[e.media_id]))
            rows.append(format_html('<tr><td>{}</td><td>{}</td><td>{}{}</td><td>{}</td></tr>',
                                    timezone.localtime(e.created_at).strftime("%d.%m.%Y %H:%M"),
                                    e.get_kind_display(), e.text, link,
                                    (e.user.get_full_name() or e.user.username) if e.user else "Minerva"))
        return format_html('<table class="sp-table">{}</table>', mark_safe("".join(rows)))

    # ── сторінки ──
    def get_urls(self):
        view = self.admin_site.admin_view
        return [
            path("find-in-mail/", view(self.find_in_mail_view), name="inventory_supplier_find_in_mail"),
            path("from-mail/<int:msg_pk>/", view(self.from_mail_view), name="inventory_supplier_from_mail"),
            path("card/", view(self.card_view), name="inventory_supplier_card"),
            path("cards/", view(self.cards_view), name="inventory_supplier_cards"),
            path("cards/<int:media_pk>/", view(self.card_review_view), name="inventory_supplier_card_review"),
            path("card/presets/", view(self.card_presets_view), name="inventory_supplier_card_presets"),
            path("job/<str:job_id>/", view(self.job_view), name="inventory_supplier_job"),
            path("search/", view(self.search_view), name="inventory_supplier_search"),
            path("attachment/<int:msg_pk>/<int:index>/", view(self.attachment_view),
                 name="inventory_supplier_attachment"),
            path("<int:pk>/history/", view(self.history_view), name="inventory_supplier_history"),
            path("<int:pk>/analysis/", view(self.analysis_view), name="inventory_supplier_analysis"),
            path("<int:pk>/research/", view(self.research_view), name="inventory_supplier_research"),
        ] + super().get_urls()

    def _ctx(self, request, title, **extra):
        from suppliers.services.ai import MODELS
        return {**self.admin_site.each_context(request), "title": title, "opts": self.model._meta,
                "models": MODELS, "style": _STYLE, "job_js": _JOB_JS, **extra}

    def _supplier(self, request, pk, perm="view"):
        obj = self.get_queryset(request).filter(pk=pk).first()
        if obj is None:
            raise Http404
        ok = self.has_view_permission(request, obj) if perm == "view" else self.has_change_permission(request, obj)
        if not ok:
            raise Http404
        setattr(obj, VIEWER, request.user)
        return obj

    def attachment_view(self, request, msg_pk, index):
        """Вкладення листа з БУДЬ-ЯКОЇ власної скриньки людини (у пошті — лише з основної)."""
        from email_assistant.attachments import attachment_bytes
        from email_assistant.views import _attachment_response
        msg = self._own_message(request, msg_pk)
        atts = msg.attachments or []
        if not 0 <= index < len(atts):
            raise Http404
        try:
            data = attachment_bytes(msg.account, msg, index)
        except Exception:  # noqa: BLE001 — IMAP недоступний
            data = None
        if not data:
            raise Http404
        att = atts[index]
        return _attachment_response(data, att.get("name") or "attachment", att.get("content_type") or "",
                                    force_download=request.GET.get("download") == "1")

    def history_view(self, request, pk):
        """POST: sup_action=rebuild — зібрати історію з пошти; action=po + topic — закупівля з теми."""
        from suppliers.services import history
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        obj = self._supplier(request, pk)
        back = reverse("admin:inventory_supplier_change", args=[obj.pk])
        if request.GET.get("action") == "po":
            if not request.user.has_perm("inventory.add_purchaseorder"):
                raise Http404
            topic = request.POST.get("topic", "")
            as_history = topic in request.POST.getlist("hist")
            try:
                po, out = history.make_purchase(obj, request.user, topic, as_history=as_history)
            except ValueError as e:
                self.message_user(request, str(e), messages.ERROR)
                return redirect(back)
            history.rebuild(obj, request.user)
            if as_history:
                self.message_user(request, f"📜 Закупівля {po.code} — як історія (уже отримано, без складу): "
                                           f"документів {out['added']}", messages.SUCCESS)
            else:
                self.message_user(request, f"🛒 Закупівля {po.code}: документів {out['added']} — перевірте позиції й "
                                           f"суми", messages.SUCCESS)
            for line in out["failed"][:5]:
                self.message_user(request, f"⚠️ {line}", messages.WARNING)
            return redirect(reverse("admin:inventory_purchaseorder_change", args=[po.pk]))
        out = history.rebuild(obj, request.user)
        self.message_user(request, f"🧵 Історію зібрано: листів {out['messages']}, тем {out['topics']}, "
                                   f"нових подій {out['events']}", messages.SUCCESS)
        self._make_purchases(request, obj)
        return redirect(back)

    def _make_purchases(self, request, supplier):
        """Галочка «🛒 одразу завести закупівлі з тем з PI» (po_options) після збору історії з пошти."""
        from suppliers.services import history
        if not request.POST.get("make_po") or not request.user.has_perm("inventory.add_purchaseorder"):
            return
        as_history = bool(request.POST.get("po_history"))
        out = history.make_purchases(supplier, request.user, as_history=as_history)
        n = len(out["pos"])
        codes = ", ".join(po.code for po in out["pos"][:12]) + ("…" if n > 12 else "")
        if not n:
            self.message_user(request, "🛒 Тем з PI / рахунком без закупівлі немає — нічого не заведено", messages.INFO)
        elif as_history:
            self.message_user(request, f"📜 Заведено закупівель як історію (уже отримано, без складу): {n} — {codes}",
                              messages.SUCCESS)
        else:
            self.message_user(request, f"🛒 Заведено закупівель: {n} — {codes}; перевірте позиції й суми",
                              messages.SUCCESS)
        for line in out["failed"][:5]:
            self.message_user(request, f"⚠️ {line}", messages.WARNING)

    def search_view(self, request):
        """Підказка постачальника за назвою / email (поле «додати до наявного»)."""
        if not _can(request, "view"):
            raise Http404
        q = " ".join(request.GET.get("q", "").split())[:80]
        qs = self.get_queryset(request).filter(Q(name__icontains=q) | Q(email__icontains=q)) if len(q) >= 2 else []
        return JsonResponse({"results": [{"id": s.pk, "text": s.name} for s in list(qs.order_by("name")[:15])]})

    def job_view(self, request, job_id):
        from suppliers.services import jobs
        s = jobs.state(job_id, request.user)
        if s is None:
            return JsonResponse({"status": "gone"}, status=404)
        return JsonResponse(s)

    def research_view(self, request, pk):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        obj = self._supplier(request, pk, "change")
        from suppliers.services import ai, jobs
        model = ai.model_of(request.POST.get("model"))
        user = request.user
        url = reverse("admin:inventory_supplier_change", args=[obj.pk])

        def run():
            ai.research(obj, user=user, model=model)
            return {"url": url, "message": "🌐 Готово"}
        return JsonResponse({"job": jobs.start(user, f"research {obj.pk}", run)})

    def analysis_view(self, request, pk):
        """✉️ Листування з постачальником за період (лише ваші скриньки) + 🧠 AI-оцінка."""
        from suppliers.services import ai, jobs, mailscan
        from suppliers.services.overview import purchases, requests_stats, score
        obj = self._supplier(request, pk)
        user = request.user
        date_from, date_to = mailscan.period(request.GET.get("from") or request.POST.get("from"),
                                             request.GET.get("to") or request.POST.get("to"))
        if request.method == "POST" and request.POST.get("action") == "ai":
            if not self.has_change_permission(request, obj):
                return JsonResponse({"error": "Немає права змінювати постачальника"}, status=403)
            model = ai.model_of(request.POST.get("model"))
            back = reverse("admin:inventory_supplier_analysis", args=[obj.pk]) + \
                f"?from={date_from:%Y-%m-%d}&to={date_to:%Y-%m-%d}#ai"

            def run():
                res = mailscan.scan(obj, user, date_from, date_to)
                ai.evaluate(obj, user, res, purchases(obj), model=model)
                return {"url": back, "message": "🧠 Оцінку складено"}
            return JsonResponse({"job": jobs.start(user, f"evaluate {obj.pk}", run)})
        result = mailscan.scan(obj, user, date_from, date_to)
        from suppliers.services import history
        history.save(obj, user, result)
        p = purchases(obj)
        rfq = requests_stats(obj)
        own_ai = SupplierAnalysis.objects.filter(supplier=obj, kind=SupplierAnalysis.AI, owner=user).first()
        return TemplateResponse(request, "admin/suppliers/analysis.html", self._ctx(
            request, f"✉️ {obj.name} — листування й оцінка", supplier=obj, r=result, p=p, rfq=rfq,
            score=score(p, mailscan.summary(result), rfq), own_ai=own_ai, can_change=self.has_change_permission(request, obj),
            back_url=reverse("admin:inventory_supplier_change", args=[obj.pk]),
            presets=[(d, (timezone.localdate() - datetime.timedelta(days=d)).strftime("%Y-%m-%d"))
                     for d in (90, 365, 730, 1825)]))

    # ── 🏭 Постачальник з листа ──
    def _own_message(self, request, msg_pk):
        from email_assistant.models import EmailMessage
        msg = EmailMessage.objects.filter(pk=msg_pk, account__user=request.user).select_related("account").first()
        if msg is None:
            raise Http404
        return msg

    def _draft_from_post(self, request) -> dict:
        from suppliers.services.draft import CONTACT_FIELDS, FIELDS
        return {k: request.POST.get(k, "") for k in FIELDS + CONTACT_FIELDS}

    def _categories_from_post(self, request):
        ids = [int(x) for x in request.POST.getlist("categories") if str(x).isdigit()]
        return list(SupplierCategory.objects.filter(pk__in=ids))

    def _attach_existing(self, request, supplier, data, *, source, message=None, media=None):
        from suppliers.services import draft
        contact, created = draft.add_contact(supplier, data, source=source, user=request.user, media=media,
                                             message=message)
        changed = draft.fill_empty(supplier, data, user=request.user, why="з листа" if source == "mail"
                                   else "з візитки") if request.POST.get("fill_empty") else []
        cats = self._categories_from_post(request)
        if cats:
            supplier.categories.add(*cats)
        if message is not None:
            SupplierEvent.objects.create(supplier=supplier, kind=SupplierEvent.MAIL, message=message, user=request.user,
                                         text=f"Лист «{(message.subject or '')[:150]}» від {message.from_email}")
        bits = []
        if created:
            bits.append(f"👤 контакт {contact.name}")
        if changed:
            bits.append(f"доповнено полів: {len(changed)}")
        self.message_user(request, f"✅ {supplier.name}: " + (", ".join(bits) or "уже все є"), messages.SUCCESS)
        if source == "mail":
            self._rebuild_quietly(request, supplier)
        return redirect(reverse("admin:inventory_supplier_change", args=[supplier.pk]))

    def _rebuild_quietly(self, request, supplier):
        """Постачальник з листа → історія з пошти людини одразу (збій — лише повідомлення, створення не скасовує)."""
        from suppliers.services import history
        try:
            out = history.rebuild(supplier, request.user)
            if out["messages"]:
                self.message_user(request, f"🧵 Історію з пошти зібрано: листів {out['messages']}, тем {out['topics']}",
                                  messages.INFO)
            self._make_purchases(request, supplier)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("supplier history rebuild failed", exc_info=True)

    def from_mail_view(self, request, msg_pk):
        from suppliers.services import ai, draft, jobs
        msg = self._own_message(request, msg_pk)
        if not (_can(request, "view") or _can(request, "add")):
            raise Http404
        action = request.POST.get("action") if request.method == "POST" else ""
        if action == "ai":
            if not (_can(request, "add") or _can(request, "change")):
                return JsonResponse({"error": "Немає права"}, status=403)
            model, web, user = ai.model_of(request.POST.get("model")), bool(request.POST.get("web")), request.user

            def run():
                return {"fields": ai.from_mail(msg, user=user, model=model, web=web), "message": "🧠 Заповнено"}
            return JsonResponse({"job": jobs.start(user, f"from-mail {msg.pk}", run)})
        if action == "create":
            if not _can(request, "add"):
                raise Http404
            data = self._draft_from_post(request)
            try:
                s = draft.create(data, user=request.user, source="mail", message=msg,
                                 categories=self._categories_from_post(request), note=request.POST.get("note", ""))
            except ValueError as e:
                self.message_user(request, str(e), messages.ERROR)
                return redirect(request.path)
            self.message_user(request, f"🏭 Створено постачальника {s.name}", messages.SUCCESS)
            self._rebuild_quietly(request, s)
            return redirect(reverse("admin:inventory_supplier_change", args=[s.pk]))
        if action == "attach":
            if not _can(request, "change"):
                raise Http404
            s = self.get_queryset(request).filter(pk=request.POST.get("supplier") or 0).first()
            if s is None:
                raise Http404
            return self._attach_existing(request, s, self._draft_from_post(request), source="mail", message=msg)
        data = draft.from_message(msg)
        return TemplateResponse(request, "admin/suppliers/from_mail.html", self._ctx(
            request, "🏭 Постачальник з листа", msg=msg, d=data, matches=draft.existing(data),
            categories=SupplierCategory.objects.filter(is_active=True), can_add=_can(request, "add"),
            can_change=_can(request, "change"),
            po_opts=po_options() if request.user.has_perm("inventory.add_purchaseorder") else ""))

    def find_in_mail_view(self, request):
        """🔎 Хто з вашого листування схожий на постачальника, але його ще немає в Minerva."""
        from suppliers.services import discover, mailscan
        if not _can(request, "view"):
            raise Http404
        if request.method == "POST" and request.POST.get("action") == "ignore":
            if discover.ignore(request.POST.get("key"), request.user):
                self.message_user(request, f"🙈 {request.POST.get('key')} — більше не пропонувати", messages.INFO)
            return redirect(request.get_full_path())
        date_from, date_to = mailscan.period(request.GET.get("from"), request.GET.get("to"), days=discover.DAYS)
        rows = discover.candidates(request.user, date_from=date_from, date_to=date_to)
        return TemplateResponse(request, "admin/suppliers/find_in_mail.html", self._ctx(
            request, "🔎 Постачальники з пошти", rows=rows, date_from=date_from, date_to=date_to,
            can_add=_can(request, "add")))

    # ── 📷 Візитка / флаєр ──
    def card_view(self, request):
        from suppliers.services import ai, jobs, qr
        if not (_can(request, "add") or _can(request, "change")):
            raise Http404
        supplier = None
        sid = request.POST.get("supplier") or request.GET.get("supplier")
        if sid and str(sid).isdigit():
            supplier = self.get_queryset(request).filter(pk=int(sid)).first()
        if request.method == "POST":
            image = request.FILES.get("image")
            if image is None:
                self.message_user(request, "Зробіть фото або виберіть файл.", messages.ERROR)
                return redirect(request.get_full_path())
            if image.size > 15 * 1024 * 1024:
                self.message_user(request, "Фото понад 15 МБ.", messages.ERROR)
                return redirect(request.get_full_path())
            kind = request.POST.get("kind") if request.POST.get("kind") in dict(SupplierMedia.KINDS) else SupplierMedia.CARD
            m = SupplierMedia(supplier=supplier, kind=kind, title=request.POST.get("title", "")[:200],
                              event=request.POST.get("event", "")[:200], note=request.POST.get("note", "")[:4000],
                              marks=[x for x in request.POST.getlist("marks") if x in dict(SupplierMedia.MARKS)],
                              created_by=request.user)
            m.image.save(image.name or "photo.jpg", image, save=False)
            back = request.FILES.get("image_back")
            if back is not None and back.size <= 15 * 1024 * 1024:
                m.image_back.save(back.name or "back.jpg", back, save=False)
            m.preset = SupplierCardPreset.objects.filter(Q(owner=request.user) | Q(shared=True),
                                                         pk=request.POST.get("preset") or 0).first()
            m.qr_text = qr.join(request.POST.get("qr_front", ""), request.POST.get("qr_back", ""))
            fields = qr.parse_all(m.qr_text)
            if fields:
                m.extracted, m.state = qr.merge(fields, {}), SupplierMedia.READ
            m.save()
            m.categories.set(self._categories_from_post(request))
            if supplier is not None:
                SupplierEvent.objects.create(supplier=supplier, kind=SupplierEvent.CARD, media=m, user=request.user,
                                             text=f"{m.get_kind_display()} додано" + (f" · {m.event}" if m.event else ""))
            url = reverse("admin:inventory_supplier_card_review", args=[m.pk])
            job = ""
            if request.POST.get("read_ai"):
                model, user = ai.model_of(request.POST.get("model")), request.user

                def run():
                    ai.card(m, user=user, model=model)
                    return {"url": url, "message": "🧠 Прочитано"}
                job = jobs.start(user, f"card {m.pk}", run)
            if request.POST.get("go") == "next":       # 🎪 на виставці: одразу наступна візитка з тим самим пресетом
                self.message_user(request, format_html(
                    '✅ Збережено <a href="{}">{}</a>{}', url, m.title or m.get_kind_display(),
                    " — AI читає у фоні; перевірити — «🪪 Усі візитки → ще не внесені»" if job else ""),
                    messages.SUCCESS)
                nxt = reverse("admin:inventory_supplier_card") + (f"?preset={m.preset_id}" if m.preset_id else "")
                if supplier is not None:
                    nxt += ("&" if "?" in nxt else "?") + f"supplier={supplier.pk}"
                return redirect(nxt)
            return redirect(url + (f"?job={job}" if job else ""))
        presets = self._presets(request.user)
        chosen = request.GET.get("preset")
        if not str(chosen or "").isdigit():
            chosen = self._default_preset(request.user)
        return TemplateResponse(request, "admin/suppliers/card.html", self._ctx(
            request, "📷 Візитка / флаєр", supplier=supplier, kinds=SupplierMedia.KINDS, marks=SupplierMedia.MARKS,
            categories=SupplierCategory.objects.filter(is_active=True), presets_json=presets,
            sides=[("front", "Лицьовий бік", True), ("back", "Зворотний бік", False)],
            preset_now=int(chosen) if str(chosen or "").isdigit() else None,
            preset_default=self._default_preset(request.user),
            recent=SupplierMedia.objects.select_related("supplier")[:8]))

    # ── 📌 Пресети візиток (виставка: однакові «де взяли», позначки, категорії) ──
    @staticmethod
    def _presets(user) -> list:
        qs = (SupplierCardPreset.objects.filter(Q(owner=user) | Q(shared=True)).select_related("owner")
              .prefetch_related("categories"))
        return [{"id": p.pk, "name": p.name, "event": p.event, "kind": p.kind, "marks": p.marks or [],
                 "note": p.note, "categories": [c.pk for c in p.categories.all()], "read_ai": p.read_ai,
                 "model": p.model, "shared": p.shared, "mine": p.owner_id == user.pk,
                 "owner": "" if p.owner_id == user.pk else (p.owner.get_full_name() or p.owner.username)} for p in qs]

    @staticmethod
    def _default_preset(user):
        profile = getattr(user, "profile", None)
        value = (getattr(profile, "ui_prefs", None) or {}).get("supplier_card_preset") if profile else None
        return value if isinstance(value, int) else None

    def card_presets_view(self, request):
        """POST {action: save | delete | default, id, name, event, kind, marks, note, categories, read_ai, model,
        shared, make_default} → {presets, default}. Змінити / видалити — лише свій пресет."""
        from core.models import UserProfile
        if not (_can(request, "add") or _can(request, "change")):
            raise Http404
        if request.method != "POST":
            return JsonResponse({"presets": self._presets(request.user), "default": self._default_preset(request.user)})
        P, action = request.POST, request.POST.get("action")
        mine = SupplierCardPreset.objects.filter(owner=request.user)
        default, saved = self._default_preset(request.user), None
        if action == "save":
            name = " ".join(P.get("name", "").split())[:120]
            if not name:
                return JsonResponse({"error": "Назва пресету порожня."}, status=400)
            p = mine.filter(pk=P.get("id") or 0).first() or SupplierCardPreset(owner=request.user)
            p.name, p.event, p.note = name, P.get("event", "")[:200], P.get("note", "")[:4000]
            p.kind = P.get("kind") if P.get("kind") in dict(SupplierMedia.KINDS) else SupplierMedia.CARD
            p.marks = [x for x in P.getlist("marks") if x in dict(SupplierMedia.MARKS)]
            p.read_ai, p.shared = bool(P.get("read_ai")), bool(P.get("shared"))
            from suppliers.services.ai import MODELS
            p.model = P.get("model") if P.get("model") in dict(MODELS) else ""
            p.save()
            p.categories.set(self._categories_from_post(request))
            saved = p.pk
            if P.get("make_default"):
                default = p.pk
        elif action == "delete":
            mine.filter(pk=P.get("id") or 0).delete()
            if str(default) == str(P.get("id")):
                default = None
        elif action == "default":
            pid = P.get("id")
            default = int(pid) if str(pid or "").isdigit() and SupplierCardPreset.objects.filter(
                Q(owner=request.user) | Q(shared=True), pk=int(pid)).exists() else None
        else:
            return JsonResponse({"error": "Невідома дія"}, status=400)
        if default != self._default_preset(request.user):
            profile, _ = UserProfile.objects.get_or_create(user=request.user)
            prefs = dict(profile.ui_prefs or {})
            if default:
                prefs["supplier_card_preset"] = default
            else:
                prefs.pop("supplier_card_preset", None)
            UserProfile.objects.filter(pk=profile.pk).update(ui_prefs=prefs)
        return JsonResponse({"presets": self._presets(request.user), "default": default,
                             "id": saved})

    def cards_view(self, request):
        if not _can(request, "view"):
            raise Http404
        qs = SupplierMedia.objects.select_related("supplier", "created_by")
        show = request.GET.get("show", "")
        if show == "new":
            qs = qs.filter(supplier=None)
        q = " ".join(request.GET.get("q", "").split())[:100]
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(note__icontains=q) | Q(event__icontains=q) |
                           Q(supplier__name__icontains=q))
        mark = request.GET.get("mark", "")
        items = [m for m in qs[:300] if not mark or mark in (m.marks or [])][:120]
        return TemplateResponse(request, "admin/suppliers/cards.html", self._ctx(
            request, "🪪 Візитки й флаєри", items=items, show=show, q=q, mark=mark, marks=SupplierMedia.MARKS,
            unassigned=SupplierMedia.objects.filter(supplier=None).count()))

    def _card_drop_side(self, request, m, url):
        """Прибрати лицьову або зворотну сторону: її фото (і оригінал) видаляється, друга лишається — лицьову
        заміщає зворотна. Єдину сторону так не прибрати — лише «🗑 Видалити повністю»."""
        back = request.POST.get("side") == "back"
        if not (m.image and m.image_back):
            self.message_user(request, "Це єдина сторона — щоб прибрати її, видаліть фото повністю.", messages.ERROR)
            return redirect(url)
        gone, keep = (("image_back", "image_back_original"), ("image", "image_original")) if back else \
            (("image", "image_original"), ("image_back", "image_back_original"))
        keep_names = {getattr(m, f).name for f in keep if getattr(m, f)}
        for f in gone:
            file = getattr(m, f)
            if file and file.name not in keep_names:     # той самий файл в іншому полі не чіпаємо
                file.delete(save=False)
        if not back:
            m.image = m.image_back.name
            m.image_original = m.image_back_original.name if m.image_back_original else ""
        m.image_back, m.image_back_original = "", ""
        m.save(update_fields=["image", "image_original", "image_back", "image_back_original"])
        self.message_user(request, "🗑 Зворотну сторону прибрано" if back else
                          "🗑 Лицьову сторону прибрано — зворотна тепер перша", messages.SUCCESS)
        return redirect(url)

    def _card_edit(self, request, m, url):
        """POST multipart: side, image (JPEG з браузера), qr_front / qr_back (QR, які браузер знайшов після правки),
        read_ai, model → JSON {url}. Перша правка переносить знімок у *_original (файл не копіюється)."""
        from suppliers.services import ai, jobs, qr
        side = "back" if request.POST.get("side") == "back" else "front"
        upload = request.FILES.get("image")
        if upload is None or upload.size > 15 * 1024 * 1024:
            return JsonResponse({"error": "Немає фото або воно понад 15 МБ."}, status=400)
        try:
            from PIL import Image
            Image.open(upload).verify()
            upload.seek(0)
        except Exception:  # noqa: BLE001 — не картинка
            return JsonResponse({"error": "Це не зображення."}, status=400)
        field, orig = ("image_back", "image_back_original") if side == "back" else ("image", "image_original")
        current = getattr(m, field)
        if not current:
            return JsonResponse({"error": "Цього боку ще немає."}, status=400)
        if getattr(m, orig):
            current.delete(save=False)            # проміжна версія не потрібна — оригінал уже збережено
        else:
            setattr(m, orig, current.name)
        getattr(m, field).save(f"edited_{side}.jpg", upload, save=False)
        updates = [field, orig]
        text = qr.join(request.POST.get("qr_front", ""), request.POST.get("qr_back", ""))
        fields = qr.parse_all(text)
        if text and text != m.qr_text:
            m.qr_text = text
            updates.append("qr_text")
            if fields:
                m.extracted = qr.merge(fields, m.extracted or {})
                updates.append("extracted")
                if m.state in (SupplierMedia.NEW, SupplierMedia.FAILED):
                    m.state = SupplierMedia.READ
                    updates.append("state")
        m.save(update_fields=updates)
        out = url
        if request.POST.get("read_ai"):
            model, user = ai.model_of(request.POST.get("model")), request.user

            def run():
                ai.card(m, user=user, model=model)
                return {"url": url, "message": "🧠 Прочитано"}
            out += "?job=" + jobs.start(user, f"card {m.pk}", run)
        self.message_user(request, "💾 Фото збережено" + (" · 🔳 " + qr.labels(qr.merge(fields, {}).get("from_qr"))
                                                          if fields else ""), messages.SUCCESS)
        return JsonResponse({"url": out})

    def card_review_view(self, request, media_pk):
        from suppliers.services import ai, draft, jobs, match, qr
        m = SupplierMedia.objects.select_related("supplier", "contact", "created_by").filter(pk=media_pk).first()
        if m is None or not _can(request, "view"):
            raise Http404
        can_edit = _can(request, "change") or (_can(request, "add") and m.created_by_id == request.user.pk)
        action = request.POST.get("action") if request.method == "POST" else ""
        if action and not can_edit:
            raise Http404
        url = reverse("admin:inventory_supplier_card_review", args=[m.pk])
        if action == "note":
            m.note = request.POST.get("note", "")[:4000]
            m.event = request.POST.get("event", "")[:200]
            m.title = request.POST.get("title", "")[:200]
            if request.POST.get("kind") in dict(SupplierMedia.KINDS):
                m.kind = request.POST["kind"]
            m.marks = [x for x in request.POST.getlist("marks") if x in dict(SupplierMedia.MARKS)]
            m.save(update_fields=["note", "event", "title", "kind", "marks"])
            self.message_user(request, "💾 Збережено", messages.SUCCESS)
            return redirect(url)
        if action == "ai":
            model, user = ai.model_of(request.POST.get("model")), request.user

            def run():
                ai.card(m, user=user, model=model)
                return {"url": url, "message": "🧠 Прочитано"}
            return JsonResponse({"job": jobs.start(user, f"card {m.pk}", run)})
        if action == "qr":
            text = qr.join(request.POST.get("qr_front", ""), request.POST.get("qr_back", ""))
            fields = qr.parse_all(text)
            if not text:
                self.message_user(request, "🔳 QR-коду на фото не знайдено.", messages.WARNING)
                return redirect(url)
            state = m.state if m.state == SupplierMedia.APPLIED else SupplierMedia.READ
            SupplierMedia.objects.filter(pk=m.pk).update(
                qr_text=text, extracted=qr.merge(fields, m.extracted or {}), state=state if fields else m.state)
            if fields:
                self.message_user(request, "🔳 З QR-коду: " + qr.labels(qr.merge(fields, {}).get("from_qr")),
                                  messages.SUCCESS)
            else:
                self.message_user(request, "🔳 QR прочитано, але це не візитка (vCard / MeCard / посилання) — "
                                           "текст збережено біля фото.", messages.INFO)
            return redirect(url)
        if action == "create":
            if not _can(request, "add"):
                raise Http404
            try:
                extra = (m.extracted or {}).get("notes", "")         # WeChat / LinkedIn з QR, що помітив AI
                s = draft.create(self._draft_from_post(request), user=request.user, source="card", media=m,
                                 categories=self._categories_from_post(request),
                                 note="\n".join(x for x in (m.note, extra) if x))
            except ValueError as e:
                self.message_user(request, str(e), messages.ERROR)
                return redirect(url)
            SupplierMedia.objects.filter(pk=m.pk).update(state=SupplierMedia.APPLIED, supplier=s)
            self.message_user(request, f"🏭 Створено постачальника {s.name} — фото збережено в його картці", messages.SUCCESS)
            return redirect(reverse("admin:inventory_supplier_change", args=[s.pk]))
        if action == "attach":
            s = self.get_queryset(request).filter(pk=request.POST.get("supplier") or 0).first()
            if s is None:
                raise Http404
            first = m.supplier_id is None
            SupplierMedia.objects.filter(pk=m.pk).update(supplier=s, state=SupplierMedia.APPLIED)
            if first:
                SupplierEvent.objects.create(supplier=s, kind=SupplierEvent.CARD, media=m, user=request.user,
                                             text=f"{m.get_kind_display()} додано" + (f" · {m.event}" if m.event else ""))
            return self._attach_existing(request, s, self._draft_from_post(request), source="card", media=m)
        if action == "edit":                       # ✏️ браузер повернув / обрізав фото → нова версія, оригінал лишається
            return self._card_edit(request, m, url)
        if action == "restore":                    # ↺ Оригінал
            side = "back" if request.POST.get("side") == "back" else "front"
            field, orig = ("image_back", "image_back_original") if side == "back" else ("image", "image_original")
            if getattr(m, orig):
                current = getattr(m, field)
                if current and current.name != getattr(m, orig).name:
                    current.delete(save=False)
                setattr(m, field, getattr(m, orig).name)
                setattr(m, orig, "")
                m.save(update_fields=[field, orig])
                self.message_user(request, "↺ Повернуто оригінал фото", messages.SUCCESS)
            return redirect(url)
        if action == "drop_side":                  # 🗑 прибрати одну сторону (двічі зняли той самий бік…)
            return self._card_drop_side(request, m, url)
        if action == "delete":
            if not (_can(request, "change") or m.created_by_id == request.user.pk):
                raise Http404
            sid = m.supplier_id
            for f in (m.image, m.image_back, m.image_original, m.image_back_original):
                if f:
                    f.delete(save=False)
            m.delete()
            self.message_user(request, "🗑 Фото видалено", messages.INFO)
            return redirect(reverse("admin:inventory_supplier_change", args=[sid]) if sid else
                            reverse("admin:inventory_supplier_cards"))
        data = dict(m.extracted or {})
        if not data:
            data = {k: "" for k in draft.FIELDS + draft.CONTACT_FIELDS}
        names = {c.name.lower(): c.pk for c in SupplierCategory.objects.filter(is_active=True)}
        picked = [names[n.lower()] for n in (data.get("categories") or []) if n.lower() in names]
        picked += [c for c in m.categories.values_list("pk", flat=True) if c not in picked]
        matches = draft.existing(data) if (data.get("name") or data.get("email") or data.get("website")) else []
        return TemplateResponse(request, "admin/suppliers/card_review.html", self._ctx(
            request, f"📷 {m.title or m.get_kind_display()}", m=m, d=data, matches=matches, picked=picked,
            categories=SupplierCategory.objects.filter(is_active=True), kinds=SupplierMedia.KINDS,
            marks=SupplierMedia.MARKS, can_edit=can_edit, can_add=_can(request, "add"),
            can_change=_can(request, "change"), job=request.GET.get("job", ""),
            photo_sides=[{"key": k, "file": f, "original": o} for k, f, o in (
                ("front", m.image, m.image_original), ("back", m.image_back, m.image_back_original)) if f],
            qr_parts=[x for x in (m.qr_text or "").split(qr.SEPARATOR) if x.strip()],
            qr_kind=_qr_kind(qr.parse_all(m.qr_text).get("kind")),
            qr_from=qr.labels(data.get("from_qr")),
            site_domain=match.site_domain(data.get("website", ""))))


_STYLE = mark_safe("""<style>
.sp-tiles{display:flex;flex-wrap:wrap;gap:8px;margin:2px 0 8px}
.sp-tile{border:1px solid var(--border-strong,#ccc);border-radius:8px;padding:6px 10px;min-width:150px;background:var(--bg-card,transparent)}
.sp-k{font-size:11px;color:var(--text-muted,#888)}.sp-v{font-weight:600}
.sp-muted{color:var(--text-muted,#888);font-size:12px}.sp-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0}
.sp-score{margin:4px 0 6px}.sp-warn{margin:6px 0;color:var(--warning-fg,#b06d00)}
.sp-h{margin:10px 0 4px;font-weight:600}.sp-table{border-collapse:collapse;margin:2px 0 6px;font-size:12px}
.sp-table td,.sp-table th{border-bottom:1px solid var(--border-strong,#ddd);padding:3px 8px;text-align:left;vertical-align:top}
.sp-cards{display:flex;flex-wrap:wrap;gap:10px}
.sp-card{display:block;width:190px;border:1px solid var(--border-strong,#ccc);border-radius:8px;padding:6px;text-decoration:none;color:inherit;background:var(--bg-card,transparent)}
.sp-card img{width:100%;height:110px;object-fit:cover;border-radius:6px;background:#fff}
.sp-note{font-size:12px;white-space:pre-line}.sp-box{border:1px solid var(--border-strong,#ccc);border-radius:8px;padding:8px 12px;margin:6px 0;max-width:980px}
.sp-job{font-size:12px;color:var(--text-muted,#888)}
.sp-chips{display:flex;flex-wrap:wrap;gap:3px 6px;margin-top:3px}
.sp-chip{font-size:11px;border:1px solid var(--border-strong,#ccc);border-radius:9px;padding:0 6px;white-space:nowrap;text-decoration:none}
</style>""")

# Кнопки data-sp-job: POST (CSRF з форми / cookie) → id завдання → опитування …/job/<id>/ → перехід / повідомлення.
_JOB_JS = mark_safe("""<script>
(function(){
  if (window.spJob) return;
  function csrf(){ var i=document.querySelector('input[name=csrfmiddlewaretoken]'); if(i) return i.value;
    var m=document.cookie.match(/csrftoken=([^;]+)/); return m?m[1]:''; }
  window.spJob = function(url, data, out, done){
    var fd = new FormData(); Object.keys(data||{}).forEach(function(k){ fd.append(k, data[k]); });
    if (out) out.textContent = '⏳ AI працює… (до хвилини)';
    fetch(url, {method:'POST', body:fd, headers:{'X-CSRFToken':csrf()}, credentials:'same-origin'})
      .then(function(r){ return r.json(); }).then(function(j){
        if (!j.job) { if (out) out.textContent = '⚠️ ' + (j.error || 'не вдалося'); return; }
        spPoll(j.job, out, done);
      }).catch(function(){ if (out) out.textContent = '⚠️ немає зв\\'язку'; });
  };
  window.spPoll = function(job, out, done){
    var base = (window.SP_JOB_URL || '/admin/inventory/supplier/job/'), t0 = Date.now();
    (function tick(){
      fetch(base + job + '/', {credentials:'same-origin'}).then(function(r){ return r.json(); }).then(function(s){
        if (s.status === 'running') { if (out) out.textContent = '⏳ AI працює… ' + Math.round((Date.now()-t0)/1000) + ' с';
          return setTimeout(tick, 2000); }
        if (s.status === 'failed') { if (out) out.textContent = '⚠️ ' + (s.error || 'помилка'); return; }
        if (s.status === 'done') { if (done) return done(s.result || {}); var r = s.result || {};
          if (r.url) { var u = new URL(r.url, location.href); if (u.pathname + u.search === location.pathname + location.search) location.reload(); else location.href = r.url; } else if (out) out.textContent = r.message || '✅'; return; }
        if (out) out.textContent = '⚠️ завдання не знайдено';
      }).catch(function(){ setTimeout(tick, 4000); });
    })();
  };
  document.addEventListener('click', function(e){
    var b = e.target.closest('[data-sp-job]'); if (!b) return; e.preventDefault();
    var box = b.closest('.sp-row') || b.parentNode, sel = box.querySelector('.sp-model');
    spJob(b.getAttribute('data-sp-job'), {model: sel ? sel.value : ''}, box.querySelector('.sp-job'));
  });
})();
</script>""")
