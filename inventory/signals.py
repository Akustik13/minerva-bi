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
    InventorySettings,
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
    Автоматично створює InventoryTransaction коли qty_received змінюється.
    Спрацьовує тільки якщо InventorySettings.add_on_po_receive = True.
    """
    if not instance.product:
        return

    settings = InventorySettings.get()
    if not settings.add_on_po_receive:
        return

    previous_received = getattr(instance, '_previous_qty_received', Decimal("0"))
    current_received = instance.qty_received or Decimal("0")
    delta = current_received - previous_received

    if delta == 0:
        return

    loc_code = settings.default_location or "MAIN"
    location, _ = Location.objects.get_or_create(
        code=loc_code,
        defaults={"name": "Основний склад"}
    )

    tx_type = InventoryTransaction.TxType.INCOMING if delta > 0 else InventoryTransaction.TxType.OUTGOING

    InventoryTransaction.objects.create(
        external_key=f"po:{instance.purchase_order.code}:line:{instance.pk}:{uuid.uuid4()}",
        tx_type=tx_type,
        qty=abs(delta),
        product=instance.product,
        location=location,
        ref_doc=f"PO-{instance.purchase_order.code}",
        tx_date=timezone.now(),
    )


# Імпортуємо SalesOrder та SalesOrderLine тут щоб уникнути циклічних імпортів
def setup_sales_signals():
    """
    Ця функція викликається з apps.py після того як всі моделі завантажені.
    Поведінка списання залежить від InventorySettings.deduct_on.
    """
    from sales.models import SalesOrder

    @receiver(pre_save, sender=SalesOrder)
    def _capture_sales_order_old_status(sender, instance, **kwargs):
        """Зберігаємо попередній статус для перевірки в post_save."""
        if instance.pk:
            try:
                instance._old_status = SalesOrder.objects.get(pk=instance.pk).status
            except SalesOrder.DoesNotExist:
                instance._old_status = None
        else:
            instance._old_status = None

    @receiver(post_save, sender=SalesOrder)
    def update_inventory_on_sales_order_save(sender, instance, created, **kwargs):
        """
        Автоматично створює InventoryTransaction відповідно до налаштування deduct_on:
          - creation  → при створенні замовлення
          - shipped   → при переході статусу на 'shipped'
          - delivered → при переході статусу на 'delivered'
        Ідемпотентно: повторний тригер не дублює транзакцій.
        """
        if not instance.affects_stock:
            return

        settings = InventorySettings.get()
        deduct_on = settings.deduct_on

        should_deduct = False
        if deduct_on == InventorySettings.DeductOn.CREATION and created:
            should_deduct = True
        elif deduct_on == InventorySettings.DeductOn.SHIPPED and not created:
            old = getattr(instance, '_old_status', None)
            if instance.status == 'shipped' and old != 'shipped':
                should_deduct = True
        elif deduct_on == InventorySettings.DeductOn.DELIVERED and not created:
            old = getattr(instance, '_old_status', None)
            if instance.status == 'delivered' and old != 'delivered':
                should_deduct = True

        if not should_deduct:
            return

        # Ідемпотентність: перевіряємо чи вже є транзакція для цього замовлення
        existing_prefix = f"so:{instance.source}:{instance.order_number}:line:"
        if InventoryTransaction.objects.filter(
            external_key__startswith=existing_prefix
        ).exists():
            return

        loc_code = settings.default_location or "MAIN"
        try:
            location = Location.objects.get(code=loc_code)
        except Location.DoesNotExist:
            location = Location.objects.create(code=loc_code, name="Основний склад")

        for line in instance.lines.all():
            if line.qty > 0:
                InventoryTransaction.objects.create(
                    external_key=f"so:{instance.source}:{instance.order_number}:line:{line.pk}:{uuid.uuid4()}",
                    tx_type=InventoryTransaction.TxType.OUTGOING,
                    qty=-abs(line.qty),
                    product=line.product,
                    location=location,
                    ref_doc=f"SO-{instance.source}:{instance.order_number}",
                    tx_date=instance.order_date or timezone.now(),
                )
