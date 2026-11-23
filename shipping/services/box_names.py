"""Класифікація коробок: клас розміру, назва й код — для списку «Пакувальні матеріали» і наклейки DYMO.

- Клас (`size_class`): вибраний людиною, інакше за об'ємом — `auto_class` (`CLASS_LIMITS`: XS до 1 л, S до 5 л,
  M до 15 л, L до 35 л, XL більше). Коробка з насічками — за повною висотою.
- Назва: «M 31×22×15» — клас і внутрішні розміри; складана — ще «↧ 27/22/16».
- Код: `<тип>-<клас>-<NN>` (`TYPE_PREFIX`: BX коробка, EN конверт, TB тубус, BG пакет, PK інше), напр. BX-M-02;
  номер — наступний вільний у цьому типі й класі (від меншої коробки до більшої). Наявний код не змінюється —
  він уже може бути на наклейці.
- `plan(materials)` — пропозиція для сторінки «🏷 Класифікація, назви й коди…», `apply(rows)` — записати вибране.
"""
from __future__ import annotations

import re
from decimal import Decimal

from django.db import IntegrityError

CLASS_LIMITS = (("XS", Decimal("1")), ("S", Decimal("5")), ("M", Decimal("15")), ("L", Decimal("35")))
CLASSES = ("XS", "S", "M", "L", "XL")
TYPE_PREFIX = {"box": "BX", "envelope": "EN", "tube": "TB", "bag": "BG", "custom": "PK"}
CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,19}$")


def litres(m) -> Decimal:
    return (Decimal(m.length_cm or 0) * Decimal(m.width_cm or 0) * Decimal(m.height_cm or 0) / 1000).quantize(
        Decimal("0.1"))


def auto_class(m) -> str:
    v = litres(m)
    for cls, limit in CLASS_LIMITS:
        if v < limit:
            return cls
    return "XL"


def class_of(m) -> str:
    return m.size_class if m.size_class in CLASSES else auto_class(m)


def _n(v) -> str:
    return f"{Decimal(v).normalize():f}"


def proposed_name(m, cls=None) -> str:
    cls = cls or class_of(m)
    name = f"{cls} {_n(m.length_cm)}×{_n(m.width_cm)}×{_n(m.height_cm)}"
    folds = m.heights[1:] if hasattr(m, "heights") else []
    if folds:
        name += " ↧" + "/".join(_n(h) for h in folds)
    return name


def prefix(m, cls=None) -> str:
    return f"{TYPE_PREFIX.get(m.box_type, 'PK')}-{cls or class_of(m)}-"


def clean_code(code) -> str:
    return re.sub(r"\s+", "", str(code or "")).upper()


def barcode_value(m) -> str:
    """Що в штрихкоді наклейки: код коробки, без нього — PKG-<номер запису>."""
    return m.code or f"PKG-{m.pk:04d}"


def _taken(exclude=()) -> set:
    from shipping.models import PackagingMaterial
    return set(PackagingMaterial.objects.exclude(code="").exclude(pk__in=list(exclude)).values_list("code", flat=True))


def plan(materials) -> list:
    """[{material, auto_class, cls, name, code, new_code}] — пропозиція для вибраних коробок (нічого не записує).
    Код: наявний лишається; без коду — наступний вільний номер у типі й класі (від меншої коробки до більшої)."""
    materials = sorted(materials, key=lambda m: (litres(m), m.pk))
    taken = _taken()
    rows = []
    for m in materials:
        cls = class_of(m)
        code = m.code
        if not code:
            n = 1
            while f"{prefix(m, cls)}{n:02d}" in taken:
                n += 1
            code = f"{prefix(m, cls)}{n:02d}"
            taken.add(code)
        rows.append({"material": m, "auto_class": auto_class(m), "cls": cls, "name": proposed_name(m, cls),
                     "code": code, "new_code": not m.code})
    return rows


class NamingError(ValueError):
    pass


AUTO_CODE_RE = re.compile(r"^([A-Z]+-(?:XS|S|M|L|XL)-)\d+$")


def apply(changes) -> list:
    """changes: [{material, cls?, name?, code?, new_code?}] — лише те, що людина вибрала. Код — латиниця, цифри й
    «-», без повторів (і з іншими коробками); запропонований новий код (`new_code`), який уже зайнятий (змінили
    клас на сторінці), — наступний вільний номер того самого типу й класу. → [(коробка, змінені поля)]."""
    taken = _taken(exclude=[c["material"].pk for c in changes if "code" in c])
    for c in changes:
        if c.get("new_code") and "code" in c:
            code, m = clean_code(c["code"]), AUTO_CODE_RE.match(clean_code(c["code"]))
            if m and code in taken | {clean_code(x["code"]) for x in changes if x is not c and "code" in x}:
                n = 1
                while f"{m.group(1)}{n:02d}" in taken or any(
                        clean_code(x.get("code")) == f"{m.group(1)}{n:02d}" for x in changes if x is not c):
                    n += 1
                c["code"] = f"{m.group(1)}{n:02d}"
    codes = {}
    for c in changes:
        if "code" in c:
            code = clean_code(c["code"])
            if code and not CODE_RE.match(code):
                raise NamingError(f"Код «{c['code']}»: лише латинські літери, цифри й «-», до 20 знаків.")
            if code and code in codes:
                raise NamingError(f"Код {code} вибрано для двох коробок.")
            codes[code] = c["material"].pk
    clash = [code for code in codes if code and code in taken]
    if clash:
        raise NamingError(f"Код {', '.join(sorted(clash))} уже має інша коробка.")
    done = []
    try:
        _write(changes, done)
    except IntegrityError:          # коробки обмінялись кодами — порядок запису не важливий для людини
        raise NamingError("Коди перетинаються з іншими коробками — змініть їх по черзі.")
    return done


def _write(changes, done):
    from django.db import transaction
    with transaction.atomic():
        for c in changes:
            m, fields = c["material"], []
            if "cls" in c and c["cls"] != m.size_class:
                m.size_class = c["cls"] if c["cls"] in CLASSES else ""
                fields.append("size_class")
            if "name" in c and c["name"].strip() and c["name"].strip() != m.name:
                m.name = c["name"].strip()[:255]
                fields.append("name")
            if "code" in c and clean_code(c["code"]) != m.code:
                m.code = clean_code(c["code"])
                fields.append("code")
            if fields:
                m.save(update_fields=fields)
                done.append((m, fields))
