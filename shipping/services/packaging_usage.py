"""📊 Коробки: скільки використали, схожі розміри й об'єднання — одна логіка для адмінки, REST API і MCP.

- `usage(days)` — за оформленими відправленнями від нас (`packaging_stock.USED`) за період: скільки коробок кожного
  виду пішло (рядки «Коробки» × «однакових» або розміри самого відправлення; яка коробка — `packaging.material_for`
  серед УСІХ, і архівних — історія лишається за нею), у скількох відправленнях, коли востаннє, скільки рекомендацій
  товарів (`ProductPackaging`) і записів «упаковано» (`OrderPackaging`) на неї посилаються; розміри, яким не
  відповідає жодна коробка (`unmatched`) — кандидати в каталог.
- `similar(tolerance_cm)` — групи коробок, де кожен вимір (у будь-якій орієнтації) відрізняється не більше ніж на
  ±tolerance від сусіда в групі (зв'язні групи); для кожної — пропозиція, у яку зливати (`suggest_target`: найчастіше
  вживана → з кодом → активна → старіша).
- `merge(target, others, user)` — об'єднати: рекомендації товарів і записи «упаковано» → на ціль (рекомендація того
  самого товару вже є в цілі — лишається її, «рекомендована» переходить, якщо була в злитої), залишок (облік на
  складі) → на ціль рухами «🧮 Інвентаризація» з приміткою, злиті — в архів («Активна» знято, у нотатках «→ злито в
  …»). Нічого не видаляється: журнал рухів і відправлення лишаються за своїми коробками. Відправлення з розмірами
  злитої коробки, оформлені після цього, жодної коробки не списують — вписуйте розміри цілі.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

DAYS_MAX = 3650
TOLERANCE_MAX = Decimal("20")
UNMATCHED_MAX = 15
SCAN_MAX = 20000                       # відправлень за період — не більше (найновіші)


def _dims(m):
    from shipping.services.packaging import _dims as dims
    return dims(m)


def dims_text(m) -> str:
    a, b, c = (Decimal(getattr(m, k) or 0) for k in ("length_cm", "width_cm", "height_cm"))
    return " × ".join(f"{v.normalize():f}" for v in (a, b, c))


# ── Використання ──────────────────────────────────────────────────────────────

def usage(days=365) -> dict:
    """{materials: {pk: {boxes, shipments, last, recs, orders}}, unmatched: [{dims, boxes, shipments, last}],
    shipments, since, days}."""
    from shipping.models import OrderPackaging, PackagingMaterial, ProductPackaging, Shipment
    from shipping.services.packaging import shipment_boxes
    from shipping.services.packaging_stock import USED
    days = max(1, min(int(days or 365), DAYS_MAX))
    since = timezone.now() - timedelta(days=days)
    pool = list(PackagingMaterial.objects.order_by("-is_active", "pk"))   # активні — першими, архівні теж
    stats = {m.pk: {"boxes": 0, "shipments": 0, "last": None, "recs": 0, "orders": 0} for m in pool}
    unmatched = {}
    shipments = (Shipment.objects.filter(status__in=USED, created_at__gte=since)
                 .exclude(direction="in").prefetch_related("packages").order_by("-created_at")[:SCAN_MAX])
    n = 0
    for s in shipments:
        n += 1
        when = s.submitted_at or s.created_at
        seen = set()
        for box in shipment_boxes(s, pool):
            qty = max(1, int(box.qty or 1))
            m = box.packaging
            if m is None:
                key = tuple(sorted(Decimal(v or 0).quantize(Decimal("1")) for v in (box.length_cm, box.width_cm,
                                                                                    box.height_cm)))
                u = unmatched.setdefault(key, {"boxes": 0, "shipments": 0, "last": None})
                u["boxes"] += qty
                u["shipments"] += 1
                u["last"] = max(filter(None, (u["last"], when)), default=None)
                continue
            st = stats[m.pk]
            st["boxes"] += qty
            if m.pk not in seen:
                st["shipments"] += 1
                seen.add(m.pk)
            st["last"] = max(filter(None, (st["last"], when)), default=None)
    for row in ProductPackaging.objects.values("packaging").annotate(n=Count("pk")):
        if row["packaging"] in stats:
            stats[row["packaging"]]["recs"] = row["n"]
    for row in (OrderPackaging.objects.filter(created_at__gte=since).values("packaging")
                .annotate(n=Sum("qty_boxes"))):
        if row["packaging"] in stats:
            stats[row["packaging"]]["orders"] = row["n"] or 0
    top = sorted(unmatched.items(), key=lambda kv: -kv[1]["boxes"])[:UNMATCHED_MAX]
    return {"materials": stats, "unmatched": [{"dims": " × ".join(f"{d:f}" for d in reversed(k)), **v} for k, v in top],
            "shipments": n, "since": since, "days": days}


CACHE_SECONDS = 600


def usage_cached(days=365) -> dict:
    """`usage` з кешем на 10 хв (колонка списку коробок, MCP) — злиття кеш скидає (`forget`)."""
    from core.shared_cache import cache
    key = f"pack-usage:{int(days)}"
    data = cache.get(key)
    if data is None:
        data = usage(days)
        cache.set(key, data, CACHE_SECONDS)
    return data


def forget():
    from core.shared_cache import cache
    cache.delete_many([f"pack-usage:{d}" for d in (30, 90, 180, 365, 730)])


# ── Схожі розміри ─────────────────────────────────────────────────────────────

def _close(a, b, tol) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(_dims(a), _dims(b)))


def similar(tolerance_cm=1, include_inactive=False, stats=None) -> list:
    """[{members: [коробки, ціль першою], target, spread: {l, w, h}}] — зв'язні групи (±tolerance кожен вимір)."""
    from shipping.models import PackagingMaterial
    tol = max(Decimal("0"), min(Decimal(str(tolerance_cm or 0)), TOLERANCE_MAX))
    qs = PackagingMaterial.objects.all() if include_inactive else PackagingMaterial.objects.filter(is_active=True)
    items = list(qs.order_by("pk"))
    parent = {m.pk: m.pk for m in items}

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if a.box_type == b.box_type and _close(a, b, tol):
                parent[root(a.pk)] = root(b.pk)
    groups = {}
    for m in items:
        groups.setdefault(root(m.pk), []).append(m)
    stats = stats if stats is not None else usage()["materials"]
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        target = suggest_target(members, stats)
        members = [target] + [m for m in members if m.pk != target.pk]
        dims = list(zip(*(_dims(m) for m in members)))
        out.append({"members": members, "target": target,
                    "spread": [f"{(max(d) - min(d)).normalize():f}" for d in dims]})
    return sorted(out, key=lambda g: -sum(stats.get(m.pk, {}).get("boxes", 0) for m in g["members"]))


def suggest_target(members, stats) -> object:
    return max(members, key=lambda m: (stats.get(m.pk, {}).get("boxes", 0), stats.get(m.pk, {}).get("recs", 0),
                                       bool(m.code), m.is_active, -m.pk))


# ── Об'єднання ────────────────────────────────────────────────────────────────

class MergeError(Exception):
    pass


def merge(target, others, user=None) -> dict:
    """Злити коробки others у target. → {moved_recs, dropped_recs, moved_orders, stock, archived}."""
    from shipping.models import OrderPackaging, PackagingMaterial, ProductPackaging
    from shipping.services import packaging_stock
    others = [o for o in others if o.pk != target.pk]
    if not others:
        raise MergeError("виберіть, які коробки злити в цю")
    if any(o.box_type != target.box_type for o in others):
        raise MergeError("зливати можна лише коробки одного типу (коробку з коробкою, конверт з конвертом…)")
    out = {"moved_recs": 0, "dropped_recs": 0, "moved_orders": 0, "stock": 0, "archived": []}
    label = f"{target} ({dims_text(target)})"
    with transaction.atomic():
        target = PackagingMaterial.objects.select_for_update().get(pk=target.pk)
        for o in PackagingMaterial.objects.select_for_update().filter(pk__in=[o.pk for o in others]):
            for rec in ProductPackaging.objects.filter(packaging=o):
                mine = ProductPackaging.objects.filter(packaging=target, product_id=rec.product_id).first()
                if mine is None:
                    ProductPackaging.objects.filter(pk=rec.pk).update(packaging=target)
                    out["moved_recs"] += 1
                    continue
                updates = {}
                if rec.is_default and not mine.is_default:
                    updates["is_default"] = True
                if (rec.qty_per_box or 0) > (mine.qty_per_box or 0):
                    updates["qty_per_box"] = rec.qty_per_box       # більша місткість — як у навчанні упаковки
                if updates:
                    ProductPackaging.objects.filter(pk=mine.pk).update(**updates)
                rec.delete()
                out["dropped_recs"] += 1
            out["moved_orders"] += OrderPackaging.objects.filter(packaging=o).update(packaging=target)
            if o.track_stock and o.stock_qty:
                qty = o.stock_qty
                packaging_stock._move(o.pk, -qty, "adjust", note=f"Злито в {label}", user=user)
                if target.track_stock:
                    packaging_stock._move(target.pk, qty, "adjust", note=f"Злито з {o} ({dims_text(o)})", user=user)
                    out["stock"] += qty
            note = f"→ злито в #{target.pk} {label} {timezone.localtime():%d.%m.%Y}"
            PackagingMaterial.objects.filter(pk=o.pk).update(
                is_active=False, notes=((o.notes or "").rstrip() + "\n" + note).strip())
            out["archived"].append(o.pk)
    forget()
    try:
        from core.models import AuditLog
        AuditLog.log(AuditLog.Action.UPDATE, module="shipping", model_name="PackagingMaterial", object_id=target.pk,
                     object_repr=str(target)[:200], user=user,
                     extra={"merged_into": target.pk, **{k: v for k, v in out.items()}})
    except Exception:  # noqa: BLE001 — журнал не обов'язковий для самого злиття
        pass
    return out


def stats_line(st) -> str:
    if not st or not (st["boxes"] or st["recs"] or st["orders"]):
        return "не використовувалась"
    last = timezone.localtime(st["last"]).strftime("%d.%m.%Y") if st["last"] else "—"
    return (f"{st['boxes']} шт у {st['shipments']} відправл. · востаннє {last} · рекомендацій товарів {st['recs']}"
            + (f" · «упаковано» {st['orders']}" if st["orders"] else ""))
