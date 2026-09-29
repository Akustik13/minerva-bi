"""Сигнали для модуля sales."""
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from django.contrib import messages
from .models import SalesOrder
from inventory.models import Product
from inventory.utils import deduct_components_for_assembly, get_assembly_status


@receiver(pre_save, sender=SalesOrder)
def capture_old_status(sender, instance, **kwargs):
    """Зберегти старий статус для перевірки змін."""
    if not instance.pk:
        instance._old_status = None
        return
    try:
        old = SalesOrder.objects.get(pk=instance.pk)
        instance._old_status = old.status
    except (SalesOrder.DoesNotExist, Exception):
        instance._old_status = None


@receiver(post_save, sender=SalesOrder)
def handle_status_change_and_assembly(sender, instance, created, **kwargs):
    """
    При зміні статусу на 'shipped' — автоматично вилучити компоненти за BOM.
    """
    try:
        old_status = getattr(instance, '_old_status', None)
        new_status = instance.status

        # Перевірка: чи статус змінився на "shipped"
        if old_status != 'shipped' and new_status == 'shipped':
            _process_assembly_deductions(instance)
    except Exception as e:
        import logging
        logging.error(f"Assembly deduction error: {e}")
        pass


def _process_assembly_deductions(order):
    """Обробити вилучення компонентів для замовлення."""
    from django.db.models import Q

    # Отримати всі товари в замовленні
    try:
        from sales.models import SalesOrderLine
    except ImportError:
        return

    lines = SalesOrderLine.objects.filter(order=order).select_related('product')

    for line in lines:
        product = line.product
        if not product or product.bom_type != Product.BomType.KEY:
            continue

        # Перевірити чи можна зібрати
        status = get_assembly_status(product, int(line.qty))

        if not status['can_assemble']:
            # Записати як помилку в order notes
            order.internal_note = (order.internal_note or '') + \
                f"\n⚠️ БОМ: {status['reason']}"
            order.save(update_fields=['internal_note'])
            continue

        # Вилучити компоненти
        result = deduct_components_for_assembly(
            product=product,
            qty=int(line.qty),
            ref_doc=f"Order #{order.order_number}",
            order_pk=order.pk
        )

        if not result['ok']:
            # Записати помилку
            order.internal_note = (order.internal_note or '') + \
                f"\n❌ БОМ помилка: {result['error']}"
            order.save(update_fields=['internal_note'])
        else:
            # Записати успіх
            after = status.get('after_assembly', {})
            summary = ', '.join(
                f"{sku}: {v['before']}→{v['after']}"
                for sku, v in after.items()
            )
            order.internal_note = (order.internal_note or '') + \
                f"\n✅ БОМ: вилучено компоненти для {product.sku} ({summary})"
            order.save(update_fields=['internal_note'])
