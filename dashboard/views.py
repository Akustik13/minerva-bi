"""
dashboard/views.py — Minerva Dashboard v2
Виручка = SalesOrderLine.unit_price × qty (або total_price рядка якщо є)
"""
from datetime import timedelta
from decimal import Decimal
from django.db.models import (
    Sum, Count, F, Q, ExpressionWrapper, DecimalField, FloatField, Avg, Value, Min
)
from django.db.models.functions import TruncMonth, TruncDate, Coalesce
from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import admin
from django.utils import timezone
from django.utils.dateparse import parse_date
import json


_ZERO_DEC = Value(Decimal('0'), output_field=DecimalField(max_digits=18, decimal_places=2))

def _line_rev_expr():
    """Per-line revenue: total_price if set, else unit_price × qty, else 0.
    Handles old DigiKey-synced lines where total_price is NULL."""
    return Coalesce(
        'total_price',
        ExpressionWrapper(
            F('unit_price') * F('qty'),
            output_field=DecimalField(max_digits=18, decimal_places=2),
        ),
        _ZERO_DEC,
    )


# Revenue helpers — graceful fallback якщо поля ще не існують
def _safe_rev(qs, field='total_price'):
    try:
        r = qs.aggregate(t=Sum(_line_rev_expr()))['t']
        return float(r or 0)
    except Exception:
        return 0.0


@staff_member_required
def dashboard(request):
    now   = timezone.now()
    today = now.date()

    from sales.models import SalesOrder, SalesOrderLine

    # ── Фільтри ───────────────────────────────────────────────────────────────
    filter_source   = request.GET.get('source', '').strip()
    filter_category = request.GET.get('category', '').strip()

    # Дати: спочатку перевіряємо date_from/date_to, потім period
    date_from_str = request.GET.get('date_from', '').strip()
    date_to_str   = request.GET.get('date_to', '').strip()
    period        = request.GET.get('period', '').strip()

    date_from = parse_date(date_from_str) if date_from_str else None
    date_to   = parse_date(date_to_str)   if date_to_str   else None

    if date_from and date_to and date_from <= date_to:
        period_days = (date_to - date_from).days or 1
        period_label = f"{date_from_str} – {date_to_str}"
    else:
        # Fallback до пресету
        try:    period_days = int(period) if period else 90
        except: period_days = 90
        date_from   = (now - timedelta(days=period_days)).date()
        date_to     = today
        date_from_str = str(date_from)
        date_to_str   = str(date_to)
        period_label  = f"{period_days} днів"

    # Попередній аналогічний період для порівняння
    prev_date_to   = date_from
    prev_date_from = date_from - timedelta(days=period_days)

    # ── Базові queryset-и ──────────────────────────────────────────────────────
    qs_all = SalesOrder.objects.filter(affects_stock=True)
    if filter_source:
        qs_all = qs_all.filter(source=filter_source)

    qs_period = qs_all.filter(
        order_date__isnull=False,
        order_date__gte=date_from,
        order_date__lt=date_to + timedelta(days=1),  # exclusive end → symmetric with prev
    )
    qs_lines = SalesOrderLine.objects.filter(order__in=qs_period)
    if filter_category:
        qs_lines = qs_lines.filter(product__category=filter_category)

    # Попередній аналогічний період (однакова довжина)
    qs_prev = qs_all.filter(
        order_date__gte=prev_date_from,
        order_date__lt=prev_date_to,
    )
    qs_prev_lines = SalesOrderLine.objects.filter(order__in=qs_prev)
    if filter_category:
        qs_prev_lines = qs_prev_lines.filter(product__category=filter_category)

    # Доступні значення для dropdown-ів
    all_sources    = list(
        SalesOrder.objects.values_list('source', flat=True).distinct().order_by('source'))
    all_categories = list(
        SalesOrderLine.objects.values_list('product__category', flat=True)
        .distinct().exclude(product__category='').order_by('product__category'))

    def rev(qs):
        return _safe_rev(qs, 'total_price')

    def pct(curr, prev):
        if not prev: return None
        return round((curr - prev) / prev * 100, 1)

    try:
        rev_curr = rev(qs_lines)
        rev_prev = rev(qs_prev_lines)
    except Exception:
        rev_curr = 0.0
        rev_prev = 0.0
    ord_curr  = qs_period.count()
    ord_prev  = qs_prev.count()
    units_curr = float(qs_lines.aggregate(t=Sum('qty'))['t'] or 0)
    units_prev = float(qs_prev_lines.aggregate(t=Sum('qty'))['t'] or 0)
    avg_curr  = rev_curr / ord_curr if ord_curr else 0
    avg_prev  = rev_prev / ord_prev if ord_prev else 0
    unshipped_cnt = qs_all.filter(shipped_at__isnull=True).count()

    kpi = {
        'revenue':   {'val': rev_curr,   'fmt': f'€{rev_curr:,.0f}' if rev_curr else '—',   'change': pct(rev_curr, rev_prev)},
        'orders':    {'val': ord_curr,   'fmt': str(ord_curr),          'change': pct(ord_curr, ord_prev)},
        'units':     {'val': units_curr, 'fmt': f'{units_curr:,.0f}',  'change': pct(units_curr, units_prev)},
        'avg_order': {'val': avg_curr,   'fmt': f'€{avg_curr:,.0f}',   'change': pct(avg_curr, avg_prev)},
        'unshipped': {'val': unshipped_cnt, 'fmt': str(unshipped_cnt), 'change': None},
    }

    # ── Виручка по місяцях (two-query merge for reliability) ─────────────────
    # Q1: line-level revenue per month — uses qs_lines (respects category filter)
    # Q2: order-level total_price per month (fallback for DigiKey-synced orders
    #     whose SalesOrderLine prices are NULL)
    try:
        _line_m = {
            r['month']: float(r['rev'] or 0)
            for r in (
                qs_lines
                .annotate(month=TruncMonth('order__order_date'))
                .values('month')
                .annotate(rev=Sum('total_price'))
                .order_by('month')
            )
        }
        _ord_m = {
            r['month']: {'rev': float(r['rev'] or 0), 'cnt': int(r['cnt'])}
            for r in (
                qs_period
                .annotate(month=TruncMonth('order_date'))
                .values('month')
                .annotate(rev=Sum('total_price'), cnt=Count('id'))
                .order_by('month')
            )
        }
        rev_by_month = []
        for m in sorted(set(list(_line_m) + list(_ord_m))):
            line_rev = _line_m.get(m, 0)
            ord_data = _ord_m.get(m, {'rev': 0, 'cnt': 0})
            rev_by_month.append({
                'month':   m,
                'revenue': line_rev if line_rev > 0 else ord_data['rev'],
                'orders':  ord_data['cnt'],
            })
    except Exception:
        rev_by_month = []

    chart_months   = [str(r['month'])[:7] for r in rev_by_month]
    chart_rev      = [float(r['revenue'] or 0) for r in rev_by_month]
    chart_orders_m = [int(r['orders'] or 0) for r in rev_by_month]

    # ── Замовлення по днях ─────────────────────────────────────────────────────
    orders_by_day = list(
        qs_period
        .annotate(day=TruncDate('order_date'))
        .values('day')
        .annotate(cnt=Count('id'))
        .order_by('day')
    )
    chart_days    = [str(r['day']) for r in orders_by_day]
    chart_day_ord = [int(r['cnt'] or 0) for r in orders_by_day]

    # ── Топ SKU по виручці ────────────────────────────────────────────────────
    top_skus = list(
        qs_lines
        .values('product__sku', 'product__name', 'product__category')
        .annotate(
            qty_total=Sum('qty'),
            orders_cnt=Count('order', distinct=True),
            revenue=Sum('total_price'),
        )
        .order_by('-revenue')[:15]
    )

    # ── Топ клієнти по виручці ─────────────────────────────────────────────────
    top_clients = list(
        qs_period
        .exclude(client='')
        .values('client', 'shipping_region')
        .annotate(
            orders=Count('id'),
            units=Sum('lines__qty'),
            revenue=Sum('lines__total_price'),
        )
        .order_by('-revenue')[:10]
    )

    # ── Географія ──────────────────────────────────────────────────────────────
    by_region = list(
        qs_period
        .exclude(shipping_region='')
        .values('shipping_region')
        .annotate(orders=Count('id'), revenue=Sum('lines__total_price'))
        .order_by('-orders')[:20]
    )
    max_region_orders = by_region[0]['orders'] if by_region else 1
    for r in by_region:
        r['pct'] = round(r['orders'] / max_region_orders * 100)

    # ── Статистика доставки ────────────────────────────────────────────────────
    by_courier = list(
        qs_period
        .exclude(shipping_courier='')
        .values('shipping_courier')
        .annotate(
            orders=Count('id'),
            avg_cost=Avg('shipping_cost'),
            total_cost=Sum('shipping_cost'),
        )
        .order_by('-orders')
    )

    shipping_by_country = list(
        qs_period
        .exclude(shipping_region='')
        .values('shipping_region')
        .annotate(
            orders=Count('id'),
            avg_cost=Avg('shipping_cost'),
            total_cost=Sum('shipping_cost'),
        )
        .order_by('-orders')[:20]
    )

    # Топ-2 кур'єри по кожній країні
    courier_country_raw = list(
        qs_period
        .exclude(shipping_courier='')
        .exclude(shipping_region='')
        .values('shipping_region', 'shipping_courier')
        .annotate(cnt=Count('id'))
        .order_by('shipping_region', '-cnt')
    )
    courier_by_country = {}
    for item in courier_country_raw:
        reg = item['shipping_region']
        if reg not in courier_by_country:
            courier_by_country[reg] = []
        if len(courier_by_country[reg]) < 2:
            courier_by_country[reg].append(item['shipping_courier'])

    for row in shipping_by_country:
        row['couriers'] = ', '.join(courier_by_country.get(row['shipping_region'], []))

    # Дані для chart кур'єрів
    chart_courier_labels = [r['shipping_courier'] for r in by_courier]
    chart_courier_orders = [r['orders'] for r in by_courier]

    chart_geo_labels = [r['shipping_region'] for r in by_region[:8]]
    chart_geo_orders = [int(r['orders']) for r in by_region[:8]]

    # ── По категоріях ──────────────────────────────────────────────────────────
    by_category = list(
        qs_lines
        .values('product__category')
        .annotate(qty_total=Sum('qty'), orders_cnt=Count('order', distinct=True))
        .order_by('-qty_total')
    )
    chart_cat_labels = [r['product__category'] or 'Other' for r in by_category]
    chart_cat_qty    = [float(r['qty_total'] or 0) for r in by_category]

    # ── Джерела ────────────────────────────────────────────────────────────────
    by_source = list(
        qs_period
        .values('source')
        .annotate(orders=Count('id'))
        .order_by('-orders')
    )
    max_src = by_source[0]['orders'] if by_source else 1
    for s in by_source:
        s['pct'] = round(s['orders'] / max_src * 100)

    # ── Не відправлені ─────────────────────────────────────────────────────────
    unshipped_list = list(
        qs_all.filter(shipped_at__isnull=True)
        .order_by('order_date')
        .values('id','order_number','source','order_date',
                'client','shipping_region')[:30]
    )
    for o in unshipped_list:
        d = o['order_date']
        if d:
            if hasattr(d, 'date'): d = d.date()
            o['days_waiting'] = (today - d).days
        else:
            o['days_waiting'] = 0

    # ── Критичний склад ────────────────────────────────────────────────────────
    critical_stock = []
    try:
        from inventory.models import Product, InventoryTransaction
        since3m = now - timedelta(days=90)
        for p in Product.objects.filter(is_active=True):
            stock = float(InventoryTransaction.objects.filter(
                product=p).aggregate(t=Sum('qty'))['t'] or 0)
            sold = float(SalesOrderLine.objects.filter(
                product=p, order__order_date__gte=since3m.date()
            ).aggregate(t=Sum('qty'))['t'] or 0)
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
    try:
        qs_with_dl = qs_period.filter(
            shipped_at__isnull=False, shipping_deadline__isnull=False)
        total_with_deadline = qs_with_dl.count()
        on_time_cnt         = qs_with_dl.filter(shipped_at__lte=F('shipping_deadline')).count()
        overdue_shipped_cnt = qs_with_dl.filter(shipped_at__gt=F('shipping_deadline')).count()
        overdue_pending_cnt = qs_all.filter(
            shipped_at__isnull=True,
            shipping_deadline__isnull=False,
            shipping_deadline__lt=today,
        ).count()
        on_time_pct = round(on_time_cnt / total_with_deadline * 100) if total_with_deadline else None

        ship_pairs = list(
            qs_period.filter(shipped_at__isnull=False, order_date__isnull=False)
            .values_list('order_date', 'shipped_at')
        )
        ship_days = [(s - o).days for o, s in ship_pairs if s and o and s >= o]
        avg_ship_days = round(sum(ship_days) / len(ship_days), 1) if ship_days else None

        del_pairs = list(
            qs_period.filter(delivered_at__isnull=False, shipped_at__isnull=False)
            .values_list('shipped_at', 'delivered_at')
        )
        del_days = []
        for s, d in del_pairs:
            if s and d:
                d_date = d.date() if hasattr(d, 'date') else d
                s_date = s.date() if hasattr(s, 'date') else s
                if d_date >= s_date:
                    del_days.append((d_date - s_date).days)
        avg_del_days = round(sum(del_days) / len(del_days), 1) if del_days else None
    except Exception:
        total_with_deadline = 0
        on_time_cnt = overdue_shipped_cnt = overdue_pending_cnt = 0
        on_time_pct = avg_ship_days = avg_del_days = None

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
        .values('product__sku', 'product__category')
        .annotate(qty_total=Sum('qty'), orders_cnt=Count('order', distinct=True))
        .order_by('-orders_cnt')[:8]
    )

    ctx = admin.site.each_context(request)
    ctx.update({
        # Фільтри
        'period':               period,
        'period_days':          period_days,
        'period_label':         period_label,
        'date_from_str':        date_from_str,
        'date_to_str':          date_to_str,
        'prev_date_from_str':   str(prev_date_from),
        'prev_date_to_str':     str(prev_date_to),
        'filter_source':        filter_source,
        'filter_category':      filter_category,
        'all_sources':          all_sources,
        'all_categories':       all_categories,
        # KPI
        'kpi': kpi,
        # Графіки
        'chart_months':     json.dumps(chart_months),
        'chart_rev':        json.dumps(chart_rev),
        'chart_orders_m':   json.dumps(chart_orders_m),
        'chart_days':       json.dumps(chart_days),
        'chart_day_ord':    json.dumps(chart_day_ord),
        'chart_geo_labels': json.dumps(chart_geo_labels),
        'chart_geo_orders': json.dumps(chart_geo_orders),
        'chart_cat_labels': json.dumps(chart_cat_labels),
        'chart_cat_qty':    json.dumps(chart_cat_qty),
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
        'chart_courier_labels': json.dumps(chart_courier_labels),
        'chart_courier_orders': json.dumps(chart_courier_orders),
    })
    return render(request, 'dashboard/dashboard.html', ctx)


@staff_member_required
def trends_view(request):
    """Trend analytics: YoY, MoM growth, quarterly, shipping + stock sections.
    Supports dynamic year range via year_from / year_to GET params.
    Fixes: future quarters show — not ▼-100%, YoY KPI compares YTD not full year."""
    from datetime import date
    from sales.models import SalesOrder, SalesOrderLine

    today = date.today()
    current_year    = today.year
    current_quarter = (today.month - 1) // 3 + 1

    filter_source = request.GET.get('source', '').strip()

    qs_all = SalesOrder.objects.filter(affects_stock=True)
    if filter_source:
        qs_all = qs_all.filter(source=filter_source)

    all_sources = list(
        SalesOrder.objects.values_list('source', flat=True).distinct().order_by('source'))

    # ── Year range filter ─────────────────────────────────────────────────────
    min_row = SalesOrder.objects.filter(
        affects_stock=True, order_date__isnull=False
    ).aggregate(y=Min('order_date__year'))
    min_db_year     = min_row['y'] or (current_year - 5)
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

    rex = _line_rev_expr()

    # ── Helpers ────────────────────────────────────────────────────────────────
    def month_start_end(y, m):
        start = date(y, m, 1)
        end   = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        return start, end

    def _qs_rev(qs_orders):
        line_rev = float(
            SalesOrderLine.objects.filter(order__in=qs_orders)
            .aggregate(t=Sum(rex))['t'] or 0
        )
        if line_rev > 0:
            return line_rev
        return float(qs_orders.aggregate(t=Sum('total_price'))['t'] or 0)

    def monthly_rev_for_year(year):
        qs_y = qs_all.filter(order_date__year=year)
        line_rows = list(
            SalesOrderLine.objects.filter(order__in=qs_y)
            .annotate(month=TruncMonth('order__order_date'))
            .values('month').annotate(rev=Sum(rex)).order_by('month')
        )
        line_by_m = {r['month'].month: float(r['rev'] or 0) for r in line_rows}
        order_rows = list(
            qs_y.annotate(month=TruncMonth('order_date'))
            .values('month')
            .annotate(order_rev=Sum('total_price'), orders=Count('id'))
            .order_by('month')
        )
        order_by_m = {
            r['month'].month: {'rev': float(r['order_rev'] or 0), 'orders': int(r['orders'])}
            for r in order_rows
        }
        result = {}
        for m in set(list(line_by_m) + list(order_by_m)):
            line_rev = line_by_m.get(m, 0)
            ord_data = order_by_m.get(m, {'rev': 0, 'orders': 0})
            result[m] = {
                'revenue': line_rev if line_rev > 0 else ord_data['rev'],
                'orders':  ord_data['orders'],
            }
        return result

    # ── Per-year data ──────────────────────────────────────────────────────────
    all_years_data = {y: monthly_rev_for_year(y) for y in years_to_show}

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
                qs_q = qs_all.filter(
                    order_date__year=year,
                    order_date__month__gte=(q - 1) * 3 + 1,
                    order_date__month__lte=q * 3,
                )
                rev_q = _qs_rev(qs_q)
                ord_q = qs_q.count()
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
            r = all_years_data[yr].get(m, {})
            rev = r.get('revenue', 0)
            if rev > 0:
                range_month_revs.append((f"{months_labels[m - 1]} {yr}", rev))
    best_month  = max(range_month_revs, key=lambda x: x[1]) if range_month_revs else None
    worst_month = min(range_month_revs, key=lambda x: x[1]) if range_month_revs else None
    avg_monthly = round(sum(r for _, r in range_month_revs) / len(range_month_revs)) if range_month_revs else 0

    # KPI: use year_to (most recent selected year), not always current_year
    # This way selecting 2021–2024 shows 2024 data, not 2026
    latest_yr_data  = all_years_data[year_to]
    curr_year_total = sum(latest_yr_data.get(m, {}).get('revenue', 0) for m in range(1, 13))

    # YoY KPI: compare year_to YTD vs year_to-1 YTD
    # If year_to == current_year, limit to months already elapsed; else compare full year
    ytd_month = today.month if year_to == current_year else 12
    curr_ytd  = sum(latest_yr_data.get(m, {}).get('revenue', 0) for m in range(1, ytd_month + 1))
    if len(years_to_show) >= 2:
        yoy_vs_year = years_to_show[-2]
        prev_ytd    = sum(
            all_years_data[yoy_vs_year].get(m, {}).get('revenue', 0)
            for m in range(1, ytd_month + 1)
        )
        yoy_growth = round((curr_ytd - prev_ytd) / prev_ytd * 100, 1) if prev_ytd else None
    else:
        yoy_vs_year = year_to - 1
        yoy_growth  = None
    yoy_ytd_label = months_labels[ytd_month - 1] if year_to == current_year else 'Гру'

    # ── Shipping analytics ─────────────────────────────────────────────────────
    # Normalize courier names in Python to merge "dhl"/"DHL"/"DHL Express" → "DHL"
    from sales.utils import normalize_courier as _nc
    raw_courier_rows = list(
        qs_all.exclude(shipping_courier='')
        .filter(order_date__year__in=years_to_show)
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

    # ── Stock / product analytics ─────────────────────────────────────────────
    top_skus_raw = list(
        SalesOrderLine.objects.filter(order__in=qs_all)
        .values('product__sku').annotate(revenue=Sum(rex)).order_by('-revenue')[:8]
        .values_list('product__sku', flat=True)
    )
    sku_year_rev = {}
    for sku in top_skus_raw:
        sku_year_rev[sku] = {
            year: float(
                SalesOrderLine.objects
                .filter(order__in=qs_all.filter(order_date__year=year), product__sku=sku)
                .aggregate(t=Sum(rex))['t'] or 0
            )
            for year in years_to_show
        }

    cat_rows_raw = {}
    for year in years_to_show:
        cat_rows_raw[year] = list(
            SalesOrderLine.objects.filter(order__in=qs_all.filter(order_date__year=year))
            .values('product__category')
            .annotate(revenue=Sum(rex), orders=Count('order', distinct=True))
            .order_by('-revenue')[:6]
        )

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
        'months_labels':    json.dumps(months_labels),
        'yoy_datasets':     json.dumps(yoy_datasets),
        'curr_rev_smooth':  json.dumps(curr_rev_smooth),
        'smooth_year':      smooth_year,
        'mom_labels':       json.dumps(mom_labels),
        'mom_growth':       json.dumps(mom_growth),
        'quarters':         quarters,
        'curr_year_total':  curr_year_total,
        'yoy_growth':       yoy_growth,
        'yoy_vs_year':      yoy_vs_year,
        'yoy_ytd_label':    yoy_ytd_label,
        'best_month':       best_month,
        'worst_month':      worst_month,
        'avg_monthly':      avg_monthly,
        'top_couriers':     json.dumps(top_couriers),
        'courier_by_year':  json.dumps(courier_by_year),
        'ontime_labels':    json.dumps(ontime_labels),
        'ontime_pct':       json.dumps(ontime_pct),
        'top_skus_raw':     json.dumps(top_skus_raw),
        'sku_year_rev':     json.dumps(sku_year_rev),
        'all_cats':         json.dumps(all_cats),
        'cat_by_year':      json.dumps(cat_by_year),
        'years_json':       json.dumps(years_to_show),
    })
    return render(request, 'dashboard/trends.html', ctx)
