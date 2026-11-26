"""🔄 Швидкі вигляди списку закупівель — чипи над списком `/admin/inventory/purchaseorder/`.

«🔄 В роботі» (чернетка / замовлено / частково — ще не закрито), «✏️ Чернетки» (лише коли є), «🛒 Замовлено»,
«📦 Частково отримано», «✅ Доставлено / отримано», «❌ Скасовано», «Усі» — з лічильником у кожному.

Адреса — `?view=<вигляд>` (`PARAM`); старе `?status__exact=<статус>` — те саме, що вигляд цього статусу.
Вигляд не є фільтром Django (`POChangeList.get_filters_params` його прибирає): «✖ Скинути фільтри» праворуч
його не скидає, а сортування, сторінки й пошук його зберігають. Лічильники — з тими самими пошуком і фільтрами
праворуч, лише без вигляду (`counts`).

Вибір людини — `core.list_views` (`LIST_KEY`): явний `?view=` запам'ятовується, список без жодних параметрів
відкривається з останнім вибраним (`start_url`; ще нічого не вибирали — `DEFAULT`), «Усі» теж запам'ятовується.
"""
from django.contrib.admin.views.main import ChangeList
from django.db.models import Count

from core import list_views

PARAM = "view"
LIST_KEY = "inventory.purchaseorder"
DEFAULT = "active"
ALL = "all"

# ключ, підпис, статуси (None — усі), тон (кольори — змінні теми в change_list.html), підказка, лише коли є
VIEWS = (
    ("active", "🔄 В роботі", ("draft", "ordered", "partial"), "accent",
     "Ще не закрито: чернетки, замовлено, частково отримано", False),
    ("draft", "✏️ Чернетки", ("draft",), "muted", "Ще не замовлено в постачальника", True),
    ("ordered", "🛒 Замовлено", ("ordered",), "accent", "Замовлено в постачальника, ще нічого не отримано", False),
    ("partial", "📦 Частково отримано", ("partial",), "warn", "Отримано не все — частину ще чекаємо", False),
    ("received", "✅ Доставлено / отримано", ("received",), "ok", "Усе отримано й оприбутковано", False),
    ("cancelled", "❌ Скасовано", ("cancelled",), "err", "Скасовані закупівлі", False),
    (ALL, "Усі", None, "muted", "Усі закупівлі, будь-який статус", False),
)
KEYS = tuple(v[0] for v in VIEWS)
STATUSES = {key: statuses for key, _l, statuses, *_rest in VIEWS}
# ?status__exact=<статус> (старі посилання, закладки) — вигляд з одним цим статусом
STATUS_VIEW = {statuses[0]: key for key, statuses in STATUSES.items() if statuses and len(statuses) == 1}
STATUS_PARAM = "status__exact"


def current(params):
    """Вигляд за параметрами адреси (dict GET.items()); None — не вибрано (тобто «Усі»)."""
    view = params.get(PARAM)
    if view in KEYS:
        return view
    return STATUS_VIEW.get(params.get(STATUS_PARAM))


def apply(qs, view):
    statuses = STATUSES.get(view)
    return qs.filter(status__in=statuses) if statuses else qs


def saved(user):
    view = list_views.saved(user, LIST_KEY)
    return view if view in KEYS else DEFAULT


def remember(user, view):
    if view in KEYS:
        list_views.remember(user, LIST_KEY, view)


def start_url(request):
    """Куди перекинути список, відкритий без жодних параметрів (None — лишити як є: «Усі»)."""
    if request.method != "GET" or request.GET:
        return None
    view = saved(request.user)
    return None if view == ALL else f"{request.path}?{PARAM}={view}"


class POChangeList(ChangeList):
    """Список закупівель з виглядом `?view=`: не фільтр Django, але застосовується до списку."""

    def get_filters_params(self, params=None):
        lookup = super().get_filters_params(params)
        lookup.pop(PARAM, None)
        if lookup.get(STATUS_PARAM) and lookup[STATUS_PARAM][-1] in STATUS_VIEW:
            lookup.pop(STATUS_PARAM)                    # те саме, що вигляд (current) — не окремий фільтр
        return lookup

    def get_queryset(self, request, exclude_parameters=None):
        qs = super().get_queryset(request, exclude_parameters)
        if exclude_parameters == [PARAM]:              # лічильники чипів — без вигляду
            return qs
        return apply(qs, current(self.params))


def counts(cl, request):
    """{статус: кількість} — з пошуком і фільтрами праворуч, без вигляду."""
    qs = cl.get_queryset(request, exclude_parameters=[PARAM])
    rows = qs.prefetch_related(None).order_by().values("status").annotate(n=Count("pk", distinct=True))
    return {r["status"]: r["n"] for r in rows}


def chips(cl, request):
    """Чипи для шаблону: [{key, label, count, href, on, tone, hint}]."""
    by_status = counts(cl, request)
    total = sum(by_status.values())
    view = current(cl.params) or ALL
    out = []
    for key, label, statuses, tone, hint, only_if_any in VIEWS:
        n = total if statuses is None else sum(by_status.get(s, 0) for s in statuses)
        if only_if_any and not n and key != view:
            continue
        out.append({"key": key, "label": label, "count": n, "on": key == view, "tone": tone, "hint": hint,
                    "href": cl.get_query_string({PARAM: key, STATUS_PARAM: None, "p": None})})
    return out
