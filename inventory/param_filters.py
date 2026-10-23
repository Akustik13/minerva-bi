"""Пошук за технічними параметрами, як на DigiKey: обираєте категорію — над списком її параметри (тип штекера,
товщина кабелю, частота…) зі значеннями й кількістю товарів; клік — фільтр, кілька значень одного параметра —
«або», різних параметрів — «і».

Параметри — Product.tech_attributes без службових кодів DigiKey (api.serializers.split_attributes).
Списки: Товари й Асортимент і ціни — на сервері (?attr=Назва:значення, ParamFilter + facets), Склад → огляд
залишків — на сторінці (JS, дані з params_map).
"""
from __future__ import annotations

import re

from django.contrib import admin

MAX_DISTINCT = 60      # параметр, де майже в кожного товару своє значення, як фільтр не допомагає
SHOWN_VALUES = 12      # решта значень — під «ще …»


def _text(value) -> str:
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value)
    return re.sub(r"\s+", " ", str(value)).strip()


def params_of(attrs) -> dict[str, str]:
    """Технічні параметри товару {назва: значення-рядок}."""
    from api.serializers import split_attributes
    return {k: _text(v) for k, v in split_attributes(attrs)[0].items() if _text(v)}


def parse(values) -> dict[str, set[str]]:
    """['Termination:SMA', 'Termination:U.FL', 'Gain:2dBi'] → {назва: {значення}}."""
    out = {}
    for item in values or []:
        name, sep, value = str(item).partition(":")
        if sep and name.strip() and value.strip():
            out.setdefault(name.strip(), set()).add(value.strip())
    return out


def matches(params: dict, selected: dict, skip: str | None = None) -> bool:
    return all(params.get(name) in values for name, values in selected.items() if name != skip)


def _natural(value: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", value)]


def facets(rows: list[dict], selected: dict) -> list[dict]:
    """Параметри для панелі: [{name, values: [{value, count, active}], active, more}].
    Лічильник значення — серед товарів, що відповідають решті вибраних параметрів (як на DigiKey)."""
    names = {}
    for params in rows:
        for name in params:
            names[name] = names.get(name, 0) + 1
    out = []
    for name, have in sorted(names.items(), key=lambda x: (-x[1], x[0].lower())):
        counts = {}
        for params in rows:
            if name in params and matches(params, selected, skip=name):
                counts[params[name]] = counts.get(params[name], 0) + 1
        chosen = selected.get(name, set())
        if not chosen and (len(counts) < 2 or len(counts) > MAX_DISTINCT):
            continue   # усі однакові або в кожного своє — нічого фільтрувати
        values = sorted(counts.items(), key=lambda x: (-x[1], _natural(x[0])))
        for v in chosen - set(counts):   # вибране, якого серед решти вже немає, — щоб можна було зняти
            values.append((v, 0))
        out.append({"name": name, "active": bool(chosen),
                    "values": [{"value": v, "count": c, "active": v in chosen} for v, c in values]})
    return out


class ParamFilter(admin.ListFilter):
    """?attr=Назва:значення (можна кілька). У бічній панелі — лише вибрані параметри з «скинути»;
    вибір — на панелі над списком (facets). Ставити останнім у list_filter: решта фільтрів уже застосована."""
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
    """Дані панелі над списком: параметри (лише коли вибрано категорію), посилання на вибір / зняття."""
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
        for v in f["values"]:
            item = f"{f['name']}:{v['value']}"
            v["url"] = link([x for x in raw if x != item] if v["active"] else raw + [item])
        f["clear_url"] = link([x for x in raw if not x.startswith(f["name"] + ":")])
        f["shown"], f["more"] = f["values"][:SHOWN_VALUES], f["values"][SHOWN_VALUES:]
        groups.append(f)
    return {"param_panel": {"hint": False, "facets": groups, "selected": bool(selected), "clear_url": link([])}}


def params_map(products) -> dict[int, dict]:
    """{pk: параметри} — для фільтра на сторінці (огляд залишків)."""
    return {p.pk: params_of(p.tech_attributes) for p in products}
