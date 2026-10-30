"""
dashboard/views.py — Minerva Dashboard v2

Що рахується і як — sales/revenue.py (одне правило з тенденціями, сигналами, дайджестами й RFM):
  - лише продажі: sale_orders() — SALE, не скасовані, з «Affects stock»; що чекає відправки — to_ship();
  - сума замовлення — SalesOrder.total_price (DigiKey: разом з рядками, що не зіставились), порожня — сума рядків;
    з фільтром категорії — сума рядків цієї категорії;
  - усе у валюті Minerva за курсами «Налаштувань цін» магазину; валюта без курсу — окремо («+ $1,000 без курсу»).
Період: N днів = N календарних днів до сьогодні включно (Europe/Berlin), попередній — стільки ж днів перед ним.
"""
from datetime import date, timedelta
import json

from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, F, Min, Sum
from django.db.models.functions import ExtractYear, TruncMonth
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date

from sales import revenue as rv


def _js(value):
    """JSON для вставки в <script> через |safe: назви з бази («</script>», «&») не виходять за межі скрипту."""
    return json.dumps(value).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def _month(p):
    return TruncMonth(p + 'order_date')


def _month_key(value):
    """TruncMonth → date першого дня місяця (PostgreSQL може віддати datetime)."""
    if hasattr(value, 'date') and callable(value.date):
        value = value.date()
    return value.replace(day=1) if value else value


def period_bounds(today, date_from=None, date_to=None, period=''):
    """Межі періоду й попереднього такої самої довжини, обидва включно.

    Свій діапазон «з … по …» — як вибрано; інакше пресет N днів (за замовчуванням 90): N днів до сьогодні включно."""
    if date_from and date_to and date_from <= date_to:
        days = (date_to - date_from).days + 1
        label = f"{date_from} – {date_to}"
    else:
        try:
            days = int(period) if period else 90
        except (TypeError, ValueError):
            days = 90
        days = max(1, days)
        date_to = today
        date_from = today - timedelta(days=days - 1)
        label = f"{days} днів"
    prev_to = date_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=days - 1)
    return {'from': date_from, 'to': date_to, 'days': days, 'label': label,
            'prev_from': prev_from, 'prev_to': prev_to}


def _date_param(request, name):
    try:
        return parse_date(request.GET.get(name, '').strip())
    except ValueError:      # «2026-02-30»
        return None


def _pct(curr, prev):
    if not prev:
        return None
    return round((curr - prev) / prev * 100, 1)


def _known_orders(qs, rates):
    """Скільки замовлень у валютах, для яких є курс (для середнього чека)."""
    return sum(r['n'] for r in qs.order_by().values('currency').annotate(n=Count('id'))
               if rates.rate(r['currency']) is not None)


def _costs(qs, field, rates):
    """Доставка по групі field: [{field, orders, avg_cost, total_cost}] у валюті Minerva (shipping_currency)."""
    money = rv.converted(qs, 'shipping_cost', 'shipping_currency', (field,), rates)
    counts = {}
    for r in qs.order_by().values(field, 'shipping_currency').annotate(n=Count('id')):
        c = counts.setdefault(r[field], {'orders': 0, 'known': 0})
        c['orders'] += r['n']
        if rates.rate(r['shipping_currency']) is not None:
            c['known'] += r['n']
    rows = []
    for value, c in counts.items():
        m = money.get((value,))
        total = float(m.total) if m else 0.0
        rows.append({field: value, 'orders': c['orders'],
                     'total_cost': total, 'avg_cost': total / c['known'] if c['known'] else None,
                     'missing': m.missing_text if m else ''})
    rows.sort(key=lambda r: -r['orders'])
    return rows


@staff_member_required
def dashboard(request):
    now   = timezone.now()
    today = timezone.localdate()

    from sales.models import SalesOrder, SalesOrderLine

    rates = rv.Rates()
    home = rates.home

    # ── Фільтри ───────────────────────────────────────────────────────────────
    filter_source   = request.GET.get('source', '').strip()
    filter_category = request.GET.get('category', '').strip()
    period          = request.GET.get('period', '').strip()
    b = period_bounds(today, _date_param(request, 'date_from'), _date_param(request, 'date_to'), period)
    date_from, date_to, period_days = b['from'], b['to'], b['days']
    prev_date_from, prev_date_to = b['prev_from'], b['prev_to']

    # ── Базові queryset-и ──────────────────────────────────────────────────────
    orders_all = SalesOrder.objects.all()
    if filter_source:
        orders_all = orders_all.filter(source=filter_source)
    qs_all = rv.sale_orders(orders_all)

    qs_period = qs_all.filter(order_date__gte=date_from, order_date__lte=date_to)
    qs_prev   = qs_all.filter(order_date__gte=prev_date_from, order_date__lte=prev_date_to)
    qs_lines  = SalesOrderLine.objects.filter(order__in=qs_period)
    qs_prev_lines = SalesOrderLine.objects.filter(order__in=qs_prev)
    if filter_category:
        qs_lines      = qs_lines.filter(product__category=filter_category)
        qs_prev_lines = qs_prev_lines.filter(product__category=filter_category)

    # Доступні значення для dropdown-ів
    all_sources    = list(
        SalesOrder.objects.values_list('source', flat=True).distinct().order_by('source'))
    all_categories = list(
        SalesOrderLine.objects.values_list('product__category', flat=True)
        .distinct().exclude(product__category='').order_by('product__category'))

    # ── KPI ───────────────────────────────────────────────────────────────────
    if filter_category:
        # з категорією — лише рядки цієї категорії
        rev_curr = rv.line_revenue(qs_lines, rates=rates)
        rev_prev = rv.line_revenue(qs_prev_lines, rates=rates)
    else:
        rev_curr = rv.revenue(qs_period, rates=rates)
        rev_prev = rv.revenue(qs_prev, rates=rates)
    ord_curr   = qs_period.count()
    ord_prev   = qs_prev.count()
    units_curr = float(qs_lines.aggregate(t=Sum('qty'))['t'] or 0)
    units_prev = float(qs_prev_lines.aggregate(t=Sum('qty'))['t'] or 0)
    known_curr = _known_orders(qs_period, rates)
    known_prev = _known_orders(qs_prev, rates)
    avg_curr   = float(rev_curr.total) / known_curr if known_curr else 0
    avg_prev   = float(rev_prev.total) / known_prev if known_prev else 0
    qs_to_ship = rv.to_ship(orders_all)
    unshipped_cnt = qs_to_ship.count()

    kpi = {
        'revenue':   {'val': float(rev_curr.total), 'fmt': rev_curr.text, 'note': rev_curr.missing_text,
                      'change': _pct(float(rev_curr.total), float(rev_prev.total))},
        'orders':    {'val': ord_curr,   'fmt': str(ord_curr),          'change': _pct(ord_curr, ord_prev)},
        'units':     {'val': units_curr, 'fmt': f'{units_curr:,.0f}',  'change': _pct(units_curr, units_prev)},
        'avg_order': {'val': avg_curr,   'fmt': rv.fmt(avg_curr, home) if avg_curr else '—',
                      'change': _pct(avg_curr, avg_prev)},
        'unshipped': {'val': unshipped_cnt, 'fmt': str(unshipped_cnt), 'change': None},
    }

    # ── Виручка по місяцях ────────────────────────────────────────────────────
    if filter_category:
        rev_m = rv.line_revenue(qs_lines, by={'month': TruncMonth('order__order_date')}, rates=rates)
    else:
        rev_m = rv.revenue(qs_period, by={'month': _month}, rates=rates)
    rev_lookup = {_month_key(k[0]): m for k, m in rev_m.items()}
    ord_lookup = {
        _month_key(r['month']): r['cnt']
        for r in qs_period.order_by().annotate(month=TruncMonth('order_date')).values('month').annotate(cnt=Count('id'))
    }
    # усі місяці вибраного періоду — і без замовлень
    months = []
    ym, end = date(date_from.year, date_from.month, 1), date(date_to.year, date_to.month, 1)
    while ym <= end:
        months.append(ym)
        ym = date(ym.year + (ym.month == 12), (ym.month % 12) + 1, 1)
    chart_months   = [str(m)[:7] for m in months]
    chart_rev      = [float(rev_lookup[m].total) if m in rev_lookup else 0.0 for m in months]
    chart_orders_m = [int(ord_lookup.get(m, 0)) for m in months]

    # ── Замовлення по днях (order_date — дата, без перерахунку часових поясів) ──
    orders_by_day = list(
        qs_period.order_by().values('order_date').annotate(cnt=Count('id')).order_by('order_date')
    )
    chart_days    = [str(r['order_date']) for r in orders_by_day]
    chart_day_ord = [int(r['cnt'] or 0) for r in orders_by_day]

    # ── Топ SKU по виручці ────────────────────────────────────────────────────
    sku_fields = ('product__sku', 'product__name', 'product__category')
    sku_money = rv.line_revenue(qs_lines, by=list(sku_fields), rates=rates)
    top_skus = list(
        qs_lines.order_by().values(*sku_fields)
        .annotate(qty_total=Sum('qty'), orders_cnt=Count('order', distinct=True))
    )
    for r in top_skus:
        m = sku_money.get(tuple(r[f] for f in sku_fields))
        r['revenue'] = float(m.total) if m else 0.0
    top_skus.sort(key=lambda r: -r['revenue'])
    top_skus = top_skus[:15]

    # ── Топ клієнти по виручці ─────────────────────────────────────────────────
    # Кількість замовлень, одиниці й виручка — окремими запитами: в одному annotate з рядками
    # кожне замовлення множилось би на кількість своїх рядків.
    client_fields = ('client', 'shipping_region')
    qs_clients = qs_period.exclude(client='')
    client_money = rv.revenue(qs_clients, by=list(client_fields), rates=rates)
    client_units = {
        (r['order__client'], r['order__shipping_region']): r['u']
        for r in SalesOrderLine.objects.filter(order__in=qs_clients).order_by()
        .values('order__client', 'order__shipping_region').annotate(u=Sum('qty'))
    }
    top_clients = list(qs_clients.order_by().values(*client_fields).annotate(orders=Count('id')))
    for r in top_clients:
        key = (r['client'], r['shipping_region'])
        m = client_money.get(key)
        r['revenue'] = float(m.total) if m else 0.0
        r['units'] = client_units.get(key)
    top_clients.sort(key=lambda r: (-r['revenue'], -r['orders']))
    top_clients = top_clients[:10]

    # ── Географія ──────────────────────────────────────────────────────────────
    by_region = list(
        qs_period
        .exclude(shipping_region='')
        .order_by()
        .values('shipping_region')
        .annotate(orders=Count('id'))
        .order_by('-orders')[:20]
    )
    max_region_orders = by_region[0]['orders'] if by_region else 1
    for r in by_region:
        r['pct'] = round(r['orders'] / max_region_orders * 100)

    # ── Статистика доставки (вартість — у валюті Minerva) ─────────────────────
    by_courier = _costs(qs_period.exclude(shipping_courier=''), 'shipping_courier', rates)
    shipping_by_country = _costs(qs_period.exclude(shipping_region=''), 'shipping_region', rates)[:20]

    # Топ-2 кур'єри по кожній країні
    courier_country_raw = list(
        qs_period
        .exclude(shipping_courier='')
        .exclude(shipping_region='')
        .order_by()
        .values('shipping_region', 'shipping_courier')
        .annotate(cnt=Count('id'))
        .order_by('shipping_region', '-cnt')
    )
    courier_by_country = {}
    for item in courier_country_raw:
        lst = courier_by_country.setdefault(item['shipping_region'], [])
        if len(lst) < 2:
            lst.append(item['shipping_courier'])
    for row in shipping_by_country:
        row['couriers'] = ', '.join(courier_by_country.get(row['shipping_region'], []))

    chart_courier_labels = [r['shipping_courier'] for r in by_courier]
    chart_courier_orders = [r['orders'] for r in by_courier]

    chart_geo_labels = [r['shipping_region'] for r in by_region[:8]]
    chart_geo_orders = [int(r['orders']) for r in by_region[:8]]

    # ── По категоріях ──────────────────────────────────────────────────────────
    by_category = list(
        qs_lines
        .order_by()
        .values('product__category')
        .annotate(qty_total=Sum('qty'), orders_cnt=Count('order', distinct=True))
        .order_by('-qty_total')
    )
    chart_cat_labels = [r['product__category'] or 'Other' for r in by_category]
    chart_cat_qty    = [float(r['qty_total'] or 0) for r in by_category]

    # ── Джерела ────────────────────────────────────────────────────────────────
    by_source = list(
        qs_period
        .order_by()
        .values('source')
        .annotate(orders=Count('id'))
        .order_by('-orders')
    )
    max_src = by_source[0]['orders'] if by_source else 1
    for s in by_source:
        s['pct'] = round(s['orders'] / max_src * 100)

    # ── Не відправлені ─────────────────────────────────────────────────────────
    unshipped_list = list(
        qs_to_ship
        .order_by('order_date')
        .values('id', 'order_number', 'source', 'order_date',
                'client', 'shipping_region')[:30]
    )
    for o in unshipped_list:
        d = o['order_date']
        o['days_waiting'] = (today - d).days if d else 0

    # ── Критичний склад ────────────────────────────────────────────────────────
    critical_stock = []
    try:
        from inventory.models import Product, InventoryTransaction
        since3m = today - timedelta(days=90)
        for p in Product.objects.filter(is_active=True):
            stock = float(InventoryTransaction.objects.filter(
                product=p).aggregate(t=Sum('qty'))['t'] or 0)
            sold = float(rv.sale_lines(SalesOrderLine.objects.filter(
                product=p, order__order_date__gte=since3m
            )).aggregate(t=Sum('qty'))['t'] or 0)
            monthly = sold / 3
            if monthly > 0:
                months_left = max(0, stock / monthly)
                if months_left < 1.5:
                    critical_stock.append({
                        'sku': p.sku, 'stock': int(stock),
                        'monthly': round(monthly, 1),
                        'months': round(months_left, 1),
                    })
        critical_stock.sort(key=lambda x: x['months'])
        critical_stock = critical_stock[:8]
    except Exception:
        pass

    # ── Shipping performance KPI ──────────────────────────────────────────────
    qs_with_dl = qs_period.filter(shipped_at__isnull=False, shipping_deadline__isnull=False)
    total_with_deadline = qs_with_dl.count()
    on_time_cnt         = qs_with_dl.filter(shipped_at__lte=F('shipping_deadline')).count()
    overdue_shipped_cnt = qs_with_dl.filter(shipped_at__gt=F('shipping_deadline')).count()
    overdue_pending_cnt = qs_to_ship.filter(shipping_deadline__isnull=False, shipping_deadline__lt=today).count()
    on_time_pct = round(on_time_cnt / total_with_deadline * 100) if total_with_deadline else None

    ship_pairs = list(
        qs_period.filter(shipped_at__isnull=False, order_date__isnull=False)
        .values_list('order_date', 'shipped_at')
    )
    ship_days = [(s - o).days for o, s in ship_pairs if s and o and s >= o]
    avg_ship_days = round(sum(ship_days) / len(ship_days), 1) if ship_days else None

    del_days = []
    for s, d in qs_period.filter(delivered_at__isnull=False, shipped_at__isnull=False).values_list(
            'shipped_at', 'delivered_at'):
        d_date = timezone.localtime(d).date() if timezone.is_aware(d) else d.date()
        if d_date >= s:
            del_days.append((d_date - s).days)
    avg_del_days = round(sum(del_days) / len(del_days), 1) if del_days else None

    shipping_kpi = {
        'on_time_pct':        on_time_pct,
        'on_time_cnt':        on_time_cnt,
        'overdue_shipped':    overdue_shipped_cnt,
        'overdue_pending':    overdue_pending_cnt,
        'avg_ship_days':      avg_ship_days,
        'avg_del_days':       avg_del_days,
        'total_with_deadline': total_with_deadline,
    }

    # ── Топ товари по попиту (кількість) ─────────────────────────────────────
    top_demand = list(
        qs_lines
        .order_by()
        .values('product__sku', 'product__category')
        .annotate(qty_total=Sum('qty'), orders_cnt=Count('order', distinct=True))
        .order_by('-orders_cnt')[:8]
    )

    ctx = admin.site.each_context(request)
    ctx.update({
        # Фільтри
        'period':               period,
        'period_days':          period_days,
        'period_label':         b['label'],
        'date_from_str':        str(date_from),
        'date_to_str':          str(date_to),
        'prev_date_from_str':   str(prev_date_from),
        'prev_date_to_str':     str(prev_date_to),
        'filter_source':        filter_source,
        'filter_category':      filter_category,
        'all_sources':          all_sources,
        'all_categories':       all_categories,
        # Валюта
        'cur_code':             home,
        'cur_sym':              rv.symbol(home),
        'cur_sym_js':           _js(rv.symbol(home)),
        # KPI
        'kpi': kpi,
        # Графіки
        'chart_months':     _js(chart_months),
        'chart_rev':        _js(chart_rev),
        'chart_orders_m':   _js(chart_orders_m),
        'chart_days':       _js(chart_days),
        'chart_day_ord':    _js(chart_day_ord),
        'chart_geo_labels': _js(chart_geo_labels),
        'chart_geo_orders': _js(chart_geo_orders),
        'chart_cat_labels': _js(chart_cat_labels),
        'chart_cat_qty':    _js(chart_cat_qty),
        # Таблиці
        'top_skus':             top_skus,
        'top_clients':          top_clients,
        'top_demand':           top_demand,
        'by_region':            by_region,
        'by_source':            by_source,
        'unshipped_list':       unshipped_list,
        'critical_stock':       critical_stock,
        'shipping_kpi':         shipping_kpi,
        # Доставка
        'by_courier':           by_courier,
        'shipping_by_country':  shipping_by_country,
        'chart_courier_labels': _js(chart_courier_labels),
        'chart_courier_orders': _js(chart_courier_orders),
    })
    return render(request, 'dashboard/dashboard.html', ctx)


def ytd_spans(today, year):
    """Однакові відрізки року для порівняння YoY: поточний рік — 1 січня … сьогодні проти того самого
    відрізка минулого року (29 лютого → 28-ме); інший рік — повний рік проти повного."""
    if year == today.year:
        cur = (date(year, 1, 1), today)
        try:
            prev_end = today.replace(year=year - 1)
        except ValueError:
            prev_end = date(year - 1, 2, 28)
        return cur, (date(year - 1, 1, 1), prev_end)
    return (date(year, 1, 1), date(year, 12, 31)), (date(year - 1, 1, 1), date(year - 1, 12, 31))


@staff_member_required
def trends_view(request):
    """Trend analytics: YoY, MoM growth, quarterly, shipping + stock sections.
    Supports dynamic year range via year_from / year_to GET params.
    YoY KPI compares the same span of both years (YTD up to today vs the same day a year ago).
    Revenue — sales/revenue.py (sales only, Minerva currency)."""
    from sales.models import SalesOrder, SalesOrderLine

    today = timezone.localdate()
    current_year    = today.year
    current_quarter = (today.month - 1) // 3 + 1
    rates = rv.Rates()
    home = rates.home

    filter_source = request.GET.get('source', '').strip()

    orders_all = SalesOrder.objects.all()
    if filter_source:
        orders_all = orders_all.filter(source=filter_source)
    qs_all = rv.sale_orders(orders_all)

    all_sources = list(
        SalesOrder.objects.values_list('source', flat=True).distinct().order_by('source'))

    # ── Year range filter ─────────────────────────────────────────────────────
    min_row = rv.sale_orders().filter(order_date__isnull=False).aggregate(d=Min('order_date'))
    min_db_year     = min_row['d'].year if min_row['d'] else (current_year - 5)
    available_years = list(range(min_db_year, current_year + 1))

    try:
        year_from = int(request.GET['year_from'])
    except (KeyError, ValueError, TypeError):
        year_from = max(min_db_year, current_year - 2)
    try:
        year_to = int(request.GET['year_to'])
    except (KeyError, ValueError, TypeError):
        year_to = current_year

    year_from = max(min_db_year, min(year_from, current_year))
    year_to   = max(year_from, min(year_to, current_year))
    years_to_show = list(range(year_from, year_to + 1))

    # ── Helpers ────────────────────────────────────────────────────────────────
    def month_start_end(y, m):
        start = date(y, m, 1)
        end   = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        return start, end

    def _qs_rev(qs_orders):
        return float(rv.revenue(qs_orders, rates=rates).total)

    # ── Per-year data (один запит на виручку й один на кількість за весь діапазон) ──
    qs_range = qs_all.filter(order_date__year__gte=year_from, order_date__year__lte=year_to)
    rev_m = rv.revenue(qs_range, by={'month': _month}, rates=rates)
    range_money = rv.total_missing(rev_m)
    ord_m = {
        _month_key(r['month']): r['n']
        for r in qs_range.order_by().annotate(month=TruncMonth('order_date')).values('month').annotate(n=Count('id'))
    }
    all_years_data = {y: {} for y in years_to_show}
    for k, m in rev_m.items():
        mk = _month_key(k[0])
        all_years_data.setdefault(mk.year, {})[mk.month] = {'revenue': float(m.total), 'orders': ord_m.get(mk, 0)}
    for mk, n in ord_m.items():
        all_years_data.setdefault(mk.year, {}).setdefault(mk.month, {'revenue': 0.0, 'orders': n})

    months_labels = ['Січ', 'Лют', 'Бер', 'Кві', 'Тра', 'Чер',
                     'Лип', 'Сер', 'Вер', 'Жов', 'Лис', 'Гру']

    # YoY chart datasets — one entry per year in range
    yoy_datasets = [
        {
            'year': y,
            'rev': [all_years_data[y].get(m, {}).get('revenue', 0) for m in range(1, 13)],
            'ord': [all_years_data[y].get(m, {}).get('orders', 0) for m in range(1, 13)],
        }
        for y in years_to_show
    ]

    # 3-month MA for the most recent year in the selected range
    smooth_year  = years_to_show[-1]
    smooth_data  = all_years_data[smooth_year]
    smooth_rev   = [smooth_data.get(m, {}).get('revenue', 0) for m in range(1, 13)]
    curr_rev_smooth = []
    for i in range(12):
        window = [smooth_rev[j] for j in range(max(0, i - 2), i + 1) if smooth_rev[j] > 0]
        curr_rev_smooth.append(round(sum(window) / len(window), 1) if window else 0)

    # ── Rolling 13 months → 12 MoM growth rates ──────────────────────────────
    rolling_months = []
    for i in range(12, -1, -1):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        start, end = month_start_end(y, m)
        qs_m = qs_all.filter(order_date__gte=start, order_date__lt=end)
        rolling_months.append({'label': start.strftime('%b %Y'), 'rev': _qs_rev(qs_m)})

    mom_labels = [rolling_months[i]['label'] for i in range(1, 13)]
    mom_growth = []
    for i in range(1, 13):
        pv = rolling_months[i - 1]['rev']
        cv = rolling_months[i]['rev']
        mom_growth.append(round((cv - pv) / pv * 100, 1) if pv else None)

    # ── Quarterly table ────────────────────────────────────────────────────────
    quarters = []
    for year in years_to_show:
        for q in range(1, 5):
            is_future = (year == current_year and q > current_quarter) or year > current_year
            if is_future:
                rev_q, ord_q = 0, 0
            else:
                months_q = range((q - 1) * 3 + 1, q * 3 + 1)
                rev_q = sum(all_years_data[year].get(m, {}).get('revenue', 0) for m in months_q)
                ord_q = sum(all_years_data[year].get(m, {}).get('orders', 0) for m in months_q)
            quarters.append({
                'year': year, 'q': q, 'is_future': is_future,
                'revenue': rev_q, 'orders': ord_q,
            })

    q_by_key = {(r['year'], r['q']): r for r in quarters}
    for r in quarters:
        if r['is_future']:
            r['delta_pct'] = None
        else:
            prev_r = q_by_key.get((r['year'] - 1, r['q']))
            if prev_r and not prev_r['is_future'] and prev_r['revenue']:
                r['delta_pct'] = round(
                    (r['revenue'] - prev_r['revenue']) / prev_r['revenue'] * 100, 1)
            else:
                r['delta_pct'] = None

    # ── Summary KPIs — from selected year range ───────────────────────────────
    range_month_revs = []
    for yr in years_to_show:
        for m in range(1, 13):
            rev = all_years_data[yr].get(m, {}).get('revenue', 0)
            if rev > 0:
                range_month_revs.append((f"{months_labels[m - 1]} {yr}", rev))
    best_month  = max(range_month_revs, key=lambda x: x[1]) if range_month_revs else None
    worst_month = min(range_month_revs, key=lambda x: x[1]) if range_month_revs else None
    avg_monthly = round(sum(r for _, r in range_month_revs) / len(range_month_revs)) if range_month_revs else 0

    # KPI: use year_to (most recent selected year), not always current_year
    latest_yr_data  = all_years_data[year_to]
    curr_year_total = sum(latest_yr_data.get(m, {}).get('revenue', 0) for m in range(1, 13))

    # YoY KPI: однакові відрізки обох років (поточний рік — 1 січня … сьогодні)
    (cur_a, cur_b), (prev_a, prev_b) = ytd_spans(today, year_to)
    curr_ytd = _qs_rev(qs_all.filter(order_date__gte=cur_a, order_date__lte=cur_b))
    prev_ytd = _qs_rev(qs_all.filter(order_date__gte=prev_a, order_date__lte=prev_b))
    yoy_vs_year = year_to - 1
    yoy_growth = round((curr_ytd - prev_ytd) / prev_ytd * 100, 1) if prev_ytd else None
    yoy_ytd_label = f"{cur_b.day} {months_labels[cur_b.month - 1]}" if year_to == current_year else 'Гру'

    # ── Shipping analytics ─────────────────────────────────────────────────────
    # Normalize courier names in Python to merge "dhl"/"DHL"/"DHL Express" → "DHL"
    from sales.utils import normalize_courier as _nc
    raw_courier_rows = list(
        qs_all.exclude(shipping_courier='')
        .filter(order_date__year__in=years_to_show)
        .order_by()
        .values('order_date__year', 'shipping_courier')
        .annotate(cnt=Count('id'))
    )
    total_courier_counts = {}
    year_courier_counts  = {}
    for row in raw_courier_rows:
        yr  = row['order_date__year']
        nc  = _nc(row['shipping_courier'])
        cnt = row['cnt']
        total_courier_counts[nc] = total_courier_counts.get(nc, 0) + cnt
        year_courier_counts.setdefault(yr, {})
        year_courier_counts[yr][nc] = year_courier_counts[yr].get(nc, 0) + cnt
    top_couriers = sorted(total_courier_counts, key=total_courier_counts.get, reverse=True)[:5]
    courier_by_year = {
        year: {c: year_courier_counts.get(year, {}).get(c, 0) for c in top_couriers}
        for year in years_to_show
    }

    ontime_labels, ontime_pct = [], []
    for i in range(11, -1, -1):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        start, end = month_start_end(y, m)
        qs_m   = qs_all.filter(order_date__gte=start, order_date__lt=end,
                               shipped_at__isnull=False, shipping_deadline__isnull=False)
        total  = qs_m.count()
        on_time = qs_m.filter(shipped_at__lte=F('shipping_deadline')).count()
        ontime_labels.append(start.strftime('%b %Y'))
        ontime_pct.append(round(on_time / total * 100) if total else None)

    # ── Stock / product analytics (рядки продажів, у валюті Minerva) ──────────
    lines_range = SalesOrderLine.objects.filter(order__in=qs_all.filter(order_date__year__in=years_to_show))
    sku_money = rv.line_revenue(lines_range, by={'sku': 'product__sku', 'year': ExtractYear('order__order_date')},
                                rates=rates)
    sku_totals = {}
    for (sku, _yr), m in sku_money.items():
        sku_totals[sku] = sku_totals.get(sku, 0.0) + float(m.total)
    top_skus_raw = sorted(sku_totals, key=sku_totals.get, reverse=True)[:8]
    sku_year_rev = {
        sku: {year: float(sku_money[(sku, year)].total) if (sku, year) in sku_money else 0.0
              for year in years_to_show}
        for sku in top_skus_raw
    }

    cat_money = rv.line_revenue(lines_range, by={'cat': 'product__category',
                                                 'year': ExtractYear('order__order_date')}, rates=rates)
    cat_rows_raw = {}
    for (cat, yr), m in cat_money.items():
        cat_rows_raw.setdefault(yr, []).append({'product__category': cat, 'revenue': float(m.total)})
    for yr in cat_rows_raw:
        cat_rows_raw[yr] = sorted(cat_rows_raw[yr], key=lambda r: -r['revenue'])[:6]

    all_cats, seen = [], set()
    for year in years_to_show:
        for r in cat_rows_raw.get(year, []):
            c = r['product__category'] or 'Other'
            if c not in seen:
                all_cats.append(c)
                seen.add(c)
    all_cats = all_cats[:6]

    cat_by_year = {}
    for year in years_to_show:
        row = {c: 0.0 for c in all_cats}
        for r in cat_rows_raw.get(year, []):
            c = r['product__category'] or 'Other'
            if c in row:
                row[c] = float(r['revenue'] or 0)
        cat_by_year[year] = row

    ctx = admin.site.each_context(request)
    ctx.update({
        'filter_source':    filter_source,
        'all_sources':      all_sources,
        'current_year':     current_year,
        'year_from':        year_from,
        'year_to':          year_to,
        'available_years':  available_years,
        'years_to_show':    years_to_show,
        'cur_code':         home,
        'cur_sym':          rv.symbol(home),
        'cur_sym_js':       _js(rv.symbol(home)),
        'missing_note':     range_money.missing_text if range_money else '',
        'months_labels':    _js(months_labels),
        'yoy_datasets':     _js(yoy_datasets),
        'curr_rev_smooth':  _js(curr_rev_smooth),
        'smooth_year':      smooth_year,
        'mom_labels':       _js(mom_labels),
        'mom_growth':       _js(mom_growth),
        'quarters':         quarters,
        'curr_year_total':  curr_year_total,
        'yoy_growth':       yoy_growth,
        'yoy_vs_year':      yoy_vs_year,
        'yoy_ytd_label':    yoy_ytd_label,
        'best_month':       best_month,
        'worst_month':      worst_month,
        'avg_monthly':      avg_monthly,
        'top_couriers':     _js(top_couriers),
        'courier_by_year':  _js(courier_by_year),
        'ontime_labels':    _js(ontime_labels),
        'ontime_pct':       _js(ontime_pct),
        'top_skus_raw':     _js(top_skus_raw),
        'sku_year_rev':     _js(sku_year_rev),
        'all_cats':         _js(all_cats),
        'cat_by_year':      _js(cat_by_year),
        'years_json':       _js(years_to_show),
    })
    return render(request, 'dashboard/trends.html', ctx)
