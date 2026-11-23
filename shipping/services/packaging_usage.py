"""📊 Коробки: скільки використали, схожі розміри й об'єднання — одна логіка для адмінки, REST API і MCP.

- `usage(days)` — за оформленими відправленнями від нас (`packaging_stock.USED`) за період: скільки коробок кожного
  виду пішло (рядки «Коробки» × «однакових» або розміри самого відправлення; яка коробка — `packaging.material_for`
  серед УСІХ, і архівних — історія лишається за нею; злита — `packaging.merged_target`: рахується в ціль), у скількох
  відправленнях, коли востаннє, скільки рекомендацій
  товарів (`ProductPackaging`) і записів «упаковано» (`OrderPackaging`) на неї посилаються; розміри, яким не
  відповідає жодна коробка (`unmatched`) — кандидати в каталог.
- `similar(tolerance_cm)` — групи коробок, де кожен вимір (у будь-якій орієнтації) відрізняється не більше ніж на
  ±tolerance від сусіда в групі (зв'язні групи); для кожної — пропозиція, у яку зливати (`suggest_target`: найчастіше
  вживана → з кодом → старіша; ціль — лише активна, архівних у групі — лише з include_inactive).
- `merge(target, others, user)` — об'єднати: рекомендації товарів і записи «упаковано» → на ціль (рекомендація того
  самого товару вже є в цілі — лишається її, «рекомендована» переходить, якщо була в злитої), залишок (облік на
  складі) → на ціль рухами «🧮 Інвентаризація» з приміткою, злиті — в архів («Активна» знято, у нотатках «→ злито в
  …», `merged_into` = ціль). Нічого не видаляється: журнал рухів і відправлення лишаються за своїми коробками, але
  відправлення з розмірами злитої коробки (і давні, і нові) рахуються в ціль, нові — списують ціль (`packaging_stock`
  бере злиті в пул і `merged_target`). Зливати можна й архівну коробку (її рекомендації товарів досі діють у
  рекомендованій упаковці) — у ціль, яка лише активна. Повернули злиту з архіву — знову окрема (`save`).
  Кожне злиття — `PackagingMerge`: `snapshot` (кожна коробка за весь час до злиття: штук, відправлень, востаннє,
  рекомендацій, «упаковано», запас; сума), `result`, `undo_data` (що саме перенесено / видалено / змінено).
- `history(target=, limit=)` — злиття з «до злиття», «зараз у цілі» й «після злиття +N», `can_undo`.
- `undo(m, user)` — «↩ Відкотити»: лише останнє злиття цих коробок (`blocker`); рекомендації, «упаковано», запас
  (рухи «Відкат злиття»), архів, нотатки й попередні «злито в» — як були; відправлення знову рахуються за своїми.
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
    from shipping.services.packaging import merged_target, shipment_boxes
    from shipping.services.packaging_stock import USED
    days = max(1, min(int(days or 365), DAYS_MAX))
    since = timezone.now() - timedelta(days=days)
    pool = list(PackagingMaterial.objects.order_by("-is_active", "pk"))   # активні — першими, архівні теж
    by_pk = {m.pk: m for m in pool}
    stats = {m.pk: {"boxes": 0, "shipments": 0, "last": None, "recs": 0, "orders": 0, "merged": 0} for m in pool}
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
            m = merged_target(box.packaging, by_pk)              # злита — рахується в ціль
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
            if m is not box.packaging:
                st["merged"] += qty                                # з них — з розмірами злитих коробок
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
    qs = qs.filter(merged_into__isnull=True)                    # уже злиті — не пропонувати знову
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
    """Активна першою (в архівну зливати не можна), далі — найчастіше вживана, з рекомендаціями, з кодом, старіша."""
    return max(members, key=lambda m: (m.is_active, stats.get(m.pk, {}).get("boxes", 0),
                                       stats.get(m.pk, {}).get("recs", 0), bool(m.code), -m.pk))


# ── Об'єднання ────────────────────────────────────────────────────────────────

class MergeError(Exception):
    pass


def _box_snapshot(m, st) -> dict:
    st = st or {}
    last = st.get("last")
    return {"id": m.pk, "code": m.code, "name": str(m), "dims": dims_text(m), "active": m.is_active,
            "boxes": st.get("boxes", 0), "shipments": st.get("shipments", 0), "merged": st.get("merged", 0),
            "last": last.isoformat() if last else None, "recs": st.get("recs", 0), "orders": st.get("orders", 0),
            "stock": m.stock_qty if m.track_stock else None}


def merge(target, others, user=None, via="") -> dict:
    """Злити коробки others у target. → {moved_recs, dropped_recs, moved_orders, stock, archived, merge_id}."""
    from shipping.models import OrderPackaging, PackagingMaterial, PackagingMerge, ProductPackaging
    from shipping.services import packaging_stock
    others = [o for o in others if o.pk != target.pk]
    if not others:
        raise MergeError("виберіть, які коробки злити в цю")
    if not target.is_active:
        raise MergeError(f"коробка-ціль «{target}» в архіві — виберіть активну або спершу поверніть її з архіву")
    if any(o.box_type != target.box_type for o in others):
        raise MergeError("зливати можна лише коробки одного типу (коробку з коробкою, конверт з конвертом…)")
    out = {"moved_recs": 0, "dropped_recs": 0, "moved_orders": 0, "stock": 0, "archived": []}
    label = f"{target} ({dims_text(target)})"
    now = timezone.now()
    before = usage(DAYS_MAX)["materials"]                  # «до злиття» — за весь час
    undo_data = {"recs_moved": [], "recs_dropped": [], "target_recs": [], "orders": [], "stock": [], "boxes": [],
                 "chain": []}
    with transaction.atomic():
        target = PackagingMaterial.objects.select_for_update().get(pk=target.pk)
        if not target.is_active:
            raise MergeError(f"коробка-ціль «{target}» в архіві — виберіть активну або спершу поверніть її з архіву")
        boxes = list(PackagingMaterial.objects.select_for_update().filter(pk__in=[o.pk for o in others]))
        snapshot = {"days": DAYS_MAX, "target": _box_snapshot(target, before.get(target.pk)),
                    "merged": [_box_snapshot(o, before.get(o.pk)) for o in boxes]}
        rows = [snapshot["target"], *snapshot["merged"]]
        snapshot["total"] = {"boxes": sum(r["boxes"] for r in rows), "shipments": sum(r["shipments"] for r in rows),
                             "recs": sum(r["recs"] for r in rows), "orders": sum(r["orders"] for r in rows)}
        for o in boxes:
            for rec in ProductPackaging.objects.filter(packaging=o):
                mine = ProductPackaging.objects.filter(packaging=target, product_id=rec.product_id).first()
                if mine is None:
                    ProductPackaging.objects.filter(pk=rec.pk).update(packaging=target)
                    undo_data["recs_moved"].append({"pk": rec.pk, "from": o.pk})
                    out["moved_recs"] += 1
                    continue
                updates = {}
                if rec.is_default and not mine.is_default:
                    updates["is_default"] = True
                if (rec.qty_per_box or 0) > (mine.qty_per_box or 0):
                    updates["qty_per_box"] = rec.qty_per_box       # більша місткість — як у навчанні упаковки
                if updates:
                    undo_data["target_recs"].append({"pk": mine.pk, "old": {k: getattr(mine, k) for k in updates}})
                    ProductPackaging.objects.filter(pk=mine.pk).update(**updates)
                    for k, v in updates.items():
                        setattr(mine, k, v)
                undo_data["recs_dropped"].append({
                    "packaging": o.pk, "product": rec.product_id, "qty_per_box": rec.qty_per_box,
                    "estimated_weight_g": rec.estimated_weight_g, "is_default": rec.is_default, "notes": rec.notes})
                rec.delete()
                out["dropped_recs"] += 1
            order_pks = list(OrderPackaging.objects.filter(packaging=o).values_list("pk", flat=True))
            if order_pks:
                undo_data["orders"].append({"from": o.pk, "pks": order_pks})
                out["moved_orders"] += OrderPackaging.objects.filter(pk__in=order_pks).update(packaging=target)
            if o.track_stock and o.stock_qty:
                qty = o.stock_qty
                packaging_stock._move(o.pk, -qty, "adjust", note=f"Злито в {label}", user=user)
                if target.track_stock:
                    packaging_stock._move(target.pk, qty, "adjust", note=f"Злито з {o} ({dims_text(o)})", user=user)
                    out["stock"] += qty
                undo_data["stock"].append({"box": o.pk, "qty": qty, "to_target": bool(target.track_stock)})
            note = f"→ злито в #{target.pk} {label} {timezone.localtime():%d.%m.%Y}"
            notes_after = ((o.notes or "").rstrip() + "\n" + note).strip()
            undo_data["boxes"].append({"pk": o.pk, "is_active": o.is_active, "merged_into": o.merged_into_id,
                                       "merged_at": o.merged_at.isoformat() if o.merged_at else None,
                                       "notes": o.notes, "notes_after": notes_after})
            PackagingMaterial.objects.filter(pk=o.pk).update(
                is_active=False, merged_into=target, merged_at=now, notes=notes_after)
            chain = list(PackagingMaterial.objects.filter(merged_into=o).values_list("pk", flat=True))
            if chain:                                            # ланцюжок — в одну ціль
                undo_data["chain"].append({"to": o.pk, "pks": chain})
                PackagingMaterial.objects.filter(pk__in=chain).update(merged_into=target)
            out["archived"].append(o.pk)
        record = PackagingMerge.objects.create(target=target, user=user if getattr(user, "pk", None) else None,
                                               via=str(via or "")[:20], snapshot=snapshot, undo_data=undo_data,
                                               result={k: v for k, v in out.items()})
        record.boxes.set(boxes)
        out["merge_id"] = record.pk
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
    merged = f" (з них {st['merged']} — з розмірами злитих коробок)" if st.get("merged") else ""
    return (f"{st['boxes']} шт у {st['shipments']} відправл.{merged} · востаннє {last} · рекомендацій товарів "
            f"{st['recs']}" + (f" · «упаковано» {st['orders']}" if st["orders"] else ""))


# ── 🔀 Історія злиттів і відкат ───────────────────────────────────────────────

def blocker(m):
    """Пізніше невідкочене злиття тих самих коробок (ціль або злиті) — його треба відкотити першим. → злиття / None."""
    from django.db.models import Q
    from shipping.models import PackagingMerge
    ids = {m.target_id, *m.boxes.values_list("pk", flat=True)} - {None}
    return (PackagingMerge.objects.filter(undone_at__isnull=True, created_at__gte=m.created_at)
            .exclude(pk=m.pk).filter(pk__gt=m.pk).filter(Q(target_id__in=ids) | Q(boxes__in=ids))
            .order_by("-pk").first())


def history(target=None, limit=20) -> list:
    """Злиття (найновіші першими): {merge, at, user, via, target, before [ціль, злиті], total, now, after,
    undone, can_undo, blocked_by}. «Зараз» — використання цілі за той самий період (весь час), «після» — різниця."""
    from shipping.models import PackagingMerge
    qs = PackagingMerge.objects.select_related("target", "user", "undone_by").prefetch_related("boxes")
    if target is not None:
        qs = qs.filter(target=target)
    items = list(qs[:limit])
    if not items:
        return []
    now_stats = usage_cached(DAYS_MAX)["materials"]
    out = []
    for m in items:
        snap = m.snapshot or {}
        total = snap.get("total") or {}
        now = (now_stats.get(m.target_id) or {}).get("boxes", 0) if m.target_id else 0
        block = None if m.undone_at else blocker(m)
        out.append({"merge": m, "at": m.created_at, "user": m.user, "via": m.via, "target": m.target,
                    "before": [snap.get("target") or {}, *(snap.get("merged") or [])], "total": total,
                    "now": now, "after": max(0, now - (total.get("boxes") or 0)), "undone": m.undone_at,
                    "undone_by": m.undone_by, "can_undo": not m.undone_at and block is None, "blocked_by": block})
    return out


def _restore_notes(current, before, after):
    if (current or "") == (after or ""):
        return before or ""
    line = (after or "").splitlines()[-1] if after else ""
    return "\n".join(x for x in (current or "").splitlines() if x != line).strip() if line else current


def undo(m, user=None) -> dict:
    """↩ Відкотити злиття m: усе, що воно перенесло, — назад. → {recs, dropped, orders, stock, boxes}."""
    from shipping.models import OrderPackaging, PackagingMaterial, PackagingMerge, ProductPackaging
    from shipping.services import packaging_stock
    from django.utils.dateparse import parse_datetime
    out = {"recs": 0, "dropped": 0, "orders": 0, "stock": 0, "boxes": 0}
    with transaction.atomic():
        m = PackagingMerge.objects.select_for_update().get(pk=m.pk)
        if m.undone_at:
            raise MergeError("це злиття вже відкочено")
        block = blocker(m)
        if block is not None:
            when = timezone.localtime(block.created_at).strftime("%d.%m.%Y %H:%M")
            raise MergeError(f"спершу відкотіть пізніше злиття #{block.pk} ({when}) — воно зачіпає ті самі коробки")
        d = m.undo_data or {}
        target_pk = m.target_id
        note = f"Відкат злиття #{m.pk}"
        for c in reversed(d.get("chain") or []):
            PackagingMaterial.objects.filter(pk__in=c["pks"], merged_into_id=target_pk).update(merged_into_id=c["to"])
        for b in reversed(d.get("boxes") or []):
            box = PackagingMaterial.objects.select_for_update().filter(pk=b["pk"]).first()
            if box is None:
                continue
            PackagingMaterial.objects.filter(pk=box.pk).update(
                is_active=b["is_active"], merged_into_id=b["merged_into"],
                merged_at=parse_datetime(b["merged_at"]) if b.get("merged_at") else None,
                notes=_restore_notes(box.notes, b.get("notes"), b.get("notes_after")))
            out["boxes"] += 1
        for s in reversed(d.get("stock") or []):
            if s.get("to_target") and target_pk:
                packaging_stock._move(target_pk, -s["qty"], "adjust", note=f"{note}: назад у #{s['box']}", user=user)
            packaging_stock._move(s["box"], s["qty"], "adjust", note=note, user=user)
            out["stock"] += s["qty"]
        for o in d.get("orders") or []:
            out["orders"] += OrderPackaging.objects.filter(pk__in=o["pks"], packaging_id=target_pk).update(
                packaging_id=o["from"])
        for r in reversed(d.get("target_recs") or []):
            ProductPackaging.objects.filter(pk=r["pk"]).update(**r["old"])
        for r in d.get("recs_dropped") or []:
            if not PackagingMaterial.objects.filter(pk=r["packaging"]).exists():
                continue
            if ProductPackaging.objects.filter(packaging_id=r["packaging"], product_id=r["product"]).exists():
                continue
            ProductPackaging.objects.create(packaging_id=r["packaging"], product_id=r["product"],
                                            qty_per_box=r["qty_per_box"], estimated_weight_g=r["estimated_weight_g"],
                                            is_default=r["is_default"], notes=r.get("notes") or "")
            out["dropped"] += 1
        for r in d.get("recs_moved") or []:
            out["recs"] += ProductPackaging.objects.filter(pk=r["pk"], packaging_id=target_pk).update(
                packaging_id=r["from"])
        m.undone_at = timezone.now()
        m.undone_by = user if getattr(user, "pk", None) else None
        m.save(update_fields=["undone_at", "undone_by"])
    forget()
    try:
        from core.models import AuditLog
        AuditLog.log(AuditLog.Action.UPDATE, module="shipping", model_name="PackagingMerge", object_id=m.pk,
                     object_repr=str(m)[:200], user=user, extra={"undo": out})
    except Exception:  # noqa: BLE001
        pass
    return out
