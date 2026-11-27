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


# Склад, що приймає лише один тип товару (stock._check_location_kind): «Компоненти & Матеріали» — компоненти,
# «Готова продукція» — готові товари; «Інше» — будь-що.
KIND_FOR = {Location.LocationType.COMPONENTS: Product.Kind.COMPONENT,
            Location.LocationType.FINISHED: Product.Kind.FINISHED}


def fit_kind(product, location):
    """Тип, якого вимагає склад, якщо теперішній тип товару йому не підходить; None — підходить."""
    want = KIND_FOR.get(location.location_type)
    return want if want and product.kind != want else None


def _kind_label(kind):
    return dict(Product.Kind.choices).get(kind, kind)


def set_kind(ma, request, product, kind, location):
    """Змінити тип товару, щоб він підходив складу (переміщення з «змінити тип»); запис в історію товару."""
    old = product.get_kind_display()
    product.kind = kind
    product.save(update_fields=["kind"])
    ma.log_change(request, product, f"Тип товару: {old} → {_kind_label(kind)} (переміщення на склад {location.code})")


def _targets(product=None):
    """Активні склади для «Куди»: fix — на який тип треба змінити товар, щоб він туди підходив (компонент — не на
    склад готової продукції й навпаки); None — підходить і так."""
    out = []
    for loc in Location.objects.filter(is_active=True).order_by("location_type", "code"):
        fix = fit_kind(product, loc) if product is not None else None
        out.append({"loc": loc, "fix": fix, "fix_label": _kind_label(fix) if fix else ""})
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
            fix = fit_kind(product, target)
            if fix and not request.POST.get("set_kind"):
                raise stock.StockError(f"Склад «{target.code}» — для типу «{_kind_label(fix)}». Позначте «змінити тип "
                                       f"товару» або виберіть інший склад.", code="wrong_location_type")
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
            from django.db import transaction
            with transaction.atomic():      # тип змінюється лише разом із переміщенням
                if fix:
                    set_kind(ma, request, product, fix, target)
                stock.move_stock(product=product, source=source, target=target, qty=qty, performed_by=request.user)
        except stock.StockError as e:
            product.refresh_from_db(fields=["kind"])
            messages.error(request, str(e))
        else:
            kind = f" Тип товару — «{_kind_label(fix)}»." if fix else ""
            messages.success(request, f"🔀 {product.sku}: {_num(qty)} перенесено {source.code} → {target.code}.{kind}")
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
    results, refused, target = [], [], None
    if request.method == "POST":
        raw = (request.POST.get("target") or "").strip()
        target = Location.objects.filter(pk=raw, is_active=True).first() if raw.isdigit() else None
        change_kind = bool(request.POST.get("set_kind"))
        if target is None:
            messages.error(request, "Виберіть склад, на який переносити.")
        else:
            before = stock.on_hand_by_location([p.pk for p in products])
            moved = 0
            for p in products:
                fix = fit_kind(p, target)
                if fix and not change_kind:
                    # тип не підходить складу — не переносимо, а пропонуємо «🔧 Змінити тип і перенести»
                    refused.append(p.pk)
                    results.append({"product": p, "ok": False,
                                    "text": f"{p.get_kind_display()} — склад «{target.code}» для типу "
                                            f"«{_kind_label(fix)}»" + ("" if before.get(p.pk) else
                                                                     " (переносити нічого)")})
                    continue
                try:
                    from django.db import transaction
                    with transaction.atomic():
                        if fix:
                            set_kind(ma, request, p, fix, target)
                        done = stock.move_all(p, target, performed_by=request.user)
                except stock.StockError as e:
                    p.refresh_from_db(fields=["kind"])      # тип змінюється лише разом із переміщенням
                    results.append({"product": p, "ok": False, "text": str(e)})
                    continue
                moved += bool(done)
                text = ", ".join(f"{src.code} → {target.code}: {_num(q)}" for src, q in done)
                text = text or ("уже там" if before.get(p.pk) else "переносити нічого — ніде немає")
                if fix:
                    text = f"тип → «{_kind_label(fix)}» · {text}"
                results.append({"product": p, "ok": True, "text": text})
            messages.success(request, f"🔀 Перенесено на «{target.code}»: товарів {moved} з {len(products)}.")
    where = stock.on_hand_by_location([p.pk for p in products])
    rows = [{"product": p, "where": where.get(p.pk, [])} for p in products]
    fix_to = KIND_FOR.get(target.location_type) if target is not None else None
    ctx = {
        **ma.admin_site.each_context(request),
        "title": "🔀 Перемістити на склад",
        "opts": Product._meta, "bulk": True, "rows": rows, "results": results,
        # «🔧 Змінити тип і перенести» — лише товари, яким склад не підійшов за типом
        "refused_ids": ",".join(str(pk) for pk in refused), "refused_n": len(refused), "target": target,
        "fix_label": _kind_label(fix_to) if fix_to else "",
        "ids": ",".join(str(p.pk) for p in products), "targets": _targets(), "can": True,
        "changelist_url": reverse("admin:inventory_product_changelist"),
        "new_location_url": reverse("admin:inventory_location_add"),
    }
    return TemplateResponse(request, TEMPLATE, ctx)


# ── Що лежить на складі (список «📍 Локації складу» і сторінка складу) ──────────

def location_totals() -> dict:
    """{pk локації: {products, units, minus}} — товарів з плюсом, одиниць разом, товарів у мінусі (фізичний
    залишок без резервів). Один запит на всі локації — колонка «На складі» списку локацій."""
    from django.db.models import Sum
    from .models import InventoryTransaction
    out = {}
    rows = (InventoryTransaction.objects.exclude(tx_type=InventoryTransaction.TxType.RESERVED)
            .order_by().values("location_id", "product_id").annotate(t=Sum("qty")))
    for r in rows:
        t = r["t"] or Decimal("0")
        if t == 0:
            continue
        d = out.setdefault(r["location_id"], {"products": 0, "units": Decimal("0"), "minus": 0})
        if t > 0:
            d["products"] += 1
            d["units"] += t
        else:
            d["minus"] += 1
    return out


def totals_html(location, totals):
    """«📦 12 товарів · 340 шт →» (+ «⚠️ 2 у мінусі») — посилання на сторінку складу."""
    t = totals.get(location.pk) or {"products": 0, "units": Decimal("0"), "minus": 0}
    url = reverse("admin:inventory_location_stock", args=[location.pk])
    minus = (format_html(' <span style="color:var(--err,#ef5350)" title="{}">⚠️ у мінусі: {}</span>',
                         "Списали більше, ніж там лежало (рухи з іншого складу)", t["minus"]) if t["minus"] else "")
    if not t["products"] and not t["minus"]:
        return format_html('<a href="{}" style="color:var(--text-dim)">{}</a>', url, "порожньо")
    return format_html('<a href="{}">📦 <b>{}</b> шт · товарів: <b>{}</b> →</a>{}', url, _num(t["units"]),
                       t["products"], minus)


def location_stock_view(ma, request, pk):
    """Сторінка складу: що на ньому лежить (і в мінусі), скільки цього ж товару на інших складах, «🔀»."""
    location = get_object_or_404(Location, pk=pk)
    if not ma.has_view_or_change_permission(request, location):
        raise PermissionDenied
    from django.db.models import Q, Sum
    from .models import InventoryTransaction
    q = (request.GET.get("q") or "").strip()
    qs = (InventoryTransaction.objects.filter(location=location)
          .exclude(tx_type=InventoryTransaction.TxType.RESERVED))
    if q:
        qs = qs.filter(Q(product__sku__icontains=q) | Q(product__name__icontains=q))
    sums = {r["product_id"]: r["t"] for r in qs.order_by().values("product_id").annotate(t=Sum("qty")) if r["t"]}
    products = {p.pk: p for p in Product.objects.filter(pk__in=list(sums))}
    where = stock.on_hand_by_location(list(sums))
    rows = []
    for pid, qty in sums.items():
        p = products.get(pid)
        if p is None:
            continue
        others = [w for w in where.get(pid, []) if w["location_id"] != location.pk]
        rows.append({"product": p, "qty": qty, "qty_txt": _num(qty), "minus": qty < 0,
                     "others": [{"code": w["code"], "qty": _num(w["qty"])} for w in others],
                     "home": bool(where.get(pid)) and where[pid][0]["location_id"] == location.pk})
    rows.sort(key=lambda r: (r["minus"], -r["qty"] if not r["minus"] else r["qty"], r["product"].sku))
    plus = [r for r in rows if not r["minus"]]
    ctx = {
        **ma.admin_site.each_context(request),
        "title": f"📍 {location.code} · {location.name}".strip(" ·"),
        "opts": Location._meta, "original": location, "location": location, "rows": rows, "q": q,
        "products": len(plus), "units": _num(sum((r["qty"] for r in plus), Decimal("0"))),
        "minus": len(rows) - len(plus),
        "can_move": request.user.has_perm("inventory.change_product"),
        "change_url": reverse("admin:inventory_location_change", args=[location.pk]),
        "list_url": reverse("admin:inventory_location_changelist"),
    }
    return TemplateResponse(request, "admin/inventory/location/stock.html", ctx)


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
