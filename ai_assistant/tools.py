from django.db import models as django_models

# ── Tool schemas for Anthropic API ────────────────────────────────────────────

ALL_TOOLS = [
    {
        "name": "get_system_overview",
        "description": "Загальна статистика системи: клієнти, замовлення, виручка, склад.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_inventory_status",
        "description": "Стан складу. Пошук товарів, фільтр по низьким залишкам.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search":        {"type": "string",  "description": "Пошук по назві або SKU"},
                "low_stock_only":{"type": "boolean", "description": "Тільки товари з низьким залишком"},
            },
            "required": [],
        },
    },
    {
        "name": "get_recent_orders",
        "description": "Останні замовлення. Фільтр по клієнту, статусу, кількості.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string",  "description": "Ім'я клієнта (частково)"},
                "status":        {"type": "string",  "description": "Статус: all, pending, completed, shipped, cancelled"},
                "limit":         {"type": "integer", "description": "Кількість результатів (макс 30)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_customer_info",
        "description": "Інформація про клієнта: RFM сегмент, email, сегмент.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string", "description": "Ім'я клієнта (частково)"},
            },
            "required": ["customer_name"],
        },
    },
    {
        "name": "get_sales_analytics",
        "description": "Аналітика продажів: виручка, ТОП товари/клієнти за період.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "description": "today | week | month | quarter | year",
                },
                "metric": {
                    "type": "string",
                    "description": "revenue | top_products | top_customers",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_financial_overview",
        "description": "Фінансовий огляд: прострочені замовлення, дедлайни.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_email_strategy",
        "description": "Отримати дані клієнта для формування стратегії комунікації.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string", "description": "Ім'я клієнта"},
            },
            "required": ["customer_name"],
        },
    },
    {
        "name": "compose_email",
        "description": "Скласти лист клієнту (без відправки). Повертає тему і текст.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string",  "description": "Ім'я клієнта"},
                "purpose":       {"type": "string",  "description": "follow_up | reactivation | promotion | reminder"},
                "language":      {"type": "string",  "description": "uk | de | en"},
            },
            "required": ["customer_name"],
        },
    },
    {
        "name": "send_email",
        "description": "Відправити email. Потребує підтвердження (confirm=true).",
        "input_schema": {
            "type": "object",
            "properties": {
                "to_email": {"type": "string",  "description": "Email адреса"},
                "subject":  {"type": "string",  "description": "Тема листа"},
                "body":     {"type": "string",  "description": "Текст листа"},
                "confirm":  {"type": "boolean", "description": "Підтвердження відправки"},
            },
            "required": ["to_email", "subject", "body"],
        },
    },
    {
        "name": "create_order",
        "description": "Створити замовлення. Потребує підтвердження (confirm=true).",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string", "description": "Ім'я клієнта"},
                "items":         {"type": "array",  "description": "Список {sku, qty, price}"},
                "confirm":       {"type": "boolean","description": "Підтвердження створення"},
            },
            "required": ["customer_name", "items"],
        },
    },
    {
        "name": "update_inventory",
        "description": "Оновити кількість товару на складі. Потребує confirm=true.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sku":      {"type": "string",  "description": "SKU товару"},
                "quantity": {"type": "number",  "description": "Нова кількість"},
                "reason":   {"type": "string",  "description": "Причина коригування"},
                "confirm":  {"type": "boolean", "description": "Підтвердження"},
            },
            "required": ["sku", "quantity"],
        },
    },
    {
        "name": "get_audit_log",
        "description": "Журнал аудиту системи за останні N днів.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "За скільки днів (макс 30)"},
                "user": {"type": "string",  "description": "Фільтр по імені юзера"},
            },
            "required": [],
        },
    },
]


# ── Tool handlers ─────────────────────────────────────────────────────────────

def execute_tool(tool_name: str, tool_input: dict, profile=None) -> dict:
    try:
        return _TOOL_HANDLERS[tool_name](tool_input, profile)
    except KeyError:
        return {"error": f"Tool '{tool_name}' не знайдено"}
    except Exception as e:
        return {"error": str(e)}


def _get_system_overview(inp, profile):
    from django.utils import timezone
    from django.db.models import Sum
    from sales.models import SalesOrder, SalesOrderLine
    from crm.models import Customer
    from inventory.models import Product

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    revenue = (SalesOrderLine.objects.filter(
        order__order_date__gte=month_start,
        order__affects_stock=True,
    ).aggregate(total=Sum('total_price'))['total'] or 0)

    low_stock = 0
    try:
        low_stock = Product.objects.filter(
            quantity__lte=django_models.F('reorder_point')
        ).count()
    except Exception:
        pass

    return {
        "customers_total":    Customer.objects.count(),
        "orders_this_month":  SalesOrder.objects.filter(order_date__gte=month_start).count(),
        "revenue_this_month": float(revenue),
        "products_total":     Product.objects.count(),
        "low_stock_count":    low_stock,
    }


def _get_inventory_status(inp, profile):
    from django.db.models import Q
    from inventory.models import Product

    qs = Product.objects.all()
    if inp.get('search'):
        qs = qs.filter(
            Q(name__icontains=inp['search']) | Q(sku__icontains=inp['search'])
        )
    if inp.get('low_stock_only'):
        try:
            qs = qs.filter(quantity__lte=django_models.F('reorder_point'))
        except Exception:
            pass

    items = []
    for p in qs[:20]:
        item = {"name": str(p), "sku": getattr(p, 'sku', '—')}
        for field in ('quantity', 'reorder_point', 'purchase_price', 'sale_price'):
            val = getattr(p, field, None)
            if val is not None:
                item[field] = float(val)
        items.append(item)
    return {"products": items, "total_shown": len(items)}


def _get_recent_orders(inp, profile):
    from django.db.models import Sum
    from sales.models import SalesOrder

    qs = SalesOrder.objects.select_related('customer').order_by('-order_date')
    if inp.get('customer_name'):
        qs = qs.filter(customer__name__icontains=inp['customer_name'])
    status = inp.get('status', 'all')
    if status != 'all':
        qs = qs.filter(status=status)
    limit = min(int(inp.get('limit', 10)), 30)

    orders = []
    for o in qs[:limit]:
        total = 0
        try:
            total = float(o.lines.aggregate(t=Sum('total_price'))['t'] or 0)
        except Exception:
            pass
        orders.append({
            "id":           o.pk,
            "order_number": getattr(o, 'order_number', str(o.pk)),
            "customer":     str(o.customer) if o.customer else '—',
            "status":       o.status,
            "date":         o.order_date.strftime('%d.%m.%Y') if o.order_date else '—',
            "total":        total,
        })
    return {"orders": orders}


def _get_customer_info(inp, profile):
    from crm.models import Customer

    customers = Customer.objects.filter(
        name__icontains=inp.get('customer_name', '')
    )[:3]
    result = []
    for c in customers:
        info = {
            "name":    c.name,
            "email":   getattr(c, 'email', '—'),
            "segment": getattr(c, 'segment', '—'),
        }
        for field in ('rfm_r', 'rfm_f', 'rfm_m'):
            val = getattr(c, field, None)
            if val is not None:
                info[field] = val
        result.append(info)
    return {"customers": result, "found": len(result)}


def _get_sales_analytics(inp, profile):
    from django.utils import timezone
    from django.db.models import Sum, Count
    from sales.models import SalesOrderLine

    now = timezone.now()
    period = inp.get('period', 'month')
    period_map = {
        'today':   now.replace(hour=0, minute=0, second=0, microsecond=0),
        'week':    now - timezone.timedelta(days=7),
        'month':   now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        'quarter': now - timezone.timedelta(days=90),
        'year':    now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0),
    }
    since = period_map.get(period, period_map['month'])

    base_qs = SalesOrderLine.objects.filter(
        order__order_date__gte=since,
        order__affects_stock=True,
    )
    metric = inp.get('metric', 'revenue')

    if metric == 'top_products':
        rows = list(
            base_qs.values('sku').annotate(
                total=Sum('total_price'), qty=Sum('quantity')
            ).order_by('-total')[:10]
        )
        return {"top_products": rows, "period": period}

    if metric == 'top_customers':
        rows = list(
            base_qs.values('order__customer__name').annotate(
                total=Sum('total_price'), orders=Count('order', distinct=True)
            ).order_by('-total')[:10]
        )
        return {"top_customers": rows, "period": period}

    agg = base_qs.aggregate(
        revenue=Sum('total_price'),
        orders=Count('order', distinct=True),
        qty=Sum('quantity'),
    )
    return {
        "period":       period,
        "revenue":      float(agg['revenue'] or 0),
        "orders_count": agg['orders'] or 0,
        "items_sold":   agg['qty'] or 0,
    }


def _get_financial_overview(inp, profile):
    from django.utils import timezone
    from sales.models import SalesOrder

    today = timezone.now().date()
    overdue = SalesOrder.objects.filter(
        shipping_deadline__lt=today
    ).exclude(status__in=['completed', 'shipped', 'cancelled']).select_related('customer')

    return {
        "overdue_orders": [
            {
                "id":          o.pk,
                "customer":    str(o.customer) if o.customer else '—',
                "deadline":    o.shipping_deadline.strftime('%d.%m.%Y'),
                "days_overdue":(today - o.shipping_deadline).days,
            }
            for o in overdue[:15]
        ],
        "overdue_count": overdue.count(),
    }


def _get_email_strategy(inp, profile):
    from crm.models import Customer

    name = inp.get('customer_name', '')
    c = Customer.objects.filter(name__icontains=name).first()
    if not c:
        return {"error": f"Клієнт '{name}' не знайдений"}
    info = {
        "customer": c.name,
        "segment":  getattr(c, 'segment', '—'),
        "instruction": (
            "На основі цих даних визнач оптимальну стратегію "
            "комунікації з клієнтом. Враховуй RFM сегмент."
        ),
    }
    for field in ('rfm_r', 'rfm_f', 'rfm_m'):
        val = getattr(c, field, None)
        if val is not None:
            info[field] = val
    return info


def _compose_email(inp, profile):
    from crm.models import Customer

    name = inp.get('customer_name', '')
    c = Customer.objects.filter(name__icontains=name).first()
    customer_data = {}
    if c:
        customer_data = {
            "name":    c.name,
            "email":   getattr(c, 'email', ''),
            "segment": getattr(c, 'segment', '—'),
        }
    return {
        "action":      "compose_only",
        "customer":    customer_data,
        "purpose":     inp.get('purpose', 'follow_up'),
        "language":    inp.get('language', 'uk'),
        "instruction": (
            f"Склади лист клієнту {name} з метою: {inp.get('purpose', 'follow_up')}. "
            f"Мова: {inp.get('language', 'uk')}. Тон: професійний але дружній. "
            "Поверни тему та текст листа."
        ),
    }


def _send_email(inp, profile):
    if not inp.get('confirm'):
        return {
            "error": "Потрібне підтвердження. Відправити цей лист?",
            "requires_confirm": True,
            "preview": {
                "to":            inp.get('to_email'),
                "subject":       inp.get('subject'),
                "body_preview":  (inp.get('body', '')[:200] + '...'),
            },
        }
    from django.core.mail import send_mail
    from strategy.models import AISettings
    s = AISettings.get()
    try:
        send_mail(
            subject=inp['subject'],
            message=inp['body'],
            from_email=s.from_email,
            recipient_list=[inp['to_email']],
            fail_silently=False,
        )
        return {"sent": True, "to": inp['to_email']}
    except Exception as e:
        return {"sent": False, "error": str(e)}


def _create_order(inp, profile):
    if not inp.get('confirm'):
        return {
            "error": "Потрібне підтвердження для створення замовлення",
            "requires_confirm": True,
            "preview": inp,
        }
    return {"error": "Створення замовлень через AI ще не реалізовано"}


def _update_inventory(inp, profile):
    if not inp.get('confirm'):
        return {
            "error": "Потрібне підтвердження для зміни складу",
            "requires_confirm": True,
            "preview": inp,
        }
    return {"error": "Редагування складу через AI ще не реалізовано"}


def _get_audit_log(inp, profile):
    from django.utils import timezone
    from core.models import AuditLog

    days = min(int(inp.get('days', 7)), 30)
    since = timezone.now() - timezone.timedelta(days=days)
    qs = AuditLog.objects.filter(timestamp__gte=since).order_by('-timestamp')
    if inp.get('user'):
        qs = qs.filter(user__username__icontains=inp['user'])

    return {
        "logs": [
            {
                "time":   l.timestamp.strftime('%d.%m %H:%M'),
                "user":   l.user.username if l.user else '—',
                "action": l.get_action_display(),
                "object": l.object_repr[:50],
            }
            for l in qs[:20]
        ],
        "total": qs.count(),
    }


_TOOL_HANDLERS = {
    "get_system_overview":   _get_system_overview,
    "get_inventory_status":  _get_inventory_status,
    "get_recent_orders":     _get_recent_orders,
    "get_customer_info":     _get_customer_info,
    "get_sales_analytics":   _get_sales_analytics,
    "get_financial_overview":_get_financial_overview,
    "get_email_strategy":    _get_email_strategy,
    "compose_email":         _compose_email,
    "send_email":            _send_email,
    "create_order":          _create_order,
    "update_inventory":      _update_inventory,
    "get_audit_log":         _get_audit_log,
}
