"""
crm/signals.py — Автоматична синхронізація Customer при збереженні SalesOrder
             + Event-based notifications (new order, status change)
"""
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver


@receiver(post_save, sender='sales.SalesOrder')
def auto_sync_customer(sender, instance, created, **kwargs):
    """
    Автоматично створює/оновлює Customer при збереженні SalesOrder.
    """
    # Якщо вже є customer_key — пропускаємо
    if instance.customer_key:
        return
    
    # Потрібен хоча б email або client
    if not instance.email and not instance.client:
        return
    
    from crm.models import Customer
    
    # Генеруємо ключ
    key = Customer.generate_key(
        instance.email or instance.client,
        instance.client or instance.email
    )
    
    # Нормалізуємо країну до ISO-2
    from config.country_utils import normalize_to_iso2
    country_iso2 = normalize_to_iso2(instance.shipping_region or "")

    # Розділяємо: client = компанія, contact_name = контактна особа
    contact = getattr(instance, 'contact_name', '') or ''
    client  = instance.client or ''
    if contact:
        # B2B: client = компанія, contact_name = контактна особа
        customer_name    = contact
        customer_company = client
    else:
        # B2C або невідомо: client = ім'я людини, company порожня
        customer_name    = client or (instance.email.split('@')[0] if instance.email else '')
        customer_company = ''

    # Шукаємо існуючого клієнта
    customer, created = Customer.objects.get_or_create(
        external_key=key,
        defaults={
            'name':         customer_name,
            'company':      customer_company,
            'email':        instance.email or '',
            'phone':        instance.phone or '',
            'country':      instance.addr_country or country_iso2,
            'addr_street':  instance.addr_street or '',
            'addr_city':    instance.addr_city or '',
            'addr_zip':     instance.addr_zip or '',
            'source':       instance.source,
        }
    )
    # Якщо клієнт вже існував — оновити company/name якщо з'явились нові дані
    if not created and contact and not customer.company:
        customer.name    = contact
        customer.company = client
        customer.save(update_fields=['name', 'company'])

    # Оновлюємо SalesOrder з customer_key
    sender.objects.filter(pk=instance.pk).update(customer_key=key)


@receiver(pre_save, sender='sales.SalesOrder')
def _capture_old_status(sender, instance, **kwargs):
    """Зберігаємо старий статус перед збереженням для порівняння."""
    if not instance.pk:
        instance._old_status = None
        return
    try:
        old = sender.objects.filter(pk=instance.pk).values('status').first()
        instance._old_status = old['status'] if old else None
    except Exception:
        instance._old_status = None


@receiver(post_save, sender='sales.SalesOrder')
def _notify_order_events(sender, instance, created, **kwargs):
    """Надсилає сповіщення про нове замовлення або зміну статусу."""
    try:
        from dashboard.notifications import notify_new_order, notify_status_change
        if created:
            notify_new_order(instance)
        else:
            old_status = getattr(instance, '_old_status', None)
            if old_status and old_status != instance.status:
                notify_status_change(instance, old_status, instance.status)
    except Exception:
        pass  # Сповіщення не повинні ламати збереження
