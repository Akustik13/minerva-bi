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

    B2B (contact_name + client): ключ за назвою компанії → один запис для всіх контактів.
    B2C (тільки email/client): ключ за email+ім'ям → один запис на людину.
    """
    # Якщо вже є customer_key — пропускаємо
    if instance.customer_key:
        return

    # Потрібен хоча б email або client
    if not instance.email and not instance.client:
        return

    from crm.models import Customer

    contact = getattr(instance, 'contact_name', '') or ''
    client  = instance.client or ''

    if contact and client:
        # B2B: ключ за назвою компанії — всі контакти однієї компанії → 1 запис
        key              = Customer.generate_key('b2b', client)
        customer_name    = contact or client
        customer_company = client
    else:
        # B2C або невідомо: ключ за email + ім'ям
        key = Customer.generate_key(
            instance.email or instance.client,
            instance.client or instance.email
        )
        customer_name    = client or (instance.email.split('@')[0] if instance.email else '')
        customer_company = ''

    # Нормалізуємо країну до ISO-2
    from config.country_utils import normalize_to_iso2
    country_iso2 = normalize_to_iso2(instance.shipping_region or "")

    # Шукаємо за новим ключем
    customer = Customer.objects.filter(external_key=key).first()

    # Fallback для B2B: знайти за назвою компанії (для старих записів зі старим ключем)
    if customer is None and contact and client:
        customer = Customer.objects.filter(company__iexact=client).first()
        if customer:
            # Переводимо існуючий запис на новий B2B-ключ
            customer.external_key = key
            customer.save(update_fields=['external_key'])

    if customer is None:
        customer = Customer.objects.create(
            external_key=key,
            name=customer_name,
            company=customer_company,
            email=instance.email or '',
            phone=instance.phone or '',
            country=instance.addr_country or country_iso2,
            addr_street=instance.addr_street or '',
            addr_city=instance.addr_city or '',
            addr_zip=instance.addr_zip or '',
            addr_state=(instance.addr_state or '')[:2],
            source=instance.source,
        )
    elif contact and client and not customer.company:
        customer.company = client
        customer.save(update_fields=['company'])

    # Оновлюємо SalesOrder з customer_key
    sender.objects.filter(pk=instance.pk).update(customer_key=key)


@receiver(post_save, sender='sales.SalesOrder')
def create_order_timeline_event(sender, instance, created, **kwargs):
    """При створенні замовлення — автоматично додати запис в хронологію клієнта."""
    if not created or not instance.customer_key:
        return
    try:
        from crm.models import Customer, CustomerTimeline
        customer = Customer.objects.filter(
            external_key=instance.customer_key).first()
        if not customer:
            return
        CustomerTimeline.objects.create(
            customer=customer,
            event_type='order',
            title=f'Замовлення #{instance.order_number}',
            body=(f'Сума: €{float(instance.total_price or 0):.2f} '
                  f'| Статус: {instance.status}'),
            related_order_id=instance.pk,
        )
    except Exception as e:
        import logging
        logging.getLogger('crm').error(f'Timeline signal error: {e}')


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
