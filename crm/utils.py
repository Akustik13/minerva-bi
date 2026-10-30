"""
crm/utils.py — Утиліти для автоматичної синхронізації клієнтів
"""
from django.db.models import Q


def sync_customer_from_order(order):
    """
    Створює або оновлює клієнта в CRM на основі замовлення.
    
    Args:
        order: SalesOrder instance
        
    Returns:
        Customer instance або None
    """
    from crm.models import Customer
    
    # Мінімальні вимоги — email або ім'я
    if not order.email and not order.client:
        return None
    
    # Шукаємо існуючого клієнта
    customer = None
    
    if order.email:
        customer = Customer.objects.filter(email__iexact=order.email).first()
    
    if not customer and order.client:
        customer = Customer.objects.filter(name__iexact=order.client).first()
    
    # Якщо знайшли — оновлюємо дані
    if customer:
        updated = False
        
        # Оновлюємо email якщо його не було
        if order.email and not customer.email:
            customer.email = order.email
            updated = True
        
        # Оновлюємо телефон якщо його не було
        if order.phone and not customer.phone:
            customer.phone = order.phone
            updated = True
        
        # Оновлюємо країну з shipping_region
        if order.shipping_region and not customer.country:
            customer.country = order.shipping_region
            updated = True
        
        if updated:
            customer.save()
        
        return customer
    
    # Створюємо нового клієнта
    customer_data = {
        'name': order.client or order.email.split('@')[0],
        'email': order.email or '',
        'phone': order.phone or '',
        'country': order.shipping_region or '',
        'company': order.client if '@' not in (order.client or '') else '',
    }
    
    customer = Customer.objects.create(**customer_data)
    return customer


def bulk_sync_all_customers():
    """
    Синхронізує ВСІ замовлення з CRM.
    Створює клієнтів для всіх замовлень де їх немає.
    
    Returns:
        dict з статистикою
    """
    from sales.models import SalesOrder
    
    stats = {
        'processed': 0,
        'created': 0,
        'updated': 0,
        'skipped': 0,
    }
    
    orders = SalesOrder.objects.all()
    
    for order in orders:
        stats['processed'] += 1
        
        if not order.email and not order.client:
            stats['skipped'] += 1
            continue
        
        # Перевіряємо чи вже є клієнт
        from crm.models import Customer
        existing = None
        
        if order.email:
            existing = Customer.objects.filter(email__iexact=order.email).first()
        if not existing and order.client:
            existing = Customer.objects.filter(name__iexact=order.client).first()
        
        if existing:
            stats['skipped'] += 1
            continue
        
        # Створюємо
        customer = sync_customer_from_order(order)
        if customer:
            stats['created'] += 1

    return stats


# ── Замовлення клієнта: постійний чи новий (фільтр «Тип клієнта», колонка «Тип») ──

REPEAT_MIN_ORDERS = 2   # від 2 замовлень — постійний (як F ≥ 2 в RFM); 0–1 — новий


def _norm_email(email) -> str:
    return (email or "").strip().lower()


def order_counts(customers=None) -> dict:
    """{pk клієнта: кількість його замовлень} — так само, як замовлення веде на клієнта в Продажах:
    спершу ключ (SalesOrder.customer_key = Customer.external_key), а замовлення без ключа клієнта CRM
    (порожній ключ або клієнта з таким ключем уже немає) — за email.

    customers — клієнти (список / queryset) або None — усі (фільтр списку). Два групові запити замість
    підзапиту на кожен рядок."""
    from django.db.models import Count
    from django.db.models.functions import Lower, Trim
    from crm.models import Customer
    from sales.models import SalesOrder

    if customers is None:
        rows = list(Customer.objects.values_list("pk", "external_key", "email"))
    else:
        rows = [(c.pk, c.external_key, c.email) for c in customers]
    if not rows:
        return {}

    orders = SalesOrder.objects.order_by()
    keyed = orders.exclude(customer_key="")
    if customers is not None:
        keyed = keyed.filter(customer_key__in={k for _, k, _ in rows if k})
    by_key = dict(keyed.values_list("customer_key").annotate(n=Count("id")))

    emails = {_norm_email(e) for _, _, e in rows if _norm_email(e)}
    by_email = {}
    if emails:
        loose = (orders.exclude(email="")
                 .exclude(customer_key__in=Customer.objects.exclude(external_key="").values("external_key"))
                 .annotate(email_norm=Lower(Trim("email"))))
        if customers is not None:
            loose = loose.filter(email_norm__in=emails)
        by_email = dict(loose.values_list("email_norm").annotate(n=Count("id")))

    counts = {}
    for pk, key, email in rows:
        email = _norm_email(email)
        counts[pk] = by_key.get(key, 0) + (by_email.get(email, 0) if email else 0)
    return counts


def is_repeat(order_count) -> bool:
    return (order_count or 0) >= REPEAT_MIN_ORDERS


def repeat_customer_ids() -> set:
    """Постійні клієнти (від REPEAT_MIN_ORDERS замовлень) — для фільтра «Тип клієнта»."""
    return {pk for pk, n in order_counts().items() if is_repeat(n)}
