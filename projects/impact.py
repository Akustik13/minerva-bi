"""⚡ Впливи між проєктами — без AI, з бази й кешу плану (schedule.persist: f_start / f_end / eff_status кроків).

  ⛓ dep      — крок залежить від кроку іншого проєкту, а той закінчиться пізніше, ніж план цього кроку;
  📦 stock    — спільний товар: відкриті замовлення й Angebote кількох проєктів потребують більше, ніж є на
               складі для них і очікується за закупівлями;
  🏭 supplier — відкриті закупівлі кількох проєктів в одного постачальника одночасно (прострочена — зачепить усі);
  👷 load     — у людини на тижні більше LOAD_MAX кроків у кількох проєктах.

compute(today, focus=None) → [Impact] (focus — лише впливи, що стосуються цього проєкту; портфель — усі),
attention(project, add, today) → пункти «потребує уваги» проєкту (overview.collect): затримка іншого проєкту для
наступного кроку й нестача спільного товару — важливе, решта — довідка (колір проєкту не змінює).
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db.models import F, Q

from .models import Project, ProjectLink, ProjectStep, StepDependency

LOAD_MAX = 5                  # кроків на тиждень у людини — далі «перевантаження»
LOAD_WEEKS = 4                # скільки тижнів наперед дивитись
OFFER_DEMAND = ("review", "approved", "sent")     # Angebote без замовлення, що ще можуть ним стати
CLOSED = (ProjectStep.DONE, ProjectStep.SKIPPED)
OPEN_ORDER_EXCLUDE = ("cancelled", "shipped", "delivered")
PO_OPEN = ("ordered", "partial")
PORTFOLIO_URL = "/admin/projects/project/portfolio/#impacts"
KINDS = {"dep": ("⛓", "Залежність від іншого проєкту"), "stock": ("📦", "Спільний товар"),
         "supplier": ("🏭", "Спільний постачальник"), "load": ("👷", "Навантаження")}


@dataclass
class Impact:
    kind: str                 # dep | stock | supplier | load
    level: str                # high | medium | info
    title: str
    text: str
    projects: list = field(default_factory=list)       # [Project] — кого стосується
    url: str = ""
    action: tuple = ()        # (підпис, адреса)
    data: dict = field(default_factory=dict)            # подробиці для attention / карти зв'язків

    @property
    def icon(self):
        return KINDS[self.kind][0]

    @property
    def project_ids(self):
        return {p.pk for p in self.projects}


def _closed(step) -> bool:
    return step.status in CLOSED or step.eff_status in CLOSED


def _end(step):
    return step.f_end or step.end


def _start(step):
    return step.f_start or step.start


def _num(value) -> str:
    d = Decimal(value or 0).normalize()
    s = f"{d:,.0f}" if d == d.to_integral() else f"{d:,.2f}".rstrip("0").rstrip(".")
    return s.replace(",", " ")


def _codes(projects) -> str:
    return ", ".join(p.code for p in projects)


# ── ⛓ залежності між проєктами ───────────────────────────────────────────────

def _forecast_ends(project_ids) -> dict:
    """{project_id: найпізніший прогноз кінця кроку} — з кешу плану."""
    out = {}
    for pid, end in (ProjectStep.objects.filter(project_id__in=project_ids)
                     .values_list("project_id", "f_end")):
        if end and (pid not in out or end > out[pid]):
            out[pid] = end
    return out


def _deps(today, focus) -> list:
    qs = (StepDependency.objects.exclude(predecessor__project_id=F("successor__project_id"))
          .filter(successor__project__status__in=Project.OPEN_STATUSES)
          .select_related("predecessor__project", "successor__project"))
    if focus is not None:
        qs = qs.filter(Q(successor__project=focus) | Q(predecessor__project=focus))
    deps = [d for d in qs if not _closed(d.successor) and not _closed(d.predecessor) and _end(d.predecessor)]
    ends = _forecast_ends({d.successor.project_id for d in deps})
    out = []
    for d in deps:
        p, s = d.predecessor, d.successor
        pp, sp = p.project, s.project
        ready = _end(p) + timedelta(days=1 + d.lag_days)
        planned = s.start or s.baseline_start
        late = (ready - planned).days if planned else 0
        end = ends.get(sp.pk)
        text = f"«{s.title}» ({sp.code}) чекає «{p.title}» ({pp.code}): прогноз {_end(p):%d.%m}"
        if late > 0:
            text += f" — на {late} дн. пізніше за план кроку ({planned:%d.%m})"
        elif planned:
            text += " — встигає"
        if end and sp.due_date and end > sp.due_date:
            text += f"; прогноз {sp.code} — {end:%d.%m} при строку клієнту {sp.due_date:%d.%m}"
        out.append(Impact("dep", "medium" if late > 0 else "info", f"{pp.code} → {sp.code}", text, [pp, sp],
                          url=f"/admin/projects/project/{sp.pk}/change/#pj-plan",
                          data={"pred": p, "succ": s, "late": late, "ready": ready}))
    return out


# ── 📦 спільний товар ────────────────────────────────────────────────────────

def _demand(product_ids=None, focus=None) -> dict:
    """{product_id: {project: [qty, held, [звідки]]}} — відкриті замовлення (кількість мінус відправлене посилками;
    held — що склад уже тримає під позицію) й Angebote без замовлення відкритих проєктів."""
    from offers.models import OfferLine
    from inventory.services import stock
    from sales.models import SalesOrderLine
    out = defaultdict(dict)

    links = (ProjectLink.objects.filter(status=ProjectLink.CONFIRMED, order__isnull=False,
                                        project__status__in=Project.OPEN_STATUSES)
             .exclude(order__status__in=OPEN_ORDER_EXCLUDE).select_related("project", "order"))
    order_project = {}
    for link in links.order_by("project_id"):
        order_project.setdefault(link.order_id, link.project)      # замовлення в кількох проєктах — один раз
    lines = stock.annotate_line_held(
        SalesOrderLine.objects.filter(order_id__in=order_project, order__affects_stock=True, qty__gt=0)
        .select_related("order"))
    if product_ids is not None:
        lines = lines.filter(product_id__in=product_ids)
    for line in lines:
        project = order_project[line.order_id]
        need = max((line.qty or Decimal("0")) - stock.line_sent(line), Decimal("0"))
        if need <= 0:
            continue
        row = out[line.product_id].setdefault(project, [Decimal("0"), Decimal("0"), []])
        row[0] += need
        row[1] += stock.line_cover_held(line)
        row[2].append(f"замовлення {line.order.order_number}")

    offers = (OfferLine.objects.filter(product__isnull=False, offer__project__status__in=Project.OPEN_STATUSES,
                                       offer__status__in=OFFER_DEMAND, offer__order__isnull=True, qty__gt=0)
              .select_related("offer__project"))
    if product_ids is not None:
        offers = offers.filter(product_id__in=product_ids)
    for line in offers:
        row = out[line.product_id].setdefault(line.offer.project, [Decimal("0"), Decimal("0"), []])
        row[0] += line.qty
        row[2].append(f"Angebot {line.offer.display_number}")
    if focus is not None:
        out = {pid: by for pid, by in out.items() if focus in by}
    return out


def _stock(today, focus) -> list:
    from inventory.models import Product
    from inventory.services import stock
    product_ids = None
    if focus is not None:
        product_ids = set(_demand(focus=focus))
        if not product_ids:
            return []
    demand = {pid: by for pid, by in _demand(product_ids).items() if len(by) >= 2}
    if not demand:
        return []
    have = stock.stock_map(demand)
    incoming = stock.incoming_info(demand)
    products = {p.pk: p for p in Product.objects.filter(pk__in=demand)}
    out = []
    for pid, by in demand.items():
        p = have.get(pid)
        if p is None:
            continue
        need = sum(v[0] for v in by.values())
        free = (p._available or Decimal("0")) + sum(v[1] for v in by.values())   # без того, що тримають чужі замовлення
        inc = incoming.get(pid) or {}
        short = need - max(free, Decimal("0")) - (inc.get("qty") or Decimal("0"))
        if short <= 0:
            continue
        projects = sorted(by, key=lambda x: x.code)
        prod = products[pid]
        parts = " + ".join(f"{_num(by[x][0])} ({x.code})" for x in projects)
        text = (f"{prod.sku} {prod.name}: потрібно {parts} = {_num(need)}; на складі для них "
                f"{_num(max(free, Decimal('0')))}")
        if inc.get("qty"):
            text += f", очікується {_num(inc['qty'])} ({', '.join(inc['codes'][:3])}"
            text += f", до {inc['date']:%d.%m})" if inc.get("date") else ")"
        text += f" — бракує {_num(short)}."
        out.append(Impact("stock", "medium", prod.sku, text, projects,
                          url=f"/admin/inventory/product/{pid}/change/",
                          action=("📦 Товар", f"/admin/inventory/product/{pid}/change/"),
                          data={"product": prod, "short": short, "need": {x.pk: by[x][0] for x in projects}}))
    return out


# ── 🏭 спільний постачальник ─────────────────────────────────────────────────

def _suppliers(today, focus) -> list:
    links = (ProjectLink.objects.filter(status=ProjectLink.CONFIRMED, purchase_order__isnull=False,
                                        purchase_order__status__in=PO_OPEN,
                                        purchase_order__supplier__isnull=False,
                                        project__status__in=Project.OPEN_STATUSES)
             .select_related("project", "purchase_order__supplier"))
    if focus is not None:
        suppliers = set(links.filter(project=focus).values_list("purchase_order__supplier_id", flat=True))
        if not suppliers:
            return []
        links = links.filter(purchase_order__supplier_id__in=suppliers)
    by = defaultdict(list)
    for link in links:
        by[link.purchase_order.supplier].append(link)
    out = []
    for supplier, items in by.items():
        projects = sorted({link.project for link in items}, key=lambda x: x.code)
        if len(projects) < 2:
            continue
        items.sort(key=lambda link: (link.purchase_order.expected_date or today + timedelta(days=9999),
                                     link.project.code))
        late = [link for link in items if link.purchase_order.expected_date
                and link.purchase_order.expected_date < today]
        pos = "; ".join(f"{link.purchase_order} ({link.project.code})"
                        + (f" до {link.purchase_order.expected_date:%d.%m}" if link.purchase_order.expected_date else "")
                        for link in items[:6])
        text = f"{supplier.name} — закупівлі для {len(projects)} проєктів одночасно: {pos}."
        if late:
            text += f" Прострочено {late[0].purchase_order} — затримка в постачальника зачепить {_codes(projects)}."
        else:
            text += " Уточніть, чи встигне все разом."
        out.append(Impact("supplier", "medium" if late else "info", supplier.name, text, projects,
                          url=f"/admin/inventory/supplier/{supplier.pk}/change/",
                          data={"supplier": supplier, "late": bool(late)}))
    return out


# ── 👷 навантаження людей ───────────────────────────────────────────────────

def _load(today, focus) -> list:
    monday = today - timedelta(days=today.weekday())
    horizon = monday + timedelta(weeks=LOAD_WEEKS)
    qs = (ProjectStep.objects.filter(project__status__in=Project.OPEN_STATUSES, assignee__isnull=False,
                                     kind=ProjectStep.TASK)
          .exclude(status__in=CLOSED).exclude(eff_status__in=CLOSED)
          .select_related("project", "assignee"))
    if focus is not None:
        people = set(qs.filter(project=focus).values_list("assignee_id", flat=True))
        if not people:
            return []
        qs = qs.filter(assignee_id__in=people)
    weeks = defaultdict(list)                     # (людина, понеділок) → кроки
    for s in qs:
        a, b = _start(s), _end(s)
        if not a or not b or b < monday or a >= horizon:
            continue
        w = max(a - timedelta(days=a.weekday()), monday)
        while w <= b and w < horizon:
            weeks[(s.assignee, w)].append(s)
            w += timedelta(days=7)
    worst = {}
    for (person, w), steps in weeks.items():
        projects = {s.project for s in steps}
        if len(steps) > LOAD_MAX and len(projects) >= 2 and len(steps) > len(worst.get(person, (None, []))[1]):
            worst[person] = (w, steps)
    out = []
    for person, (w, steps) in worst.items():
        projects = sorted({s.project for s in steps}, key=lambda x: x.code)
        name = person.get_full_name() or person.get_username()
        out.append(Impact("load", "info", name,
                          f"{name}: {len(steps)} кроків на тижні {w:%d.%m}–{w + timedelta(days=6):%d.%m} у "
                          f"{len(projects)} проєктах ({_codes(projects)}) — більше {LOAD_MAX}.",
                          projects, data={"person": person, "week": w, "steps": len(steps)}))
    return out


# ── усе разом ────────────────────────────────────────────────────────────────

ORDER = {"high": 0, "medium": 1, "info": 2}


def compute(today, focus=None) -> list:
    """Впливи між проєктами: важливі першими. focus — проєкт: лише ті, що його стосуються."""
    out = []
    for part in (_deps, _stock, _suppliers, _load):
        out.extend(part(today, focus))
    out.sort(key=lambda i: (ORDER.get(i.level, 9), list(KINDS).index(i.kind), i.title))
    return out


def attention(project, add, today):
    """Впливи проєкту → «потребує уваги» (add(level, icon, text, url, kind))."""
    if not project.is_open:
        return
    for i in compute(today, focus=project):
        if i.kind == "dep":
            d = i.data
            if d["succ"].project_id == project.pk:
                if d["late"] > 0:
                    add("medium", "⛓", f"Крок «{d['succ'].title}» чекає «{d['pred'].title}» з "
                                       f"{d['pred'].project.code}: прогноз {_end(d['pred']):%d.%m} — на {d['late']} дн. "
                                       f"пізніше за план кроку", i.url, "risk")
            elif d["late"] > 0:
                add("info", "⛓", f"Від кроку «{d['pred'].title}» залежить {d['succ'].project.code} "
                                 f"(«{d['succ'].title}») — прогноз пізніше на {d['late']} дн.",
                    f"/admin/projects/project/{d['succ'].project_id}/change/#pj-plan", "risk")
        elif i.kind == "stock":
            others = [p.code for p in i.projects if p.pk != project.pk]
            add("medium", "📦", f"Спільний товар {i.title}: бракує {_num(i.data['short'])} для цього проєкту й "
                               f"{', '.join(others)}", PORTFOLIO_URL, "risk")
        elif i.kind == "supplier":
            add("info", "🏭", i.text, PORTFOLIO_URL, "risk")     # прострочену закупівлю вже видно окремо (overview)
        elif i.kind == "load":
            add("info", "👷", i.text, PORTFOLIO_URL, "risk")
