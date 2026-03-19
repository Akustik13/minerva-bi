"""
dashboard/views.py — Minerva Dashboard v2
Виручка = SalesOrderLine.unit_price × qty (або total_price рядка якщо є)
"""
from datetime import timedelta
from django.db.models import (
    Sum, Count, F, Q, ExpressionWrapper, DecimalField, FloatField, Avg
)
from django.db.models.functions import TruncMonth, TruncDate
from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import admin
from django.utils import timezone
from django.utils.dateparse import parse_date
import json


# Revenue helpers — graceful fallback якщо поля ще не існують
def _safe_rev(qs, field='total_price'):
    try:
        r = qs.aggregate(t=Sum(field))['t']
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
            .annotate(revenue=Sum('total_price'), orders=Count('order', distinct=True))
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
