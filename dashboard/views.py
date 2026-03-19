"""
dashboard/views.py — Minerva Dashboard v2
Виручка = SalesOrderLine.unit_price × qty (або total_price рядка якщо є)
"""
from datetime import timedelta
from decimal import Decimal
from django.db.models import (
    Sum, Count, F, Q, ExpressionWrapper, DecimalField, FloatField, Avg, Value
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

    # ── Виручка по місяцях через SalesOrderLine (узгоджено з KPI) ─────────────
    try:
        rev_by_month = list(
            SalesOrderLine.objects
            .filter(order__in=qs_period)
            .annotate(month=TruncMonth('order__order_date'))
            .values('month')
            .annotate(revenue=Sum(_line_rev_expr()), orders=Count('order', distinct=True))
            .order_by('month')
        )
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
        'period':          period,
        'period_days':     period_days,
        'period_label':    period_label,
        'date_from_str':   date_from_str,
        'date_to_str':     date_to_str,
        'filter_source':   filter_source,
        'filter_category': filter_category,
        'all_sources':     all_sources,
        'all_categories':  all_categories,
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
    """Trend analytics: YoY, MoM growth, quarterly, shipping + stock sections."""
    import calendar
    from datetime import date
    from sales.models import SalesOrder, SalesOrderLine

    today = date.today()
    current_year = today.year
    prev_year    = current_year - 1
    prev2_year   = current_year - 2

    filter_source = request.GET.get('source', '').strip()

    qs_all = SalesOrder.objects.filter(affects_stock=True)
    if filter_source:
        qs_all = qs_all.filter(source=filter_source)

    all_sources = list(
        SalesOrder.objects.values_list('source', flat=True).distinct().order_by('source'))

    rex = _line_rev_expr()  # Coalesce(total_price, unit_price*qty, 0)

    # ── Helper ────────────────────────────────────────────────────────────────
    def month_start_end(y, m):
        start = date(y, m, 1)
        end   = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        return start, end

    def monthly_rev_for_year(year):
        qs_y = qs_all.filter(order_date__year=year)
        rows = list(
            SalesOrderLine.objects
            .filter(order__in=qs_y)
            .annotate(month=TruncMonth('order__order_date'))
            .values('month')
            .annotate(revenue=Sum(rex), orders=Count('order', distinct=True))
            .order_by('month')
        )
        result = {}
        for r in rows:
            result[r['month'].month] = {
                'revenue': float(r['revenue'] or 0),
                'orders':  int(r['orders'] or 0),
            }
        return result

    # ── YoY data ──────────────────────────────────────────────────────────────
    curr_data  = monthly_rev_for_year(current_year)
    prev_data  = monthly_rev_for_year(prev_year)
    prev2_data = monthly_rev_for_year(prev2_year)

    months_labels = ['Січ', 'Лют', 'Бер', 'Кві', 'Тра', 'Чер',
                     'Лип', 'Сер', 'Вер', 'Жов', 'Лис', 'Гру']

    curr_rev   = [curr_data.get(m, {}).get('revenue', 0) for m in range(1, 13)]
    prev_rev   = [prev_data.get(m, {}).get('revenue', 0) for m in range(1, 13)]
    prev2_rev  = [prev2_data.get(m, {}).get('revenue', 0) for m in range(1, 13)]
    curr_ord   = [curr_data.get(m, {}).get('orders', 0) for m in range(1, 13)]
    prev_ord   = [prev_data.get(m, {}).get('orders', 0) for m in range(1, 13)]

    # 3-month moving average for current year
    curr_rev_smooth = []
    for i in range(12):
        window = [curr_rev[j] for j in range(max(0, i - 2), i + 1) if curr_rev[j] > 0]
        curr_rev_smooth.append(round(sum(window) / len(window), 1) if window else 0)

    # ── Rolling last 13 months → 12 MoM growth rates ─────────────────────────
    rolling_months = []
    for i in range(12, -1, -1):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        start, end = month_start_end(y, m)
        rev_val = float(
            SalesOrderLine.objects
            .filter(order__in=qs_all,
                    order__order_date__gte=start,
                    order__order_date__lt=end)
            .aggregate(t=Sum(rex))['t'] or 0
        )
        rolling_months.append({'label': start.strftime('%b %Y'), 'rev': rev_val})

    mom_labels = [rolling_months[i]['label'] for i in range(1, 13)]
    mom_growth = []
    for i in range(1, 13):
        prev_val = rolling_months[i - 1]['rev']
        curr_val = rolling_months[i]['rev']
        mom_growth.append(
            round((curr_val - prev_val) / prev_val * 100, 1) if prev_val else None
        )

    # ── Quarterly table ────────────────────────────────────────────────────────
    quarters = []
    for year in [prev2_year, prev_year, current_year]:
        for q in range(1, 5):
            qs_q = qs_all.filter(
                order_date__year=year,
                order_date__month__gte=(q - 1) * 3 + 1,
                order_date__month__lte=q * 3,
            )
            revenue = float(
                SalesOrderLine.objects.filter(order__in=qs_q)
                .aggregate(t=Sum(rex))['t'] or 0
            )
            quarters.append({'year': year, 'q': q,
                              'revenue': revenue, 'orders': qs_q.count()})

    q_by_key = {(r['year'], r['q']): r for r in quarters}
    for r in quarters:
        prev_r = q_by_key.get((r['year'] - 1, r['q']))
        if prev_r and prev_r['revenue']:
            r['delta_pct'] = round((r['revenue'] - prev_r['revenue']) / prev_r['revenue'] * 100, 1)
        else:
            r['delta_pct'] = None

    # ── Summary KPIs ───────────────────────────────────────────────────────────
    last_12 = [(rolling_months[i]['label'], rolling_months[i]['rev']) for i in range(1, 13)]
    non_zero = [(l, r) for l, r in last_12 if r > 0]
    best_month  = max(non_zero, key=lambda x: x[1]) if non_zero else None
    worst_month = min(non_zero, key=lambda x: x[1]) if non_zero else None
    avg_monthly = round(sum(r for _, r in non_zero) / len(non_zero)) if non_zero else 0

    curr_year_total = sum(curr_rev)
    prev_year_total = sum(prev_rev)
    yoy_growth = round((curr_year_total - prev_year_total) / prev_year_total * 100, 1) \
        if prev_year_total else None

    # ── Shipping analytics ─────────────────────────────────────────────────────
    # Courier breakdown per year (for stacked bar: top-5 couriers)
    top_couriers = list(
        qs_all.exclude(shipping_courier='')
        .values('shipping_courier')
        .annotate(cnt=Count('id'))
        .order_by('-cnt')[:5]
        .values_list('shipping_courier', flat=True)
    )
    courier_by_year = {}
    for year in [prev2_year, prev_year, current_year]:
        row = {}
        for c in top_couriers:
            row[c] = qs_all.filter(order_date__year=year, shipping_courier=c).count()
        courier_by_year[year] = row

    # On-time % by month (last 12 months)
    ontime_labels = []
    ontime_pct    = []
    for i in range(11, -1, -1):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        start, end = month_start_end(y, m)
        qs_m = qs_all.filter(
            order_date__gte=start, order_date__lt=end,
            shipped_at__isnull=False, shipping_deadline__isnull=False,
        )
        total = qs_m.count()
        on_time = qs_m.filter(shipped_at__lte=F('shipping_deadline')).count()
        ontime_labels.append(start.strftime('%b %Y'))
        ontime_pct.append(round(on_time / total * 100) if total else None)

    # ── Stock / product analytics ─────────────────────────────────────────────
    # Top-8 SKUs by revenue (all time, for this source filter)
    top_skus_raw = list(
        SalesOrderLine.objects
        .filter(order__in=qs_all)
        .values('product__sku')
        .annotate(revenue=Sum(rex))
        .order_by('-revenue')[:8]
        .values_list('product__sku', flat=True)
    )

    # Revenue per SKU per year (for grouped table)
    sku_year_rev = {}
    for sku in top_skus_raw:
        sku_year_rev[sku] = {}
        for year in [prev2_year, prev_year, current_year]:
            val = float(
                SalesOrderLine.objects
                .filter(order__in=qs_all.filter(order_date__year=year),
                        product__sku=sku)
                .aggregate(t=Sum(rex))['t'] or 0
            )
            sku_year_rev[sku][year] = val

    # Revenue by category per year
    cat_rows_raw = {}
    for year in [prev2_year, prev_year, current_year]:
        rows = list(
            SalesOrderLine.objects
            .filter(order__in=qs_all.filter(order_date__year=year))
            .values('product__category')
            .annotate(revenue=Sum(rex), orders=Count('order', distinct=True))
            .order_by('-revenue')[:6]
        )
        cat_rows_raw[year] = rows

    # Flatten top categories across years
    all_cats = []
    seen = set()
    for year in [prev2_year, prev_year, current_year]:
        for r in cat_rows_raw.get(year, []):
            c = r['product__category'] or 'Other'
            if c not in seen:
                all_cats.append(c)
                seen.add(c)
    all_cats = all_cats[:6]

    cat_by_year = {}
    for year in [prev2_year, prev_year, current_year]:
        row = {c: 0.0 for c in all_cats}
        for r in cat_rows_raw.get(year, []):
            c = r['product__category'] or 'Other'
            if c in row:
                row[c] = float(r['revenue'] or 0)
        cat_by_year[year] = row

    ctx = admin.site.each_context(request)
    ctx.update({
        # Revenue
        'filter_source':    filter_source,
        'all_sources':      all_sources,
        'current_year':     current_year,
        'prev_year':        prev_year,
        'prev2_year':       prev2_year,
        'months_labels':    json.dumps(months_labels),
        'curr_rev':         json.dumps(curr_rev),
        'prev_rev':         json.dumps(prev_rev),
        'prev2_rev':        json.dumps(prev2_rev),
        'curr_rev_smooth':  json.dumps(curr_rev_smooth),
        'curr_ord':         json.dumps(curr_ord),
        'prev_ord':         json.dumps(prev_ord),
        'mom_labels':       json.dumps(mom_labels),
        'mom_growth':       json.dumps(mom_growth),
        'quarters':         quarters,
        'curr_year_total':  curr_year_total,
        'prev_year_total':  prev_year_total,
        'yoy_growth':       yoy_growth,
        'best_month':       best_month,
        'worst_month':      worst_month,
        'avg_monthly':      avg_monthly,
        # Shipping
        'top_couriers':         json.dumps(top_couriers),
        'courier_by_year':      json.dumps(courier_by_year),
        'ontime_labels':        json.dumps(ontime_labels),
        'ontime_pct':           json.dumps(ontime_pct),
        # Stock / products
        'top_skus_raw':         json.dumps(top_skus_raw),
        'sku_year_rev':         json.dumps(sku_year_rev),
        'all_cats':             json.dumps(all_cats),
        'cat_by_year':          json.dumps(cat_by_year),
        'years_list':           json.dumps([prev2_year, prev_year, current_year]),
    })
    return render(request, 'dashboard/trends.html', ctx)
