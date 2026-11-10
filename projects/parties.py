"""🔗 Сторони проєкту й ланцюжок постачання (projects.ProjectParty): хто бере участь і кому віддає свою частину.

- suggest(project) — сторони з даних проєкту без AI: замовник (клієнт проєкту), ми (назва фірми), постачальники
  закупівель проєкту, JLCPCB (виробник плат), учасники-постачальники; лише ті, яких ще немає; нові «віддають» нам,
  ми — замовнику.
- save / delete — зміни людини (кому віддає — без кіл; видалена сторона: її постачальники віддають далі, кроки плану
  лишаються без сторони).
- statuses(project) — стан сторони: закупівлі її постачальника, JLC-замовлення, кроки плану зі стороною.
- render(project, parties, statuses, can_change) — SVG-дерево: товар іде зліва направо до замовника, гілки — кілька
  сторін, що віддають одній; рамка — стан (прострочено / чекаємо / готово).
"""
from html import escape

from django.db import transaction
from django.utils import timezone

from .models import ProjectLink, ProjectParty, ProjectStep

P = ProjectParty
ROLE_SIDE = {P.CUSTOMER: ProjectStep.CUSTOMER, P.WE: ProjectStep.WE, P.CONTRACTOR: ProjectStep.SUPPLIER,
             P.MANUFACTURER: ProjectStep.SUPPLIER, P.SUPPLIER: ProjectStep.SUPPLIER,
             P.LOGISTICS: ProjectStep.SUPPLIER, P.OTHER: ProjectStep.COLLEAGUE}
ROLE_ICON = {k: v.split(" ", 1)[0] for k, v in P.ROLE_CHOICES}
ROLE_NAME = {k: v.split(" ", 1)[1] for k, v in P.ROLE_CHOICES}
JLC = "JLCPCB"
MAX_PARTIES = 60

NODE_W, NODE_H, COL_GAP, ROW_GAP, PAD = 210, 62, 70, 16, 12


class PartyError(Exception):
    pass


def company_name() -> str:
    try:
        from config.models import SystemSettings
        return (SystemSettings.get().company_name or "").strip() or "Ми"
    except Exception:  # noqa: BLE001
        return "Ми"


def _fmt(d) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


def _purchases(project):
    from inventory.models import PurchaseOrder
    ids = (ProjectLink.objects.filter(project=project, status=ProjectLink.CONFIRMED)
           .exclude(purchase_order=None).values_list("purchase_order_id", flat=True))
    return list(PurchaseOrder.objects.filter(pk__in=list(ids)).select_related("supplier")
                .prefetch_related("lines__product", "jlc_orders").order_by("order_date", "pk"))


def _jlc(project, purchases):
    from .overview import _jlc as jlc_orders
    return jlc_orders(project, purchases)


@transaction.atomic
def suggest(project) -> int:
    """«🔎 Підставити з даних проєкту»: додає відсутні сторони. → скільки додано."""
    from inventory.models import Supplier
    from .portfolio import customer_name
    have = list(project.parties.all())
    if len(have) >= MAX_PARTIES:
        return 0
    added = []
    order = max([p.order for p in have] or [0])

    def find(role=None, name="", supplier=None, customer=None):
        for p in have + added:
            if supplier is not None and p.supplier_id == supplier.pk:
                return p
            if customer is not None and p.customer_id == customer.pk:
                return p
            if name and p.name.strip().lower() == name.strip().lower() and (role is None or p.role == role):
                return p
        return None

    def add(role, name, **kw):
        nonlocal order
        found = find(role, name, kw.get("supplier"), kw.get("customer"))
        if found is not None:
            return found
        order += 10
        p = ProjectParty.objects.create(project=project, role=role, name=name[:200], order=order, **kw)
        added.append(p)
        return p

    we = next((p for p in have if p.role == P.WE), None) or add(P.WE, company_name())
    c = project.customer
    if c is not None:
        cust = add(P.CUSTOMER, customer_name(c) or str(c), customer=c, email=(c.email or "")[:254])
        if we.delivers_to_id is None and we.pk != cust.pk:
            we.delivers_to = cust
            we.save(update_fields=["delivers_to"])
    purchases = _purchases(project)
    for po in purchases:
        if not po.supplier_id:
            continue
        skus = []
        for line in po.lines.all():
            label = line.product.sku if line.product_id else (line.description or "")
            if label and label not in skus:
                skus.append(label)
        add(P.SUPPLIER, po.supplier.name, supplier=po.supplier, email=(po.supplier.email or "")[:254],
            what=", ".join(skus[:4])[:255], delivers_to=we)
    if _jlc(project, purchases):
        add(P.MANUFACTURER, JLC, what="друковані плати / складання", delivers_to=we)
    for part in project.participants.filter(role="supplier"):
        sup = Supplier.objects.filter(email__iexact=part.email).first()
        if sup is not None:
            add(P.SUPPLIER, sup.name, supplier=sup, email=part.email, delivers_to=we)
        else:
            add(P.SUPPLIER, part.name or part.email, email=part.email, delivers_to=we)
    return len(added)


def _would_cycle(party, parent) -> bool:
    seen = set()
    cur = parent
    while cur is not None:
        if cur.pk == party.pk or cur.pk in seen:
            return True
        seen.add(cur.pk)
        cur = cur.delivers_to
    return False


@transaction.atomic
def save(project, data) -> ProjectParty:
    """Створити / змінити сторону: {id, role, name, what, delivers_to, email, note}."""
    from inventory.models import Supplier
    name = " ".join(str(data.get("name") or "").split())[:200]
    if not name:
        raise PartyError("Напишіть назву сторони")
    party = (ProjectParty.objects.filter(project=project, pk=data.get("id")).first()
             if str(data.get("id") or "").isdigit() else None)
    if data.get("id") and party is None:
        raise PartyError("Сторону не знайдено")
    if party is None:
        if project.parties.count() >= MAX_PARTIES:
            raise PartyError(f"У проєкті вже {MAX_PARTIES} сторін")
        party = ProjectParty(project=project, order=(project.parties.order_by("-order")
                                                     .values_list("order", flat=True).first() or 0) + 10)
    role = data.get("role")
    party.role = role if role in dict(P.ROLE_CHOICES) else P.SUPPLIER
    party.name = name
    party.what = " ".join(str(data.get("what") or "").split())[:255]
    party.email = str(data.get("email") or "").strip().lower()[:254]
    party.note = " ".join(str(data.get("note") or "").split())[:255]
    parent_id = data.get("delivers_to")
    parent = (ProjectParty.objects.filter(project=project, pk=parent_id).first()
              if str(parent_id or "").isdigit() else None)
    if parent is not None and party.pk and _would_cycle(party, parent):
        raise PartyError("Так вийде коло: ця сторона вже постачає (через інших) тій, кому ви хочете віддавати")
    party.delivers_to = parent
    if party.role in (P.SUPPLIER, P.MANUFACTURER, P.CONTRACTOR, P.LOGISTICS) and not party.supplier_id:
        party.supplier = Supplier.objects.filter(name__iexact=name).first()
    party.save()
    # кроки плану цієї сторони — «Хто» за роллю (чекаємо клієнта / постачальника)
    ProjectStep.objects.filter(party=party).exclude(side=ROLE_SIDE[party.role]).update(side=ROLE_SIDE[party.role])
    return party


@transaction.atomic
def delete(project, pk) -> str:
    party = ProjectParty.objects.filter(project=project, pk=pk).first()
    if party is None:
        raise PartyError("Сторону не знайдено")
    ProjectParty.objects.filter(delivers_to=party).update(delivers_to=party.delivers_to)
    name = party.name
    party.delete()
    return name


def statuses(project, parties, today=None) -> dict:
    """{pk сторони: [{text, cls}]} — закупівлі її постачальника, JLC-замовлення (виробник JLCPCB), кроки плану."""
    today = today or timezone.localdate()
    out = {p.pk: [] for p in parties}
    purchases = _purchases(project)
    by_supplier = {}
    for po in purchases:
        if po.supplier_id:
            by_supplier.setdefault(po.supplier_id, []).append(po)
    jlc = _jlc(project, purchases)
    steps = {}
    for s in ProjectStep.objects.filter(project=project).exclude(party=None):
        steps.setdefault(s.party_id, []).append(s)
    for p in parties:
        lines = out[p.pk]
        for po in by_supplier.get(p.supplier_id, []) if p.supplier_id else []:
            if po.status == po.Status.RECEIVED:
                lines.append({"text": f"{po} · отримано {_fmt(po.received_date)}", "cls": "ok"})
            elif po.status == po.Status.CANCELLED:
                continue
            elif po.status == po.Status.DRAFT:
                lines.append({"text": f"{po} · чернетка", "cls": "open"})
            elif po.expected_date and po.expected_date < today:
                lines.append({"text": f"{po} · чекали {_fmt(po.expected_date)}", "cls": "bad"})
            else:
                lines.append({"text": f"{po} · очікується {_fmt(po.expected_date)}" if po.expected_date
                              else f"{po} · замовлено", "cls": "warn"})
        if p.role == P.MANUFACTURER and p.name.strip().upper().startswith("JLC"):
            for j in jlc:
                num = j.jlc_order_number or j.jlc_order_id
                if j.local_status == j.LocalStatus.DELIVERED:
                    lines.append({"text": f"JLC {num} · доставлено", "cls": "ok"})
                elif j.local_status == j.LocalStatus.CANCELLED:
                    continue
                elif j.expected_date and j.expected_date < today:
                    lines.append({"text": f"JLC {num} · чекали {_fmt(j.expected_date)}", "cls": "bad"})
                else:
                    lines.append({"text": f"JLC {num} · {j.get_local_status_display()}", "cls": "warn"})
        mine = steps.get(p.pk, [])
        if mine:
            open_ = [s for s in mine if (s.eff_status or s.status) not in ("done", "skipped")]
            late = [s for s in open_ if (s.f_end or s.end) and (s.f_end or s.end) < today]
            if late:
                lines.append({"text": f"кроків прострочено: {len(late)} — «{late[0].title}»", "cls": "bad"})
            elif open_:
                lines.append({"text": f"відкритих кроків: {len(open_)}", "cls": "open"})
            else:
                lines.append({"text": f"усі кроки виконано ({len(mine)})", "cls": "ok"})
    return out


def worst(lines) -> str:
    keys = [x["cls"] for x in lines]
    for k in ("bad", "warn", "ok"):
        if k in keys:
            return k
    return "open"


def _cut(text, n) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def layout(parties):
    """→ ([(party, depth, row)], max_depth, rows): корені (кому ніхто не віддає далі) — праворуч; гілки — ліворуч."""
    by_pk = {p.pk: p for p in parties}
    kids = {p.pk: [] for p in parties}
    roots = []
    for p in parties:
        if p.delivers_to_id in by_pk and p.delivers_to_id != p.pk:
            kids[p.delivers_to_id].append(p)
        else:
            roots.append(p)
    first = {P.CUSTOMER: 0, P.WE: 1}
    roots.sort(key=lambda p: (first.get(p.role, 2), p.order, p.pk))
    placed, seen, row = [], set(), [0]

    def walk(p, depth):
        if p.pk in seen:
            return None
        seen.add(p.pk)
        children = [walk(c, depth + 1) for c in sorted(kids[p.pk], key=lambda c: (c.order, c.pk))]
        children = [c for c in children if c is not None]
        if children:
            r = (children[0] + children[-1]) / 2
        else:
            r = row[0]
            row[0] += 1
        placed.append((p, depth, r))
        return r

    for r in roots:
        walk(r, 0)
        row[0] += 0.4                                       # проміжок між окремими деревами
    for p in parties:                                       # залишки кіл (не мали б бути) — окремо
        if p.pk not in seen:
            walk(p, 0)
    depth = max([d for _, d, _ in placed] or [0])
    return placed, depth, max(row[0] - 0.4, 1)


def render(parties, stats, can_change=False) -> str:
    if not parties:
        return ""
    placed, max_depth, rows = layout(parties)
    W = PAD * 2 + (max_depth + 1) * NODE_W + max_depth * COL_GAP
    H = int(PAD * 2 + rows * (NODE_H + ROW_GAP))
    pos = {}
    for p, depth, r in placed:
        x = PAD + (max_depth - depth) * (NODE_W + COL_GAP)
        y = PAD + r * (NODE_H + ROW_GAP)
        pos[p.pk] = (x, y)
    out = [f'<svg class="pjy" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="Сторони проєкту: хто кому постачає" xmlns="http://www.w3.org/2000/svg">',
           '<defs><marker id="pjy-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
           'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="pjy-arrowhead"/></marker></defs>']
    for p, _depth, _r in placed:
        if p.delivers_to_id in pos and p.delivers_to_id != p.pk:
            x1, y1 = pos[p.pk]
            x2, y2 = pos[p.delivers_to_id]
            sx, sy, ex, ey = x1 + NODE_W, y1 + NODE_H / 2, x2, y2 + NODE_H / 2
            mx = (sx + ex) / 2
            out.append(f'<path d="M{sx:.1f},{sy:.1f} C{mx:.1f},{sy:.1f} {mx:.1f},{ey:.1f} {ex - 2:.1f},{ey:.1f}" '
                       f'class="pjy-edge pjy-e-{worst(stats.get(p.pk, []))}" marker-end="url(#pjy-arrow)"/>')
    for p, _depth, _r in placed:
        x, y = pos[p.pk]
        lines = stats.get(p.pk, [])
        state = worst(lines)
        tip = [f"{ROLE_ICON[p.role]} {ROLE_NAME[p.role]}: {p.name}"]
        if p.what:
            tip.append(f"Що: {p.what}")
        if p.delivers_to_id in pos:
            tip.append(f"Віддає: {next(q.name for q, _, _ in placed if q.pk == p.delivers_to_id)}")
        if p.email:
            tip.append(p.email)
        tip += [x["text"] for x in lines]
        if p.note:
            tip.append(p.note)
        attrs = f' data-party="{p.pk}" tabindex="0" role="button"' if can_change else ""
        sub = lines[0]["text"] if lines and state in ("bad", "warn") else (p.what or (lines[0]["text"] if lines else ""))
        out.append(
            f'<g class="pjy-node pjy-r-{p.role} pjy-{state}"{attrs}><title>{escape(chr(10).join(tip))}</title>'
            f'<rect x="{x}" y="{y}" width="{NODE_W}" height="{NODE_H}" rx="8" class="pjy-box"/>'
            f'<text x="{x + 10}" y="{y + 16}" class="pjy-role">{escape(ROLE_ICON[p.role])} {escape(ROLE_NAME[p.role])}</text>'
            f'<text x="{x + 10}" y="{y + 35}" class="pjy-name">{escape(_cut(p.name, 28))}</text>'
            f'<text x="{x + 10}" y="{y + 52}" class="pjy-sub pjy-s-{state}">{escape(_cut(sub, 34))}</text></g>')
    out.append("</svg>")
    return "".join(out)


def context(project, can_change, today=None) -> dict:
    """Для вкладки «🔗 Сторони» пульта: SVG, список для вікна, підказка, чи є що підставити."""
    from django.db.models import Count
    parties = list(project.parties.select_related("delivers_to", "supplier", "customer").annotate(n_steps=Count("steps")))
    stats = statuses(project, parties, today)
    return {
        "count": len(parties),
        "svg": render(parties, stats, can_change),
        "roles": P.ROLE_CHOICES,
        "data": {"roles": [[k, v] for k, v in P.ROLE_CHOICES],
                 "parties": [{"id": p.pk, "role": p.role, "name": p.name, "what": p.what,
                              "delivers_to": p.delivers_to_id or "", "email": p.email, "note": p.note,
                              "steps": p.n_steps} for p in parties]},
    }
