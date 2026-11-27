"""🔀 Склади товару: де лежить, переміщення між складами, звідки йде компонент BOM.

Логіка руху — лише inventory/services/stock.py (move_stock, move_all, on_hand_by_location, location_breakdown,
home_location); тут — сторінки й HTML для картки товару.

    /admin/inventory/product/<pk>/move-stock/   — один товар: склади, «з якого / на який / скільки»
    /admin/inventory/product/move-stock/?ids=…  — дія списку «🔀 Перемістити на склад…»: усе — на один склад
"""
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from .models import Location, Product
from .services import stock

TEMPLATE = "admin/inventory/product/move_stock.html"
BULK_MAX = 500


def _num(q):
    q = Decimal(str(q or 0))
    return f"{q.normalize():f}" if q != q.to_integral() else str(int(q))


def _targets(product=None):
    """Активні склади для «Куди»: ok — чи підходить товару (компонент — не на склад готової продукції й навпаки)."""
    out = []
    for loc in Location.objects.filter(is_active=True).order_by("location_type", "code"):
        ok, why = True, ""
        if product is not None:
            try:
                stock._check_location_kind(product, loc)
            except stock.StockError as e:
                ok, why = False, str(e)
        out.append({"loc": loc, "ok": ok, "why": why})
    return out


def _breakdown(product):
    """location_breakdown + об'єкт локації й назва типу — таблиця «Де лежить»."""
    rows = stock.location_breakdown(product)
    locs = {l.code: l for l in Location.objects.filter(code__in=[r["location"] for r in rows])}
    for r in rows:
        r["loc"] = locs.get(r["location"])
    return rows


def move_view(ma, request, pk):
    product = get_object_or_404(Product, pk=pk)
    if not ma.has_view_or_change_permission(request, product):
        raise PermissionDenied
    can = ma.has_change_permission(request, product)
    url = reverse("admin:inventory_product_move_stock", args=[product.pk])
    form = {"source": request.GET.get("from", ""), "target": "", "qty": ""}
    if request.method == "POST":
        if not can:
            raise PermissionDenied
        form = {k: (request.POST.get(k) or "").strip() for k in ("source", "target", "qty")}
        source = Location.objects.filter(pk=form["source"]).first() if form["source"].isdigit() else None
        target = Location.objects.filter(pk=form["target"]).first() if form["target"].isdigit() else None
        try:
            if source is None or target is None:
                raise stock.StockError("Виберіть, з якого складу й на який.", code="no_location")
            qty = form["qty"].replace(",", ".")
            if not qty:
                there = {r["location_id"]: r["qty"] for r in stock.on_hand_by_location([product.pk]).get(product.pk, [])}
                qty = there.get(source.pk, Decimal("0"))
                if qty <= 0:
                    raise stock.StockError(f"На «{source.code}» цього товару немає.", code="insufficient_stock")
            try:
                qty = Decimal(str(qty))
            except Exception:  # noqa: BLE001 — не число
                raise stock.StockError("Кількість — число, напр. 5 або 2,5.", code="bad_qty")
            stock.move_stock(product=product, source=source, target=target, qty=qty, performed_by=request.user)
        except stock.StockError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f"🔀 {product.sku}: {_num(qty)} перенесено {source.code} → {target.code}.")
            return redirect(url)
    ctx = {
        **ma.admin_site.each_context(request),
        "title": f"🔀 Склади: {product.sku}",
        "opts": Product._meta, "original": product, "product": product, "bulk": False,
        "rows": _breakdown(product), "home": stock.home_location(product.pk) if product.kind == Product.Kind.COMPONENT else None,
        "sources": stock.on_hand_by_location([product.pk]).get(product.pk, []),
        "targets": _targets(product), "can": can, "form": form,
        "card_url": reverse("admin:inventory_product_change", args=[product.pk]),
        "new_location_url": reverse("admin:inventory_location_add"),
    }
    return TemplateResponse(request, TEMPLATE, ctx)


def _ids(request):
    raw = request.POST.get("ids") or request.GET.get("ids") or ""
    return [int(x) for x in raw.split(",") if x.strip().isdigit()][:BULK_MAX]


def bulk_move_view(ma, request):
    if not ma.has_change_permission(request):
        raise PermissionDenied
    ids = _ids(request)
    products = list(Product.objects.filter(pk__in=ids).order_by("sku"))
    results = []
    if request.method == "POST":
        raw = (request.POST.get("target") or "").strip()
        target = Location.objects.filter(pk=raw, is_active=True).first() if raw.isdigit() else None
        if target is None:
            messages.error(request, "Виберіть склад, на який переносити.")
        else:
            moved = 0
            for p in products:
                try:
                    stock._check_location_kind(p, target)
                    done = stock.move_all(p, target, performed_by=request.user)
                except stock.StockError as e:
                    results.append({"product": p, "ok": False, "text": str(e)})
                    continue
                moved += bool(done)
                text = ", ".join(f"{src.code} → {target.code}: {_num(q)}" for src, q in done) or "уже там"
                results.append({"product": p, "ok": True, "text": text})
            messages.success(request, f"🔀 Перенесено на «{target.code}»: товарів {moved} з {len(products)}.")
    where = stock.on_hand_by_location([p.pk for p in products])
    rows = [{"product": p, "where": where.get(p.pk, [])} for p in products]
    ctx = {
        **ma.admin_site.each_context(request),
        "title": "🔀 Перемістити на склад",
        "opts": Product._meta, "bulk": True, "rows": rows, "results": results,
        "ids": ",".join(str(p.pk) for p in products), "targets": _targets(), "can": True,
        "changelist_url": reverse("admin:inventory_product_changelist"),
        "new_location_url": reverse("admin:inventory_location_add"),
    }
    return TemplateResponse(request, TEMPLATE, ctx)


# ── HTML для картки товару ────────────────────────────────────────────────────

def where_html(product, where=None):
    """«📍 MAIN 10 · COMPONENTS 3» — де лежить компонент; перший — звідки його спише продаж / BOM."""
    if product is None:
        return "—"
    rows = where if where is not None else stock.on_hand_by_location([product.pk]).get(product.pk, [])
    if not rows:
        return format_html('<span style="color:var(--text-dim)" title="{}">📍 ніде немає</span>',
                           "Немає на жодному складі — прихід піде на локацію за замовчуванням")
    first, rest = rows[0], rows[1:]
    head = format_html('<b title="{}">📍 {}</b> {}', f"{first['name']} — звідси списується", first["code"],
                       _num(first["qty"]))
    if not rest:
        return head
    more = format_html_join(" · ", '<span title="{}">{} {}</span>',
                            ((r["name"], r["code"], _num(r["qty"])) for r in rest))
    return format_html('{} <span style="color:var(--text-muted);font-size:11px">· також {}</span>', head, more)


def locations_panel(product):
    """Блок «📍 Склади» в картці товару: де скільки лежить, резерв, доступно + «🔀 Перемістити між складами»."""
    if product is None or not product.pk:
        return "—"
    rows = _breakdown(product)
    url = reverse("admin:inventory_product_move_stock", args=[product.pk])
    body = format_html_join("", '<tr><td style="padding:3px 10px 3px 0"><b>{}</b> <span style="color:var(--text-muted)">'
                                '{}</span></td><td style="padding:3px 10px;text-align:right">{}</td>'
                                '<td style="padding:3px 10px;text-align:right;color:var(--text-muted)">{}</td></tr>',
                            ((r["location"], r["location_name"], _num(r["on_hand"]),
                              f"резерв {_num(r['reserved'])}" if r["reserved"] else "") for r in rows))
    table = (format_html('<table style="border-collapse:collapse;font-size:12px;margin-bottom:6px">{}</table>', body)
             if rows else format_html('<div style="color:var(--text-dim);font-size:12px">{}</div>', "Руху ще не було"))
    return format_html('{}<a class="button" href="{}">🔀 Перемістити між складами</a>', table, url)
