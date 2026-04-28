"""
Сигнали для автоматичного оновлення складу при закупівлях та продажах
"""
import logging
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

logger = logging.getLogger(__name__)


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

    # delta > 0 = receipt (incoming), delta < 0 = return/correction (outgoing)
    # qty must always be positive; tx_type determines direction
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


def _deduct_line(line, order, location):
    """
    Внутрішня функція: списати один рядок замовлення зі складу.
    Повертає True якщо транзакція створена, False — якщо вже існує.
    """
    external_key_prefix = f"so:{order.source}:{order.order_number}:line:{line.pk}:"
    if InventoryTransaction.objects.filter(
        external_key__startswith=external_key_prefix
    ).exists():
        return False  # вже списано

    InventoryTransaction.objects.create(
        external_key=f"so:{order.source}:{order.order_number}:line:{line.pk}:{uuid.uuid4()}",
        tx_type=InventoryTransaction.TxType.OUTGOING,
        qty=-abs(line.qty),  # негативне значення зменшує залишок через Sum("qty")
        product=line.product,
        location=location,
        ref_doc=f"SO-{order.source}:{order.order_number}",
        tx_date=order.order_date or timezone.now(),
    )
    return True


def _get_or_create_location(settings):
    loc_code = settings.default_location or "MAIN"
    location, _ = Location.objects.get_or_create(
        code=loc_code,
        defaults={"name": "Основний склад"}
    )
    return location


# Імпортуємо SalesOrder та SalesOrderLine тут щоб уникнути циклічних імпортів
def setup_sales_signals():
    """
    Ця функція викликається з apps.py після того як всі моделі завантажені.
    Поведінка списання залежить від InventorySettings.deduct_on:
      - creation  → при ЗБЕРЕЖЕННІ КОЖНОГО РЯДКА замовлення (SalesOrderLine.post_save)
                    Причина: Django Admin зберігає рядки ПІСЛЯ батьківського запису,
                    тому при SalesOrder.post_save рядків ще немає.
      - shipped   → при переході статусу на «Відправлено» (SalesOrder.post_save)
      - delivered → при переході статусу на «Доставлено» (SalesOrder.post_save)
    """
    from sales.models import SalesOrder, SalesOrderLine

    # ── PRE-SAVE: зберігаємо попередній статус ────────────────────────────────
    pre_save.connect(
        _capture_sales_order_old_status,
        sender=SalesOrder,
        dispatch_uid="inventory_capture_salesorder_status",
        weak=False,
    )

    # ── POST-SAVE SalesOrder: SHIPPED / DELIVERED modes ───────────────────────
    post_save.connect(
        _salesorder_post_save,
        sender=SalesOrder,
        dispatch_uid="inventory_salesorder_deduct",
        weak=False,
    )

    # ── POST-SAVE SalesOrderLine: CREATION mode ───────────────────────────────
    # При deduct_on='creation' списуємо КОЖЕН рядок одразу після збереження.
    # Це обходить проблему порожнього instance.lines.all() при першому збереженні
    # батьківського SalesOrder через Django Admin inline.
    post_save.connect(
        _salesorderline_post_save,
        sender=SalesOrderLine,
        dispatch_uid="inventory_salesorderline_deduct",
        weak=False,
    )


def _capture_sales_order_old_status(sender, instance, **kwargs):
    """Зберігаємо попередній статус для перевірки в post_save."""
    if instance.pk:
        try:
            instance._old_status = sender.objects.get(pk=instance.pk).status
        except sender.DoesNotExist:
            instance._old_status = None
    else:
        instance._old_status = None


def _salesorder_post_save(sender, instance, created, **kwargs):
    """
    SHIPPED та DELIVERED: списання при зміні статусу замовлення.
    CREATION: НЕ обробляється тут — дивись _salesorderline_post_save.
    """
    if not instance.affects_stock:
        return

    try:
        settings = InventorySettings.get()
    except Exception:
        return

    deduct_on = settings.deduct_on

    should_deduct = False
    if deduct_on == InventorySettings.DeductOn.SHIPPED and not created:
        old = getattr(instance, '_old_status', None)
        if instance.status == 'shipped' and old != 'shipped':
            should_deduct = True
    elif deduct_on == InventorySettings.DeductOn.DELIVERED and not created:
        old = getattr(instance, '_old_status', None)
        if instance.status == 'delivered' and old != 'delivered':
            should_deduct = True

    if not should_deduct:
        return

    # Ідемпотентність: якщо хоч один рядок вже списаний — пропускаємо
    existing_prefix = f"so:{instance.source}:{instance.order_number}:line:"
    if InventoryTransaction.objects.filter(
        external_key__startswith=existing_prefix
    ).exists():
        logger.info(
            "Inventory: order %s:%s already deducted — skipping",
            instance.source, instance.order_number
        )
        return

    try:
        location = _get_or_create_location(settings)
    except Exception as e:
        logger.error("Inventory: cannot get/create location: %s", e)
        return

    count = 0
    for line in instance.lines.all():
        if line.qty > 0:
            try:
                if _deduct_line(line, instance, location):
                    count += 1
            except Exception as e:
                logger.error(
                    "Inventory: failed to deduct line %s for order %s:%s — %s",
                    line.pk, instance.source, instance.order_number, e
                )

    if count:
        logger.info(
            "Inventory: deducted %d lines for order %s:%s (trigger: %s)",
            count, instance.source, instance.order_number,
            InventorySettings.get().deduct_on,
        )


def _salesorderline_post_save(sender, instance, created, **kwargs):
    """
    CREATION mode: списання при збереженні рядка замовлення.
    Спрацьовує тільки при deduct_on='creation' і тільки для нових рядків.
    """
    if not created:
        return

    order = instance.order
    if not order.affects_stock:
        return

    try:
        settings = InventorySettings.get()
    except Exception:
        return

    if settings.deduct_on != InventorySettings.DeductOn.CREATION:
        return

    if instance.qty <= 0:
        return

    try:
        location = _get_or_create_location(settings)
        _deduct_line(instance, order, location)
    except Exception as e:
        logger.error(
            "Inventory: failed to deduct line %s for order %s:%s — %s",
            instance.pk, order.source, order.order_number, e
        )
