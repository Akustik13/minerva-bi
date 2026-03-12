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
