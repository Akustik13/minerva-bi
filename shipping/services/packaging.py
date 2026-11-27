"""Рекомендована упаковка замовлення й навчання з відправлень — одна логіка для
картки замовлення («📦 Рекомендована упаковка», `sales.admin.packaging_panel`), форми
нового відправлення (`ShipmentAdmin._fill_packaging_from_order`), «💾 Зберегти упаковку»
в картці відправлення і «🔍 Аналізувати відправлення» в налаштуваннях доставки.

Рекомендація — `recommend(order)`:
1. 📜 Так само вже відправляли: останнє відправлення (не чернетка, помилка чи
   скасоване) замовлення з тими самими товарами й кількостями, єдине в своєму
   замовленні, — його коробки, розміри й вага.
2. 📦 Упаковки товарів (`ProductPackaging`). «Товарів в коробку» — скільки одиниць
   вміщує коробка (кілька записів з тією самою коробкою — найбільше число). Позиції, яким
   випала та сама коробка, ділять її: частка = к-сть ÷ місткість, коробок — частки разом,
   округлені вгору. Різні коробки — усе в найбільшу, якщо вона не менша за кожну з інших
   у всіх вимірах і так виходить менше коробок. «Макс. вага вмісту» теж додає коробок.
   Коробка товару — «Рекомендована», якщо вміщує всю кількість, інакше та, з якою
   найменше коробок.
   Вага: «Вага нетто» × к-сть + «Вага порожньої» × коробок; без ваги нетто —
   («Орієнт. вага посилки» − вага коробки) ÷ «Товарів в коробку» на одиницю (≈).

Навчання — `learn_plan(shipment)` / `learn(shipment)`: з відправлення запам'ятовується,
скільки одиниць вмістила коробка (усі товари коробки разом — так дві позиції, що їхали
в одній коробці, наступного разу знову підуть в одну), лише якщо Minerva ще не знає
такої або більшої місткості цієї коробки для товару.

Коробка з насічками («Можна скласти до висоти», `PackagingMaterial.heights`) — одна коробка в усіх висотах:
`material_for` впізнає 30×30×16 як складену 30×30×30 (запас, навчання без нової коробки-дубля); місткість
«Товарів в коробку» — завжди на повну висоту (з посилки, складеної до 16 см, перераховується пропорційно);
рекомендація (`fill_height`) складає коробку до найменшої висоти, якої вистачає на вміст.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace

from django.db.models import Count

MIN_WEIGHT_KG = Decimal("0.1")
HISTORY_SCAN = 300                    # скільки останніх відправлень з цими товарами переглядати
SKIP_STATUSES = ("draft", "error", "cancelled")   # не взірець і не навчання
DIM_TOLERANCE = Decimal("1.0")        # ±см — та сама коробка


def _dims(obj):
    return sorted(Decimal(getattr(obj, k) or 0) for k in ("length_cm", "width_cm", "height_cm"))


def _volume(obj):
    a, b, c = _dims(obj)
    return a * b * c


def _fits(inner, outer):
    """Коробка inner влазить в outer (виміри в будь-якій орієнтації)."""
    return all(a <= b for a, b in zip(_dims(inner), _dims(outer)))


def _same_size(a, b):
    return all(abs(x - y) <= DIM_TOLERANCE for x, y in zip(_dims(a), _dims(b)))


def _variant(m, h):
    return SimpleNamespace(length_cm=m.length_cm, width_cm=m.width_cm, height_cm=h)


def _heights(m):
    return m.heights if hasattr(m, "heights") else [Decimal(m.height_cm or 0)]


def fold_height(m, obj):
    """До якої висоти складено коробку m, щоб вийшли розміри obj (±1 см, будь-яка орієнтація): повна висота,
    одна з «Можна скласти до висоти» або None — це не вона."""
    for h in _heights(m):
        if _same_size(_variant(m, h), obj):
            return h
    return None


def _distance(m, obj):
    h = fold_height(m, obj)
    return sum(abs(x - y) for x, y in zip(_dims(_variant(m, h)), _dims(obj)))


def is_folded(m, h) -> bool:
    return m is not None and h is not None and Decimal(h) < Decimal(m.height_cm or 0) - DIM_TOLERANCE


def _num(v):
    v = Decimal(v or 0).normalize()
    return f"{v:f}"


def materials():
    from shipping.models import PackagingMaterial
    return list(PackagingMaterial.objects.filter(is_active=True).order_by("pk"))


def merged_target(m, by_pk=None):
    """Коробка, у яку злили m («📊 Використання й схожі» → `merged_into`), — ланцюжком до активної; активна (і
    повернута з архіву) — сама m. by_pk {pk: коробка} — без запитів до бази."""
    seen = set()
    while m is not None and not m.is_active and m.merged_into_id and m.merged_into_id not in seen:
        seen.add(m.pk)
        nxt = by_pk.get(m.merged_into_id) if by_pk is not None else None
        m = nxt if nxt is not None else m.merged_into
    return m


def material_for(obj, pool=None):
    """Активний пакувальний матеріал тих самих розмірів (±1 см, будь-яка орієнтація) — і складений до однієї з
    «Можна скласти до висоти». Кілька — найближчий за розмірами, при рівних — не складений."""
    pool = materials() if pool is None else pool
    exact = [m for m in pool if fold_height(m, obj) is not None]
    if not exact:
        return None
    return min(exact, key=lambda m: (_distance(m, obj), is_folded(m, fold_height(m, obj))))


def fold_boxes(pool=None) -> list:
    """Коробки з насічками для форми відправлення: [{name, l, w, heights}] — кнопки «↧ 30 · 22 · 16» біля висоти."""
    pool = materials() if pool is None else pool
    return [{"name": str(m), "l": float(m.length_cm), "w": float(m.width_cm),
             "heights": [float(h) for h in m.heights]} for m in pool if len(m.heights) > 1]


def _num(d):
    """Decimal → коротке число для підпису: 30.0 → «30», 15.5 → «15.5»."""
    return format(Decimal(d or 0).normalize(), "f")


def _plain_dims(text):
    return re.sub(r"(\d)\.0+(?!\d)", r"\1", str(text)).replace(" ", "").replace("x", "×").lower()


def catalog_boxes(pool=None) -> list:
    """Активні пакувальні матеріали для вибору «📦 Коробка» в рядках «Коробки» форми відправлення:
    [{pk, label, type, l, w, h, heights, tare}] — за типом (коробки, конверти…) і розмірами. Вибір підставляє
    розміри; введені вручну розміри форма сама впізнає (як `material_for`: ±1 см, будь-яка орієнтація, складена)."""
    from shipping.models import PackagingMaterial as PM
    pool = materials() if pool is None else pool
    order = {v: i for i, v in enumerate(PM.BoxType.values)}
    types = dict(PM.BoxType.choices)
    out = []
    for m in sorted(pool, key=lambda m: (order.get(m.box_type, 99), _volume(m), m.pk)):
        dims = f"{_num(m.length_cm)}×{_num(m.width_cm)}×{_num(m.height_cm)} см"
        name = (m.name or "").strip()
        parts = [m.code] if m.code else []
        own = name and _plain_dims(name) != _plain_dims(dims)     # назва = розміри («30.0×22.0×15.0 см») — без повтору
        if own:
            parts.append(name)
        if not own or _plain_dims(dims[:-3]) not in _plain_dims(name):   # розміри вже в назві («M 30×22×15») — не вдруге
            parts.append(dims)
        out.append({"pk": m.pk, "label": " · ".join(parts), "type": str(types.get(m.box_type, m.box_type)),
                    "l": float(m.length_cm), "w": float(m.width_cm), "h": float(m.height_cm),
                    "heights": [float(h) for h in m.heights], "tare": float(m.tare_weight_kg or 0)})
    return out


def fill_height(m, fill):
    """Найменша висота, до якої можна скласти m, коли вміст займає частку fill (0…1) повної висоти."""
    hs = _heights(m)
    fill = Fraction(fill)
    need = Decimal(hs[0]) * fill.numerator / fill.denominator
    fits = [h for h in hs if h >= need]
    return min(fits) if fits else hs[0]


@dataclass
class Box:
    length_cm: Decimal
    width_cm: Decimal
    height_cm: Decimal
    qty: int = 1                        # однакових коробок
    weight_g: Decimal | None = None     # одна коробка з вмістом
    packaging: object = None            # PackagingMaterial
    lines: int = 0                      # позицій замовлення в цих коробках

    @property
    def folded_to(self):
        """Висота, до якої складено коробку (None — повна)."""
        h = fold_height(self.packaging, self) if self.packaging else None
        return h if is_folded(self.packaging, h) else None

    @property
    def label(self):
        if self.packaging:
            h = self.folded_to
            return f"{self.packaging} · складено до {_num(h)} см" if h else str(self.packaging)
        return f"{_num(self.length_cm)}×{_num(self.width_cm)}×{_num(self.height_cm)} см"

    def row(self):
        """Рядок коробки для форми відправлення (`pkg_rows`)."""
        kg = (self.weight_g / 1000).quantize(Decimal("0.001")) if self.weight_g else Decimal(0)
        return {
            "weight_kg": float(max(MIN_WEIGHT_KG, kg)),
            "length_cm": float(self.length_cm),
            "width_cm":  float(self.width_cm),
            "height_cm": float(self.height_cm),
            "quantity":  self.qty,
        }


@dataclass
class Line:
    line: object                        # SalesOrderLine
    rec: object = None                  # ProductPackaging
    auto: bool = False                  # не «Рекомендована» — підібрано за кількістю
    capacity: int = 0                   # одиниць товару в коробку rec.packaging
    weight_g: Decimal | None = None     # вага вмісту позиції
    approx: bool = False                # вага з «Орієнт. ваги посилки», а не нетто


@dataclass
class Recommendation:
    source: str                         # history | product_packaging | weight
    lines: list
    boxes: list = field(default_factory=list)
    weight_g: Decimal = Decimal(0)      # уся посилка; 0 — невідомо
    missing_weight: bool = False
    no_packaging: bool = False
    approx_weight: bool = False
    shipment: object = None             # 📜 відправлення-взірець

    @property
    def total_boxes(self):
        return sum(b.qty for b in self.boxes)

    @property
    def box(self):
        return self.boxes[0].packaging if self.boxes else None

    def rows(self):
        return [b.row() for b in self.boxes]


def recommend(order):
    """Рекомендація для замовлення або None, якщо позицій немає."""
    lines = list(order.lines.select_related("product").order_by("pk"))
    if not lines:
        return None
    return _from_history(order, lines) or _from_products(lines)


def _contents(lines):
    """{товар: к-сть} позицій замовлення; None — є позиція без товару."""
    out = defaultdict(Decimal)
    for l in lines:
        if not l.qty or l.qty <= 0:
            continue
        if not l.product_id:
            return None
        out[l.product_id] += l.qty
    return dict(out)


def _single_shipment_orders(order_ids):
    from shipping.models import Shipment
    counts = (Shipment.objects.filter(order_id__in=order_ids)
              .exclude(status__in=("cancelled", "error"))
              .order_by().values_list("order_id").annotate(n=Count("pk")))
    return {oid for oid, n in counts if n == 1}


def _from_history(order, lines):
    from sales.models import SalesOrderLine
    from shipping.models import Shipment

    want = _contents(lines)
    if not want:
        return None
    found = list(
        Shipment.objects.filter(order__lines__product_id__in=list(want))
        .exclude(order_id=order.pk).exclude(status__in=SKIP_STATUSES)
        .order_by("-created_at", "-pk").values_list("pk", "order_id", "created_at")
        .distinct()[:HISTORY_SCAN]
    )
    order_ids = {oid for _, oid, _ in found}
    if not order_ids:
        return None
    contents = defaultdict(lambda: defaultdict(Decimal))
    unknown = set()
    for oid, pid, qty in SalesOrderLine.objects.filter(order_id__in=order_ids) \
            .values_list("order_id", "product_id", "qty"):
        if not qty or qty <= 0:
            continue
        if pid is None:
            unknown.add(oid)
        else:
            contents[oid][pid] += qty
    alone = _single_shipment_orders(order_ids)
    pool = None
    for pk, oid, _ in found:
        if oid in unknown or oid not in alone or dict(contents.get(oid, {})) != want:
            continue
        shipment = Shipment.objects.select_related("order").prefetch_related("packages").get(pk=pk)
        pool = materials() if pool is None else pool
        boxes = shipment_boxes(shipment, pool)
        if not boxes:
            continue
        infos = [Line(l, weight_g=_net(l)) for l in lines]
        return Recommendation(
            source="history", lines=infos, boxes=boxes, shipment=shipment,
            weight_g=sum((b.weight_g or 0) * b.qty for b in boxes),
        )
    return None


def _net(line):
    nw = getattr(line.product, "net_weight_g", None) if line.product_id else None
    return Decimal(nw) * line.qty if nw else None


def shipment_boxes(shipment, pool=None):
    """Коробки відправлення: рядки «Коробки» або розміри самого відправлення."""
    pool = materials() if pool is None else pool
    out = []
    for p in shipment.packages.all():
        out.append(Box(p.length_cm, p.width_cm, p.height_cm, qty=max(1, p.quantity or 1),
                       weight_g=Decimal(p.weight_kg or 0) * 1000 or None,
                       packaging=material_for(p, pool)))
    if not out and shipment.length_cm and shipment.width_cm and shipment.height_cm:
        out.append(Box(shipment.length_cm, shipment.width_cm, shipment.height_cm,
                       weight_g=Decimal(shipment.weight_kg or 0) * 1000 or None,
                       packaging=material_for(shipment, pool)))
    return out


WEIGHT_DIFF_KG = Decimal("0.5")     # «⚖️ Розбіжність у вазі» — від такої різниці …
WEIGHT_DIFF_PCT = 20                # … або від такої частки ваги посилки


def box_tare(shipment, pool=None) -> dict:
    """Вага порожніх коробок відправлення (`shipment_boxes` → «Вага порожньої» коробки): {kg, boxes [{name, qty,
    kg}], unknown [{name, pk}]}. Коробку без «Вага порожньої» не враховано — вона в unknown; не впізнану за
    розмірами — пропускаємо."""
    kg, boxes, unknown = Decimal(0), [], []
    for b in shipment_boxes(shipment, pool):
        m = b.packaging
        if m is None:
            continue
        tare = Decimal(m.tare_weight_kg or 0)
        if tare > 0:
            kg += tare * b.qty
            boxes.append({"name": str(m), "qty": b.qty, "kg": tare})
        elif all(u["pk"] != m.pk for u in unknown):
            unknown.append({"name": str(m), "pk": m.pk})
    return {"kg": kg, "boxes": boxes, "unknown": unknown}


def weight_check(shipment, content_kg, pool=None) -> dict | None:
    """«⚖️ Розбіжність у вазі» на сторінках перевірки UPS / Jumingo: вага посилки проти митних позицій + вага
    порожніх коробок (якщо відома, інакше без неї). None — збігається або порівнювати нічим."""
    ship = Decimal(shipment.weight_kg or 0)
    content = Decimal(str(content_kg or 0))
    if ship <= 0 or content <= 0:
        return None
    tare = box_tare(shipment, pool)
    expected = content + tare["kg"]
    diff = abs(ship - expected)
    pct = diff / ship * 100
    if diff < WEIGHT_DIFF_KG and pct < WEIGHT_DIFF_PCT:
        return None

    def r3(d):
        return float(Decimal(d).quantize(Decimal("0.001")))

    return {"shipment_kg": r3(ship), "customs_kg": r3(content), "tare_kg": r3(tare["kg"]),
            "boxes": [dict(b, kg=r3(b["kg"])) for b in tare["boxes"]], "unknown": tare["unknown"],
            "expected_kg": r3(expected), "diff_kg": r3(diff), "diff_pct": float(round(pct, 1))}


def _capacity(recs, rec):
    """Скільки одиниць товару вміщує коробка rec — найбільше з його записів з нею."""
    return max([r.qty_per_box for r in recs if r.packaging_id == rec.packaging_id] + [1])


def _pick(recs, qty):
    """(запис, підібрано автоматично) для к-сті товару."""
    if not recs:
        return None, False
    default = next((r for r in recs if r.is_default), None)
    if default and qty <= _capacity(recs, default):
        return default, False
    best = min(recs, key=lambda r: (math.ceil(Fraction(qty) / _capacity(recs, r)),
                                    _volume(r.packaging), -r.qty_per_box))
    if default and math.ceil(Fraction(qty) / _capacity(recs, default)) <= \
            math.ceil(Fraction(qty) / _capacity(recs, best)):
        return default, False
    return best, True


def _content_weight(product, qty, recs, rec):
    """(вага вмісту позиції, приблизна) — без коробки."""
    nw = getattr(product, "net_weight_g", None)
    if nw:
        return Decimal(nw) * qty, False
    for r in ([rec] if rec else []) + [x for x in recs if x is not rec]:
        if r.estimated_weight_g:
            tare = Decimal(r.packaging.tare_weight_kg or 0) * 1000
            unit = (Decimal(r.estimated_weight_g) - tare) / max(1, r.qty_per_box)
            if unit > 0:
                return unit * qty, True
    return None, False


def _boxes_needed(material, share, content_g):
    n = max(1, math.ceil(share))
    limit = getattr(material, "max_weight_kg", None)
    if limit and limit > 0 and content_g > 0:
        n = max(n, math.ceil(content_g / (Decimal(limit) * 1000)))
    return n


def _from_products(lines):
    from shipping.models import ProductPackaging

    recs = defaultdict(list)
    for r in (ProductPackaging.objects
              .filter(product_id__in={l.product_id for l in lines if l.product_id})
              .select_related("packaging").order_by("pk")):
        recs[r.product_id].append(r)
    totals = defaultdict(Decimal)        # той самий товар у кількох позиціях — разом
    for l in lines:
        if l.product_id and l.qty and l.qty > 0:
            totals[l.product_id] += l.qty

    groups = {}                          # коробка → частка, вага вмісту, позицій
    infos, loose = [], Decimal(0)
    missing = no_pack = approx = False
    for l in lines:
        info = Line(l)
        infos.append(info)
        if not l.product_id or not l.qty or l.qty <= 0:
            continue
        mine = recs.get(l.product_id, [])
        rec, auto = _pick(mine, totals[l.product_id])
        w, ap = _content_weight(l.product, l.qty, mine, rec)
        info.weight_g, info.approx = w, ap
        missing |= w is None
        approx |= ap
        if not rec:
            no_pack = True
            loose += w or 0
            continue
        cap = _capacity(mine, rec)
        info.rec, info.auto, info.capacity = rec, auto, cap
        g = groups.setdefault(rec.packaging_id, {
            "material": rec.packaging, "share": Fraction(0), "content": Decimal(0), "lines": 0})
        g["share"] += Fraction(l.qty) / cap
        g["content"] += w or 0
        g["lines"] += 1

    if not groups:
        return Recommendation(source="weight", lines=infos, weight_g=loose,
                              missing_weight=missing, no_packaging=no_pack, approx_weight=approx)

    plans = [(g, _boxes_needed(g["material"], g["share"], g["content"])) for g in groups.values()]
    if len(plans) > 1:
        big = max((g["material"] for g, _ in plans), key=_volume)
        if _volume(big) > 0 and all(_fits(g["material"], big) for g, _ in plans):
            share = sum(g["share"] * Fraction(_volume(g["material"])) / Fraction(_volume(big))
                        for g, _ in plans)
            content = sum(g["content"] for g, _ in plans)
            n = _boxes_needed(big, share, content)
            if n < sum(k for _, k in plans):
                plans = [({"material": big, "share": share, "content": content,
                           "lines": sum(g["lines"] for g, _ in plans)}, n)]

    plans.sort(key=lambda p: _volume(p[0]["material"]), reverse=True)
    plans[0][0]["content"] += loose      # товари без упаковки — у найбільшу коробку
    boxes = []
    for g, n in plans:
        m = g["material"]
        tare = Decimal(m.tare_weight_kg or 0) * 1000
        per_box = g["content"] / n + tare
        h = fill_height(m, min(Fraction(1), g["share"] / n))      # коробка з насічками — до висоти вмісту
        boxes.append(Box(m.length_cm, m.width_cm, h, qty=n, packaging=m,
                         weight_g=per_box if per_box > 0 else None, lines=g["lines"]))
    return Recommendation(
        source="product_packaging", lines=infos, boxes=boxes,
        weight_g=sum((b.weight_g or 0) * b.qty for b in boxes),
        missing_weight=missing, no_packaging=no_pack, approx_weight=approx,
    )


# ── Навчання з відправлень ────────────────────────────────────────────────────

def _box_contents(shipment, lines):
    """[(коробка, вага однієї кг, {товар: одиниць}, однакових коробок)] або [] — невідомо,
    що в якій коробці."""
    qty = defaultdict(Decimal)
    for l in lines:
        qty[l.product_id] += l.qty
    pkgs = list(shipment.packages.all())
    if len(pkgs) <= 1:
        src = pkgs[0] if pkgs else shipment
        if not (src.length_cm and src.width_cm and src.height_cm):
            return []
        n = max(1, pkgs[0].quantity or 1) if pkgs else 1
        return [(src, src.weight_kg, dict(qty), n)]
    # Кілька різних коробок — лише з «Розподілом товарів в коробці»
    by_sku = {}
    for l in lines:
        for s in (l.product.sku, l.sku_raw):
            if s:
                by_sku[s.strip().upper()] = l.product_id
    articles = (shipment.customs_articles or {}).get("customs_line_items") or []
    out = []
    for p in pkgs:
        units = defaultdict(Decimal)
        for it in (p.items_distribution or {}).get("items") or []:
            idx = it.get("index")
            sku = articles[idx].get("sku", "") if isinstance(idx, int) and 0 <= idx < len(articles) else ""
            pid = by_sku.get(str(sku).strip().upper())
            if not pid:
                return []
            units[pid] += Decimal(str(it.get("quantity") or 0))
        if not units:
            return []
        out.append((p, p.weight_kg, dict(units), max(1, p.quantity or 1)))
    return out


def learn_plan(shipment, pool=None):
    """Що запам'ятати з відправлення: [{product, sku, name, box, qty_per_box, weight_g,
    approx, packaging}] — товари, для яких коробка показала нову (більшу) місткість."""
    from shipping.models import ProductPackaging

    order = shipment.order
    if not order or shipment.status in SKIP_STATUSES:
        return []
    if order.pk not in _single_shipment_orders([order.pk]):
        return []                        # кілька відправлень — невідомо, що в якому
    lines = [l for l in order.lines.select_related("product").order_by("pk")
             if l.product_id and l.qty and l.qty > 0]
    if not lines:
        return []
    products = {l.product_id: l.product for l in lines}
    pool = materials() if pool is None else pool
    known = defaultdict(list)
    for r in ProductPackaging.objects.filter(product_id__in=products).select_related("packaging"):
        known[r.product_id].append(r)

    def same_box(known_material, known_dims, box_material, box):
        """Той самий пакувальний матеріал (і складений), інакше — ті самі розміри."""
        if known_material is not None and box_material is not None:
            return known_material.pk == box_material.pk
        return _same_size(known_dims, box)

    plan = []
    for box, weight_kg, units, n in _box_contents(shipment, lines):
        cap = math.ceil(sum(units.values()) / n)
        if cap < 1:
            continue
        material = material_for(box, pool)
        dims = f"{_num(box.length_cm)}×{_num(box.width_cm)}×{_num(box.height_cm)} см"
        folded = fold_height(material, box) if material is not None else None
        if is_folded(material, folded):
            # Коробку склали нижче — місткість на повну висоту (рекомендація потім складе її знову)
            full = Decimal(material.height_cm)
            sent = cap
            cap = max(cap, int(Decimal(cap) * full / folded))
            dims = (f"{_num(material.length_cm)}×{_num(material.width_cm)}×{_num(full)} см "
                    f"(складено до {_num(folded)} см: {sent} шт → на повну висоту ≈ {cap} шт)")
        for pid in units:
            if any(same_box(r.packaging, r.packaging, material, box) and r.qty_per_box >= cap for r in known[pid]):
                continue
            if any(p["product"].pk == pid and p["qty_per_box"] >= cap
                   and same_box(p["packaging"], p["box"], material, box) for p in plan):
                continue
            p = products[pid]
            plan.append({
                "product": p, "sku": p.sku, "name": str(p),
                "box": box, "packaging": material, "dims": dims,
                "qty_per_box": min(cap, 32767),
                "weight_g": int(Decimal(weight_kg or 0) * 1000) or None,
                "approx": len(units) > 1,
                "folded": folded if is_folded(material, folded) else None,
            })
    return plan


def learn(shipment, plan=None, pool=None):
    """Записує план навчання: коробку (знайдену або нову) і `ProductPackaging`."""
    from shipping.models import PackagingMaterial, ProductPackaging

    pool = materials() if pool is None else pool
    plan = learn_plan(shipment, pool) if plan is None else plan
    done = []
    for c in plan:
        material = c.get("packaging") or material_for(c["box"], pool)
        if not material:
            b = c["box"]
            material = PackagingMaterial.objects.create(
                box_type=PackagingMaterial.BoxType.BOX,
                length_cm=b.length_cm, width_cm=b.width_cm, height_cm=b.height_cm,
                tare_weight_kg=Decimal("0"),
                notes=f"Авто зі відправлення #{shipment.pk}",
            )
            pool.append(material)
        note = f"Авто зі відправлення #{shipment.pk}"
        if c.get("folded"):
            note += f" (коробку складено до {_num(c['folded'])} см — місткість перераховано на повну висоту)"
        if c["approx"]:
            note += " (в коробці кілька товарів — вага на всю коробку)"
        ProductPackaging.objects.create(
            product=c["product"], packaging=material, qty_per_box=c["qty_per_box"],
            estimated_weight_g=c["weight_g"],
            is_default=not ProductPackaging.objects.filter(product=c["product"]).exists(),
            notes=note,
        )
        done.append({"sku": c["sku"], "qty": c["qty_per_box"], "box": str(material),
                     "weight_g": c["weight_g"]})
    return done
