"""
dashboard/signals_views.py  — Модуль сигналів дій Minerva

Підключити в dashboard/urls.py:
    from .signals_views import signals_page
    path("signals/", signals_page, name="signals"),

Підключити в tabele/urls.py вже є через:
    path("dashboard/", include("dashboard.urls")),
"""
from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import admin
from django.utils import timezone
from django.db.models import Sum, Count, Max, Q
from datetime import timedelta, date


def _collect_signals():
    """Збирає всі сигнали з усіх модулів. Повертає список dict."""
    signals = []
    now = timezone.now()
    today = now.date()

    # ── 1. СКЛАД: товари що закінчуються ───────────────────────────────────
    try:
        from inventory.models import Product, InventoryTransaction, PurchaseOrderLine, PurchaseOrder
        from sales.models import SalesOrderLine

        for product in Product.objects.filter(is_active=True):
            # Поточний залишок
            stock_result = InventoryTransaction.objects.filter(
                product=product).aggregate(total=Sum('qty'))
            stock = float(stock_result['total'] or 0)

            # Середні продажі за 3 місяці
            since_3m = now - timedelta(days=90)
            sold = SalesOrderLine.objects.filter(
                product=product,
                order__order_date__gte=since_3m,
            ).aggregate(total=Sum('qty'))
            monthly = float(sold['total'] or 0) / 3

            if monthly <= 0:
                continue

            months_left = stock / monthly if monthly > 0 else 999

            if months_left <= 0.5:
                signals.append({
                    'type': 'critical',
                    'module': 'Склад',
                    'icon': '🔥',
                    'title': f'КРИТИЧНО: {product.sku}',
                    'body': f'Залишок {int(stock)} шт. — вистачить менше 2 тижнів '
                            f'(продажі ~{round(monthly,1)}/міс)',
                    'action': f'Замовити ~{int(monthly*3*1.2 - stock)} шт.',
                    'action_url': f'/admin/inventory/purchaseorder/add/?product={product.pk}',
                    'age_days': 0,
                })
            elif months_left <= 1.5:
                signals.append({
                    'type': 'warning',
                    'module': 'Склад',
                    'icon': '⚠️',
                    'title': f'Мало запасів: {product.sku}',
                    'body': f'Залишок {int(stock)} шт. — вистачить {round(months_left,1)} міс.',
                    'action': f'Замовити ~{int(monthly*3*1.2 - stock)} шт.',
                    'action_url': f'/admin/inventory/purchaseorder/add/?product={product.pk}',
                    'age_days': 0,
                })
    except Exception as e:
        signals.append({
            'type': 'info', 'module': 'Система', 'icon': '⚙️',
            'title': 'Помилка аналізу складу',
            'body': str(e), 'action': '', 'action_url': '', 'age_days': 0,
        })

    # ── 2. ПРОДАЖІ: замовлення не відправлені давно ─────────────────────────
    try:
        from sales.models import SalesOrder

        # Не відправлені більше 7 днів
        overdue_7 = SalesOrder.objects.filter(
            shipped_at__isnull=True,
            order_date__isnull=False,
            order_date__lt=today - timedelta(days=7),
        ).order_by('order_date')[:10]

        for order in overdue_7:
            days_waiting = (today - order.order_date).days
            signals.append({
                'type': 'critical' if days_waiting > 14 else 'warning',
                'module': 'Продажі',
                'icon': '📦' if days_waiting <= 14 else '🚨',
                'title': f'Не відправлено {days_waiting} днів: #{order.order_number}',
                'body': f'Клієнт: {order.client or "—"} | '
                        f'Дата: {order.order_date} | '
                        f'Країна: {order.shipping_region or "—"}',
                'action': 'Відкрити замовлення',
                'action_url': f'/admin/sales/salesorder/{order.pk}/change/',
                'age_days': days_waiting,
            })

        # Замовлення без tracking більше 3 днів після дати
        no_tracking = SalesOrder.objects.filter(
            shipped_at__isnull=False,
            tracking_number='',
            shipped_at__lt=today - timedelta(days=3),
        ).order_by('-shipped_at')[:5]

        for order in no_tracking:
            signals.append({
                'type': 'info',
                'module': 'Продажі',
                'icon': '🔍',
                'title': f'Немає трекінгу: #{order.order_number}',
                'body': f'Відправлено {order.shipped_at} але трекінг не вказано. '
                        f'Клієнт: {order.client or "—"}',
                'action': 'Додати трекінг',
                'action_url': f'/admin/sales/salesorder/{order.pk}/change/',
                'age_days': (today - order.shipped_at).days if order.shipped_at else 0,
            })

    except Exception as e:
        signals.append({
            'type': 'info', 'module': 'Система', 'icon': '⚙️',
            'title': 'Помилка аналізу продажів',
            'body': str(e), 'action': '', 'action_url': '', 'age_days': 0,
        })

    # ── 3. CRM: клієнти що замовчали ────────────────────────────────────────
    try:
        from crm.models import Customer

        for customer in Customer.objects.all():
            last_order = customer.sales_orders.order_by('-order_date').first()
            if not last_order or not last_order.order_date:
                continue

            last_date = last_order.order_date
            if hasattr(last_date, 'date'):
                last_date = last_date.date()

            days_silent = (today - last_date).days
            order_count = customer.sales_orders.count()

            # Активний клієнт (2+ замовлень) мовчить 90+ днів
            if order_count >= 2 and days_silent >= 90:
                revenue = customer.sales_orders.aggregate(
                    t=Sum('lines__total_price'))['t'] or 0
                signals.append({
                    'type': 'warning' if days_silent < 180 else 'info',
                    'module': 'CRM',
                    'icon': '😴' if days_silent < 180 else '💤',
                    'title': f'Клієнт мовчить {days_silent} днів: {customer.name}',
                    'body': f'Замовлень: {order_count} | '
                            f'Виручка: €{float(revenue):.0f} | '
                            f'Останнє: {last_date}',
                    'action': 'Написати клієнту',
                    'action_url': f'/admin/crm/customer/{customer.pk}/change/',
                    'age_days': days_silent,
                })

    except Exception as e:
        signals.append({
            'type': 'info', 'module': 'Система', 'icon': '⚙️',
            'title': 'Помилка аналізу CRM',
            'body': str(e), 'action': '', 'action_url': '', 'age_days': 0,
        })

    # ── 4. ЗАКУПІВЛІ: PO зависли ─────────────────────────────────────────────
    try:
        from inventory.models import PurchaseOrder

        stale_po = PurchaseOrder.objects.filter(
            status__in=['ordered', 'partial'],
            order_date__lt=today - timedelta(days=30),
        ).select_related('supplier')

        for po in stale_po:
            days_old = (today - po.order_date).days
            signals.append({
                'type': 'warning',
                'module': 'Закупівлі',
                'icon': '📋',
                'title': f'PO зависло {days_old} днів: {po.code}',
                'body': f'Постачальник: {po.supplier.name} | '
                        f'Статус: {po.get_status_display()} | '
                        f'Відкрито: {po.order_date}',
                'action': 'Перевірити статус',
                'action_url': f'/admin/inventory/purchaseorder/{po.pk}/change/',
                'age_days': days_old,
            })

        # Draft PO старше 7 днів — нагадування підтвердити
        old_drafts = PurchaseOrder.objects.filter(
            status='draft',
            order_date__lt=today - timedelta(days=7),
        ).select_related('supplier')

        for po in old_drafts:
            days_old = (today - po.order_date).days
            signals.append({
                'type': 'info',
                'module': 'Закупівлі',
                'icon': '📝',
                'title': f'Чернетка PO {days_old} днів: {po.code}',
                'body': f'Постачальник: {po.supplier.name} — підтвердіть або видаліть',
                'action': 'Відкрити PO',
                'action_url': f'/admin/inventory/purchaseorder/{po.pk}/change/',
                'age_days': days_old,
            })

    except Exception as e:
        pass

    # ── Сортування: критичні → warnings → info, всередині по age_days ───────
    priority = {'critical': 0, 'warning': 1, 'info': 2}
    signals.sort(key=lambda s: (priority.get(s['type'], 9), -s.get('age_days', 0)))

    return signals


@staff_member_required
def signals_page(request):
    signals = _collect_signals()

    counts = {
        'critical': sum(1 for s in signals if s['type'] == 'critical'),
        'warning':  sum(1 for s in signals if s['type'] == 'warning'),
        'info':     sum(1 for s in signals if s['type'] == 'info'),
        'total':    len(signals),
    }

    ctx = admin.site.each_context(request)
    ctx.update({
        'signals': signals,
        'counts':  counts,
        'modules': sorted(set(s['module'] for s in signals)),
    })
    return render(request, 'dashboard/signals.html', ctx)
