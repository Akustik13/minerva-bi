"""Пошук за технічними параметрами, як на DigiKey: обираєте категорію — над списком рядок кнопок її параметрів
(тип штекера, довжина кабелю, частота…); кнопка відкриває список значень з кількістю товарів, пошуком і галочками.
Кілька значень одного параметра — «або», різних параметрів — «і».

Параметри — Product.tech_attributes без службових кодів DigiKey (api.serializers.split_attributes). Значення через
кому («802.15.4, Bluetooth, WiFi») — окремі варіанти: «Bluetooth» знаходить усі товари, де він є; кома в дужках
чи десяткова («1,5») не ділить.
Списки: Товари й Асортимент і ціни — на сервері (?attr=Назва:значення, ParamFilter + panel_context), Склад → огляд
залишків — на сторінці (JS, дані з params_map).
"""
from __future__ import annotations

import re

from django.contrib import admin

MAX_DISTINCT = 150     # більше варіантів — не фільтр
MOSTLY_UNIQUE = 0.7    # і понад 25 варіантів, а варіантів більше за 70 % товарів (VSWR, Return Loss…) — теж ні


def _split(value: str) -> list[str]:
    """Значення через «, » → окремі варіанти; кома в дужках не ділить («U.FL (UMCC), I-PEX» → 2, «(a, b)» → 1)."""
    out, depth, cur = [], 0, []
    for i, ch in enumerate(value):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(depth - 1, 0)
        if ch == "," and depth == 0 and (i + 1 == len(value) or value[i + 1] == " "):
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    seen, result = set(), []
    for t in (re.sub(r"\s+", " ", x).strip() for x in out):
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


def params_of(attrs) -> dict[str, list[str]]:
    """Технічні параметри товару {назва: [варіанти]}."""
    from api.serializers import split_attributes
    out = {}
    for name, value in split_attributes(attrs)[0].items():
        parts = []
        for v in (value if isinstance(value, (list, tuple)) else [value]):
            parts += _split(str(v))
        if parts:
            out[name] = list(dict.fromkeys(parts))
    return out


def parse(values) -> dict[str, set[str]]:
    """['Termination:SMA', 'Termination:U.FL', 'Gain:2dBi'] → {назва: {значення}}."""
    out = {}
    for item in values or []:
        name, sep, value = str(item).partition(":")
        if sep and name.strip() and value.strip():
            out.setdefault(name.strip(), set()).add(value.strip())
    return out


def matches(params: dict, selected: dict, skip: str | None = None) -> bool:
    return all(any(v in values for v in params.get(name, ())) for name, values in selected.items() if name != skip)


def _natural(value: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", value)]


def facets(rows: list[dict], selected: dict) -> list[dict]:
    """Параметри для панелі: [{name, values: [{value, count, active}], active}].
    Лічильник — серед товарів, що відповідають решті вибраних параметрів (як на DigiKey)."""
    names = {}
    for params in rows:
        for name in params:
            names[name] = names.get(name, 0) + 1
    out = []
    for name, have in sorted(names.items(), key=lambda x: (-x[1], x[0].lower())):
        counts = {}
        for params in rows:
            if name in params and matches(params, selected, skip=name):
                for v in params[name]:
                    counts[v] = counts.get(v, 0) + 1
        chosen = selected.get(name, set())
        if not chosen and (len(counts) < 2 or len(counts) > MAX_DISTINCT
                           or (len(counts) > 25 and len(counts) > MOSTLY_UNIQUE * have)):
            continue   # усі однакові або майже в кожного своє — фільтрувати нічого
        values = sorted(counts.items(), key=lambda x: (-x[1], _natural(x[0])))
        for v in sorted(chosen - set(counts)):   # вибране, якого серед решти вже немає, — щоб можна було зняти
            values.append((v, 0))
        out.append({"name": name, "active": bool(chosen),
                    "values": [{"value": v, "count": c, "active": v in chosen} for v, c in values]})
    return out


class ParamFilter(admin.ListFilter):
    """?attr=Назва:значення (можна кілька). У бічній панелі — лише вибрані параметри з «скинути»;
    вибір — на панелі над списком. Ставити останнім у list_filter: решта фільтрів уже застосована."""
    title = "Параметри"
    parameter_name = "attr"
    prefix = ""   # шлях до товару, напр. «product__»
    template = "admin/inventory/param_filter.html"

    def __init__(self, request, params, model, model_admin):
        super().__init__(request, params, model, model_admin)
        raw = params.pop(self.parameter_name, [])
        self.raw = [raw] if isinstance(raw, str) else list(raw)
        self.selected = parse(self.raw)
        if self.raw:
            self.used_parameters[self.parameter_name] = self.raw

    def has_output(self):
        return True   # інакше Django не викликає queryset()

    def expected_parameters(self):
        return [self.parameter_name]

    def choices(self, changelist):
        for item in self.raw:
            rest = [x for x in self.raw if x != item]
            yield {"display": item.replace(":", ": ", 1), "query_string": changelist.get_query_string(
                {self.parameter_name: rest or None, "p": None})}   # не remove=["p"]: прибрало б і product__…

    def queryset(self, request, qs):
        rows = list(qs.values_list("pk", self.prefix + "tech_attributes"))
        request._param_rows = [(pk, params_of(attrs)) for pk, attrs in rows]   # для панелі параметрів
        if not self.selected:
            return qs
        return qs.filter(pk__in=[pk for pk, params in request._param_rows if matches(params, self.selected)])


class ListingParamFilter(ParamFilter):
    prefix = "product__"


def panel_context(request, changelist, category_param: str) -> dict:
    """Дані панелі над списком: кнопки параметрів (лише коли вибрано категорію), вибрані значення-чипи."""
    rows = getattr(request, "_param_rows", None)
    raw = request.GET.getlist(ParamFilter.parameter_name)
    selected = parse(raw)
    has_category = any(k.startswith(category_param) for k in request.GET)
    if rows is None or not has_category:
        return {"param_panel": {"hint": not has_category, "facets": [], "selected": bool(selected)}}

    def link(new_raw):   # номер сторінки — скинути; remove=["p"] прибрав би й «product__category»
        return changelist.get_query_string({ParamFilter.parameter_name: new_raw or None, "p": None})
    groups = []
    for f in facets([params for _pk, params in rows], selected):
        f["base_url"] = link([x for x in raw if not x.startswith(f["name"] + ":")])   # без вибору цього параметра
        f["n_active"] = sum(1 for v in f["values"] if v["active"])
        groups.append(f)
    chips = [{"name": n, "value": v, "url": link([x for x in raw if x != f"{n}:{v}"])}
             for n, values in selected.items() for v in sorted(values, key=_natural)]
    return {"param_panel": {"hint": False, "facets": groups, "selected": bool(selected), "chips": chips,
                            "clear_url": link([])}}


def params_map(products) -> dict[int, dict]:
    """{pk: параметри} — для фільтра на сторінці (огляд залишків)."""
    return {p.pk: params_of(p.tech_attributes) for p in products}
