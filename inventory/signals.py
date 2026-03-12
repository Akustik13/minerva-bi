"""
Сигнали для автоматичного оновлення складу при закупівлях та продажах
"""
from decimal import Decimal
import uuid

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .models import (
    PurchaseOrderLine,
    InventoryTransaction,
    Location,
)


@receiver(pre_save, sender=PurchaseOrderLine)
def track_purchase_order_line_changes(sender, instance, **kwargs):
    """
    Зберігаємо попереднє значення qty_received для порівняння
    """
    if instance.pk:
        try:
            previous = PurchaseOrderLine.objects.get(pk=instance.pk)
            instance._previous_qty_received = previous.qty_received
        except PurchaseOrderLine.DoesNotExist:
            instance._previous_qty_received = Decimal("0")
    else:
        instance._previous_qty_received = Decimal("0")


@receiver(post_save, sender=PurchaseOrderLine)
def update_inventory_on_purchase_receipt(sender, instance, created, **kwargs):
    """
    Автоматично створює InventoryTransaction коли qty_received змінюється
    
    Це означає що коли ви отримуєте товар (збільшуєте qty_received),
    автоматично створюється запис про надходження на склад.
    """
    if not instance.product:
        return  # Якщо немає прив'язаного продукту, нічого не робимо
    
    # Отримуємо попереднє значення (збережене в pre_save)
    previous_received = getattr(instance, '_previous_qty_received', Decimal("0"))
    current_received = instance.qty_received or Decimal("0")
    
    # Обчислюємо різницю
    delta = current_received - previous_received
    
    if delta == 0:
        return  # Якщо кількість не змінилась, нічого не робимо
    
    # Знаходимо основну локацію (або створюємо якщо немає)
    location, _ = Location.objects.get_or_create(
        code="MAIN",
        defaults={"name": "Основний склад"}
    )
    
    # Створюємо транзакцію надходження
    tx_type = InventoryTransaction.TxType.INCOMING if delta > 0 else InventoryTransaction.TxType.OUTGOING
    
    InventoryTransaction.objects.create(
        external_key=f"po:{instance.purchase_order.code}:line:{instance.pk}:{uuid.uuid4()}",
        tx_type=tx_type,
        qty=abs(delta),  # Завжди позитивне значення
        product=instance.product,
        location=location,
        ref_doc=f"PO-{instance.purchase_order.code}",
        tx_date=timezone.now(),
    )


# Імпортуємо SalesOrder та SalesOrderLine тут щоб уникнути циклічних імпортів
def setup_sales_signals():
    """
    Ця функція викликається з apps.py після того як всі моделі завантажені
    """
    from sales.models import SalesOrder, SalesOrderLine
    
    @receiver(post_save, sender=SalesOrder)
    def update_inventory_on_sales_order_save(sender, instance, created, **kwargs):
        """
        Автоматично створює InventoryTransaction для всіх позицій замовлення
        коли SalesOrder зберігається і affects_stock = True
        
        ВАЖЛИВО: Цей сигнал спрацює тільки один раз при створенні замовлення.
        Якщо потрібно оновити кількість пізніше, використовуйте адмінку для ручного коригування.
        """
        if not created:
            return  # Оновлення існуючого замовлення не створює нових транзакцій
        
        if not instance.affects_stock:
            return  # Якщо замовлення не впливає на склад
        
        # Знаходимо основну локацію
        try:
            location = Location.objects.get(code="MAIN")
        except Location.DoesNotExist:
            location = Location.objects.create(
                code="MAIN",
                name="Основний склад"
            )
        
        # Створюємо транзакції для кожної позиції
        for line in instance.lines.all():
            if line.qty > 0:
                InventoryTransaction.objects.create(
                    external_key=f"so:{instance.source}:{instance.order_number}:line:{line.pk}:{uuid.uuid4()}",
                    tx_type=InventoryTransaction.TxType.OUTGOING,
                    qty=-abs(line.qty),  # Від'ємне значення для відвантаження
                    product=line.product,
                    location=location,
                    ref_doc=f"SO-{instance.source}:{instance.order_number}",
                    tx_date=instance.order_date or timezone.now(),
                )
