"""
Сигнали для автоматичного оновлення складу при закупівлях та продажах
"""
import logging
from decimal import Decimal
import uuid

from django.db.models.signals import post_save, pre_delete, pre_save
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
    # При надходженні товару - тип залежить від Product.kind
    product = instance.product
    location_type = Location.LocationType.COMPONENTS if product and product.kind == product.Kind.COMPONENT else Location.LocationType.FINISHED

    location, created = Location.objects.get_or_create(
        code=loc_code,
        defaults={
            "name": "Основний склад",
            "location_type": location_type,
            "is_active": True,
        }
    )
    # Оновлюємо тип якщо локація вже існує але має неправильний
    if not created and location.location_type != location_type:
        location.location_type = location_type
        location.save(update_fields=['location_type'])

    # delta > 0 = receipt → INCOMING with positive qty
    # delta < 0 = return/correction → OUTGOING with negative qty (reduces stock)
    if delta > 0:
        tx_type = InventoryTransaction.TxType.INCOMING
        qty_val  = abs(delta)
    else:
        tx_type = InventoryTransaction.TxType.OUTGOING
        qty_val  = -abs(delta)

    try:
        from core.utils import get_current_user
        _performer = get_current_user()
    except Exception:
        _performer = None

    InventoryTransaction.objects.create(
        external_key=f"po:{instance.purchase_order.code}:line:{instance.pk}:{uuid.uuid4()}",
        tx_type=tx_type,
        qty=qty_val,
        product=instance.product,
        location=location,
        ref_doc=f"PO-{instance.purchase_order.code}",
        tx_date=timezone.now(),
        performed_by=_performer,
    )


# ── Замовлення продажу → склад ────────────────────────────────────────────────
# Уся логіка — inventory/services/stock.py (sync_order, release_order_stock, restore_order_stock,
# release_line_stock, rekey_order). Тут лише події моделей.

def setup_sales_signals():
    """
    Викликається з apps.py після завантаження всіх моделей. Коли саме списується товар — order_target
    (InventorySettings.deduct_on / use_reservation і статус замовлення):
      - позицію додано / змінено кількість чи товар → sync_order (Django Admin зберігає позиції ПІСЛЯ
        замовлення, тому при SalesOrder.post_save позицій ще немає)
      - змінився статус → order_status_changed: «Скасовано» повертає товар, зняття скасування — знову
        списує / бронює, «Відправлено» / «Доставлено» — списання за налаштуваннями
      - позицію або замовлення видалено → release_line_stock / release_order_stock (товар повертається,
        як при скасуванні; компоненти BOM замовлення — теж)
      - змінились джерело чи номер → rekey_order (рухи переходять на новий ключ)
      - перемикач «Впливає на склад»: вимкнули — резерви знімаються (уже списане лишається), увімкнули — за
        правилами вище (stock.order_saved)
    Компоненти BOM списує той самий sync_order (разом із позицією), окремого сигналу немає.
    Збереження лише інших полів (save(update_fields=["internal_note"]) тощо) склад не чіпає й не перебиває
    попередній стан зовнішнього збереження: стан — стек, pre_save кладе, post_save знімає.
    """
    from sales.models import SalesOrder, SalesOrderLine

    pre_save.connect(_capture_sales_order_old_state, sender=SalesOrder,
                     dispatch_uid="inventory_capture_salesorder_status", weak=False)
    post_save.connect(_salesorder_post_save, sender=SalesOrder,
                      dispatch_uid="inventory_salesorder_deduct", weak=False)
    pre_save.connect(_capture_salesorderline_old_state, sender=SalesOrderLine,
                     dispatch_uid="inventory_capture_salesorderline_state", weak=False)
    post_save.connect(_salesorderline_post_save, sender=SalesOrderLine,
                      dispatch_uid="inventory_salesorderline_deduct", weak=False)
    pre_delete.connect(_salesorderline_pre_delete, sender=SalesOrderLine,
                       dispatch_uid="inventory_salesorderline_release", weak=False)
    pre_delete.connect(_salesorder_pre_delete, sender=SalesOrder,
                       dispatch_uid="inventory_salesorder_release", weak=False)


_STOCK_FIELDS = ("status", "affects_stock", "source", "order_number")


def _touches_stock(update_fields):
    return update_fields is None or bool(set(_STOCK_FIELDS).intersection(update_fields))


def _capture_sales_order_old_state(sender, instance, update_fields=None, **kwargs):
    """Попередній статус, «Впливає на склад», джерело й номер — для post_save. Власний стек на об'єкті:
    вкладене збереження (інший сигнал post_save зберіг замовлення ще раз) не перебиває стан зовнішнього."""
    if not _touches_stock(update_fields):
        return
    old = (sender.objects.filter(pk=instance.pk).values(*_STOCK_FIELDS).first() if instance.pk else None)
    instance.__dict__.setdefault("_stock_old_states", []).append(old)


def _salesorder_post_save(sender, instance, created, update_fields=None, **kwargs):
    from inventory.services import stock
    if not _touches_stock(update_fields):
        return
    states = instance.__dict__.get("_stock_old_states") or []
    old = states.pop() if states else None
    if getattr(instance, "_skip_stock_signal", False):
        return
    try:
        changes = stock.order_saved(instance, old)
        if changes:
            logger.info("Inventory: order %s:%s — %s", instance.source, instance.order_number, "; ".join(changes))
    except Exception:
        # склад не повинен зламати збереження замовлення (сервіс працює в savepoint) — лише журнал
        logger.exception("Inventory: stock update failed for order %s:%s", instance.source, instance.order_number)


def _capture_salesorderline_old_state(sender, instance, **kwargs):
    """Попередній товар і кількість позиції — чи треба коригувати склад."""
    old = sender.objects.filter(pk=instance.pk).values("product_id", "qty").first() if instance.pk else None
    instance._old_product_id = old["product_id"] if old else None
    instance._old_qty = old["qty"] if old else None


def _salesorderline_post_save(sender, instance, created, **kwargs):
    """Нова позиція або змінено товар / кількість → склад за правилами (без подвійного повернення)."""
    from inventory.services import stock
    old = (getattr(instance, "_old_product_id", None), getattr(instance, "_old_qty", None))
    if not created and old == (instance.product_id, instance.qty):
        return
    order = instance.order
    try:
        changes = stock.sync_order(order)
    except Exception:
        logger.exception("Inventory: stock update failed for line %s order %s:%s",
                         instance.pk, order.source, order.order_number)
        return
    if changes and not created:
        # картка замовлення показує, що склад скориговано (SalesOrderAdmin.save_formset)
        instance._inventory_correction_msg = "; ".join(changes)


def _salesorderline_pre_delete(sender, instance, **kwargs):
    """Позицію (або все замовлення) видаляють — її товар повертається на склад, як при скасуванні."""
    from inventory.services import stock
    try:
        stock.release_line_stock(instance)
    except Exception:
        logger.exception("Inventory: stock release failed for deleted line %s", instance.pk)


def _salesorder_pre_delete(sender, instance, **kwargs):
    """Замовлення видаляють (позиції — у своєму сигналі раніше): ще й компоненти BOM, списані під нього."""
    from inventory.services import stock
    try:
        stock.release_order_stock(instance, reason="Видалення")
    except Exception:
        logger.exception("Inventory: stock release failed for deleted order %s", instance.pk)
