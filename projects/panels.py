"""Блок «📁 Проєкти» в картках клієнта, постачальника, закупівлі й товару (етап 3 ведення проєктів).

ProjectsBlockMixin — у ModelAdmin цих карток: поле лише для читання `projects_block` і група «📁 Проєкти»
(після групи `projects_block_after`), лише людям з правом projects.view_project; хто дивиться — get_object
запам'ятовує (як worktime.panels). Рядок — portfolio.load (кеш стану й плану): фаза, колір і причина, наступний
крок, строк клієнту / прогноз; для постачальника, закупівлі й товару — ще й що саме пов'язує з проєктом.
"""
from collections import defaultdict

from django.db.models import Q
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from .models import Project, ProjectLink, ProjectParticipant, ProjectStep

VIEWER_ATTR = "_projects_viewer"
SHOWN = 12
BLOCK = "projects_block"


def can_see(viewer) -> bool:
    return bool(viewer is not None and getattr(viewer, "is_authenticated", False)
                and viewer.has_perm("projects.view_project"))


def _table(rows, extra, viewer, empty) -> str:
    from . import portfolio
    if not rows:
        return format_html('<span style="opacity:.75">{}</span>', empty)
    today = timezone.localdate()
    projects = [p for p, _ in rows][:SHOWN]
    loaded = {r.project.pk: r for r in portfolio.load(projects, viewer, today)}
    body = []
    for p, why in rows[:SHOWN]:
        r = loaded.get(p.pk)
        if r is None:
            continue
        due = ""
        if p.due_date:
            late = bool(r.forecast and r.forecast > p.due_date)
            due = format_html('<span style="{}">⏰ {}{}</span>', "color:var(--err,#f85149);font-weight:600" if late else "",
                              p.due_date.strftime("%d.%m.%Y"),
                              f" · прогноз {r.forecast:%d.%m}" if late else "")
        nxt = format_html("{} {}", r.next["title"], f"до {r.next['end']:%d.%m}" if r.next.get("end") else "") \
            if r.next else (format_html("➡️ {}", p.next_action) if p.next_action else "—")
        body.append(format_html(
            '<tr><td style="white-space:nowrap;padding:3px 10px 3px 0"><a href="/admin/projects/project/{}/change/">'
            '<b>{}</b></a></td><td style="padding:3px 10px 3px 0" translate="no">{} {}</td>'
            '<td style="padding:3px 10px 3px 0;white-space:nowrap">{} {}</td>'
            '<td style="padding:3px 10px 3px 0">{}</td><td style="padding:3px 10px 3px 0">{}</td>'
            '<td style="padding:3px 10px 3px 0">{}</td><td style="padding:3px 0;opacity:.8">{}</td></tr>',
            p.pk, p.code, p.icon, p.name, r.dot, r.reason, r.phase_text or "без плану", nxt, due, why))
    more = ""
    if len(rows) > SHOWN:
        more = format_html('<div style="opacity:.75;margin-top:4px">{}</div>', f"… і ще {len(rows) - SHOWN}")
    head = format_html('<tr>{}</tr>', format_html_join("", '<th style="text-align:left;padding:2px 10px 4px 0;'
                                                           'font-size:11px;opacity:.75">{}</th>',
                                                       ((h,) for h in ("Проєкт", "Назва", "Стан", "Фаза",
                                                                       "Наступне", "Строк", extra))))
    return format_html('<table style="border-collapse:collapse">{}{}</table>{}'
                       '<div style="margin-top:6px"><a href="/admin/projects/project/portfolio/">📊 Портфель →</a></div>',
                       head, format_html_join("", "{}", ((b,) for b in body)), more)


def _order(projects_with_why):
    """Відкриті першими, далі — за строком клієнту."""
    from datetime import date
    return sorted(projects_with_why, key=lambda pw: (not pw[0].is_open, pw[0].due_date or date.max, pw[0].code))


def customer_html(customer, viewer) -> str:
    if not can_see(viewer):
        return ""
    rows = [(p, "") for p in Project.objects.filter(customer=customer).select_related("customer", "owner")]
    return _table(_order(rows), "", viewer, "У клієнта ще немає проєктів.")


def _supplier_projects(supplier) -> dict:
    """{project: [звідки]} — закупівлі цього постачальника, прив'язані до проєкту, кроки з ними, учасник-постачальник."""
    out = defaultdict(list)
    links = (ProjectLink.objects.filter(status=ProjectLink.CONFIRMED, purchase_order__supplier=supplier)
             .select_related("project__customer", "project__owner", "purchase_order"))
    steps = defaultdict(list)
    for row in (ProjectStep.links.through.objects.filter(projectlink__in=links).select_related("projectstep")):
        steps[row.projectlink_id].append(row.projectstep.title)
    for link in links:
        what = str(link.purchase_order)
        if steps.get(link.pk):
            what += " · крок «" + "», «".join(steps[link.pk][:2]) + "»"
        out[link.project].append(what)
    if supplier.email:
        for part in (ProjectParticipant.objects.filter(role=ProjectParticipant.SUPPLIER, email__iexact=supplier.email)
                     .select_related("project__customer", "project__owner")):
            out[part.project].append("учасник проєкту")
    return out


def supplier_html(supplier, viewer) -> str:
    if not can_see(viewer):
        return ""
    found = _supplier_projects(supplier)
    rows = _order([(p, "; ".join(dict.fromkeys(why))) for p, why in found.items()])
    return _table(rows, "Що пов'язує", viewer, "Закупівлі цього постачальника ще не прив'язані до проєктів.")


def purchase_html(po, viewer) -> str:
    if not can_see(viewer):
        return ""
    links = (ProjectLink.objects.filter(purchase_order=po).exclude(status=ProjectLink.REJECTED)
             .select_related("project__customer", "project__owner"))
    rows = []
    for link in links:
        steps = list(link.steps.values_list("title", flat=True)[:3])
        why = ("крок «" + "», «".join(steps) + "»") if steps else ""
        if link.status == ProjectLink.SUGGESTED:
            why = (why + " · " if why else "") + "пропозиція — підтвердіть у пульті проєкту (✓ / ✗)"
        rows.append((link.project, why))
    return _table(_order(rows), "Крок плану", viewer,
                  "Закупівля ще не прив'язана до проєкту — «🔎 Знайти пов'язане» в пульті проєкту або таблиця "
                  "«🏭 Закупівлі проєкту» в картці проєкту.")


def product_projects(product) -> dict:
    """{project: [звідки]} — «Товари проєкту», замовлення й Angebote проєкту з цим товаром."""
    from offers.models import OfferLine
    from sales.models import SalesOrderLine
    from django.db.models import Sum
    out = defaultdict(list)
    for p in Project.objects.filter(products=product).select_related("customer", "owner"):
        out[p].append("товар проєкту")
    links = list(ProjectLink.objects.filter(status=ProjectLink.CONFIRMED, order__lines__product=product)
                 .exclude(order__status="cancelled").select_related("project__customer", "project__owner", "order")
                 .distinct())
    qty = dict(SalesOrderLine.objects.filter(product=product, order_id__in=[link.order_id for link in links])
               .values("order_id").annotate(q=Sum("qty")).values_list("order_id", "q"))
    for link in links:
        out[link.project].append(f"замовлення {link.order.order_number}: {_qty(qty.get(link.order_id))} шт")
    for line in (OfferLine.objects.filter(product=product, offer__project__isnull=False)
                 .exclude(offer__status__in=("rejected", "expired", "cancelled"))
                 .select_related("offer__project__customer", "offer__project__owner")):
        out[line.offer.project].append(f"Angebot {line.offer.display_number}: {_qty(line.qty)} шт")
    return out


def _qty(value) -> str:
    from decimal import Decimal
    d = Decimal(value or 0).normalize()
    return f"{d:f}"


def product_html(product, viewer) -> str:
    if not can_see(viewer):
        return ""
    rows = _order([(p, "; ".join(dict.fromkeys(why))) for p, why in product_projects(product).items()])
    return _table(rows, "Що пов'язує", viewer,
                  "Товар не використовується в проєктах (ні в «Товарах проєкту», ні в замовленнях чи Angebote проєктів).")


RENDER = {"customer": customer_html, "supplier": supplier_html, "purchase": purchase_html, "product": product_html}


class ProjectsBlockMixin:
    """Група «📁 Проєкти» в картці (див. опис модуля). projects_block_kind: customer / supplier / purchase / product."""
    projects_block_kind = ""
    projects_block_after = None           # назва групи, після якої вставити; None — в кінець

    def get_object(self, request, object_id, from_field=None):
        obj = super().get_object(request, object_id, from_field)
        if obj is not None:
            setattr(obj, VIEWER_ATTR, request.user)
        return obj

    def get_readonly_fields(self, request, obj=None):
        fields = tuple(super().get_readonly_fields(request, obj))
        return fields if BLOCK in fields else fields + (BLOCK,)

    def get_fieldsets(self, request, obj=None):
        fieldsets = [(name, {**opts, "fields": [f for f in opts.get("fields", ()) if f != BLOCK]})
                     for name, opts in super().get_fieldsets(request, obj)]
        if obj is None or not obj.pk or not can_see(request.user):
            return fieldsets
        group = ("📁 Проєкти", {"fields": [BLOCK], "description": "Проєкти, з якими пов'язаний цей запис: фаза, стан, "
                                                                 "наступний крок, строк клієнту."})
        names = [name for name, _opts in fieldsets]
        at = names.index(self.projects_block_after) + 1 if self.projects_block_after in names else len(fieldsets)
        return fieldsets[:at] + [group] + fieldsets[at:]

    @staticmethod
    def _block(kind, obj):
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        try:
            return RENDER[kind](obj, getattr(obj, VIEWER_ATTR, None)) or "—"
        except Exception:  # noqa: BLE001 — картка без блоку краще, ніж помилка сторінки
            import logging
            logging.getLogger(__name__).exception("projects block %s %s", kind, obj.pk)
            return "—"

    def projects_block(self, obj):
        return self._block(self.projects_block_kind, obj)
    projects_block.short_description = "Проєкти"
