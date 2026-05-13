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

    try:
        from core.utils import get_current_user
        _performer = get_current_user()
    except Exception:
        _performer = None

    InventoryTransaction.objects.create(
        external_key=f"po:{instance.purchase_order.code}:line:{instance.pk}:{uuid.uuid4()}",
        tx_type=tx_type,
        qty=abs(delta),
        product=instance.product,
        location=location,
        ref_doc=f"PO-{instance.purchase_order.code}",
        tx_date=timezone.now(),
        performed_by=_performer,
    )


def _deduct_line(line, order, location, tx_type=None):
    """
    Внутрішня функція: створити транзакцію для рядка замовлення.
    tx_type = OUTGOING (фактичне списання) або RESERVED (бронювання).
    Повертає True якщо транзакція створена, False — якщо вже існує.
    """
    if tx_type is None:
        tx_type = InventoryTransaction.TxType.OUTGOING

    external_key_prefix = f"so:{order.source}:{order.order_number}:line:{line.pk}:"
    existing = InventoryTransaction.objects.filter(
        external_key__startswith=external_key_prefix
    ).first()

    if existing:
        # Якщо є резерв і тепер треба списати — конвертуємо
        if (existing.tx_type == InventoryTransaction.TxType.RESERVED
                and tx_type == InventoryTransaction.TxType.OUTGOING):
            existing.tx_type = InventoryTransaction.TxType.OUTGOING
            existing.save(update_fields=['tx_type'])
            return True
        return False  # вже є потрібна транзакція

    try:
        from core.utils import get_current_user
        _performer = get_current_user()
    except Exception:
        _performer = None

    InventoryTransaction.objects.create(
        external_key=f"so:{order.source}:{order.order_number}:line:{line.pk}:{uuid.uuid4()}",
        tx_type=tx_type,
        qty=-abs(line.qty),
        product=line.product,
        location=location,
        ref_doc=f"SO-{order.source}:{order.order_number}",
        tx_date=order.order_date or timezone.now(),
        performed_by=_performer,
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

    # ── PRE-SAVE SalesOrderLine: capture old state for correction detection ──────
    pre_save.connect(
        _capture_salesorderline_old_state,
        sender=SalesOrderLine,
        dispatch_uid="inventory_capture_salesorderline_state",
        weak=False,
    )

    # ── POST-SAVE SalesOrderLine: CREATION mode + SKU/QTY change correction ─────
    # При deduct_on='creation' списуємо КОЖЕН рядок одразу після збереження.
    # Також обробляє зміни SKU / кількості в існуючих рядках (авто-коригування).
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
    SHIPPED / DELIVERED: списання або конвертація резервів при зміні статусу.
    CREATION mode обробляється в _salesorderline_post_save.
    """
    if not instance.affects_stock:
        return

    try:
        settings = InventorySettings.get()
    except Exception:
        return

    deduct_on       = settings.deduct_on
    use_reservation = getattr(settings, 'use_reservation', False)
    old_status      = getattr(instance, '_old_status', None)
    just_shipped    = (not created and instance.status == 'shipped' and old_status != 'shipped')
    just_delivered  = (not created and instance.status == 'delivered' and old_status != 'delivered')

    should_deduct         = False
    should_convert_reserv = False

    if just_shipped:
        if use_reservation:
            # Convert any existing RESERVED → OUTGOING
            should_convert_reserv = True
        if deduct_on == InventorySettings.DeductOn.SHIPPED and not use_reservation:
            should_deduct = True

    if just_delivered:
        if use_reservation:
            # Safety net: convert any reservations not yet released on shipped
            should_convert_reserv = True
        if deduct_on == InventorySettings.DeductOn.DELIVERED and not use_reservation:
            should_deduct = True

    if not should_deduct and not should_convert_reserv:
        return

    try:
        location = _get_or_create_location(settings)
    except Exception as e:
        logger.error("Inventory: cannot get/create location: %s", e)
        return

    existing_prefix = f"so:{instance.source}:{instance.order_number}:line:"

    if should_convert_reserv:
        # Convert RESERVED → OUTGOING (idempotent: re-run is safe)
        count = InventoryTransaction.objects.filter(
            external_key__startswith=existing_prefix,
            tx_type=InventoryTransaction.TxType.RESERVED,
        ).update(tx_type=InventoryTransaction.TxType.OUTGOING)
        if count:
            logger.info(
                "Inventory: converted %d reservations → deductions for order %s:%s",
                count, instance.source, instance.order_number,
            )

    if should_deduct:
        # Ідемпотентність: якщо рядки вже є (не RESERVED) — пропускаємо
        if InventoryTransaction.objects.filter(
            external_key__startswith=existing_prefix,
        ).exclude(tx_type=InventoryTransaction.TxType.RESERVED).exists():
            logger.info(
                "Inventory: order %s:%s already deducted — skipping",
                instance.source, instance.order_number,
            )
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
                        line.pk, instance.source, instance.order_number, e,
                    )
        if count:
            logger.info(
                "Inventory: deducted %d lines for order %s:%s (trigger: %s)",
                count, instance.source, instance.order_number, deduct_on,
            )


def _capture_salesorderline_old_state(sender, instance, **kwargs):
    """Зберігаємо попередній product_id і qty для detection змін у post_save."""
    if instance.pk:
        try:
            old = sender.objects.get(pk=instance.pk)
            instance._old_product_id = old.product_id
            instance._old_qty        = old.qty
        except sender.DoesNotExist:
            instance._old_product_id = None
            instance._old_qty        = None
    else:
        instance._old_product_id = None
        instance._old_qty        = None


def _correct_salesorderline_tx(line, old_product_id, old_qty, order, location):
    """
    При зміні SKU або кількості в рядку замовлення:
    1. Знаходить існуючі транзакції для цього рядка
    2. Створює Adjustment-транзакцію (+qty) для старого товару (повернення)
    3. Видаляє старі транзакції рядка
    4. Створює нову транзакцію (-qty) для нового товару

    Повертає опис коригування або None якщо нічого не змінювалось.
    """
    from inventory.models import Product

    prefix = f"so:{order.source}:{order.order_number}:line:{line.pk}:"
    existing = list(InventoryTransaction.objects.filter(external_key__startswith=prefix))
    if not existing:
        return None  # Не було транзакцій — нічого коригувати

    settings = InventorySettings.get()
    use_reservation = getattr(settings, 'use_reservation', False)

    try:
        performer = None
        try:
            from core.utils import get_current_user
            performer = get_current_user()
        except Exception:
            pass

        old_product = None
        if old_product_id:
            try:
                old_product = Product.objects.get(pk=old_product_id)
            except Product.DoesNotExist:
                pass

        reversal_qty = abs(old_qty or Decimal("0"))
        if old_product and reversal_qty > 0:
            InventoryTransaction.objects.create(
                external_key=f"so-adj:{order.source}:{order.order_number}:line:{line.pk}:rev:{uuid.uuid4()}",
                tx_type=InventoryTransaction.TxType.ADJUSTMENT,
                qty=reversal_qty,
                product=old_product,
                location=location,
                ref_doc=(f"Коригування зміни SKU: {old_product.sku}"
                         f" → {line.product.sku if line.product else '?'}"
                         f" | SO-{order.source}:{order.order_number}"),
                tx_date=timezone.now(),
                performed_by=performer,
            )

        # Remove old line transactions
        for tx in existing:
            tx.delete()

        # Create new deduction for current product
        if line.product_id and line.qty > 0:
            tx_type = (InventoryTransaction.TxType.RESERVED
                       if use_reservation
                       else InventoryTransaction.TxType.OUTGOING)
            InventoryTransaction.objects.create(
                external_key=f"so:{order.source}:{order.order_number}:line:{line.pk}:{uuid.uuid4()}",
                tx_type=tx_type,
                qty=-abs(line.qty),
                product=line.product,
                location=location,
                ref_doc=f"SO-{order.source}:{order.order_number}",
                tx_date=order.order_date or timezone.now(),
                performed_by=performer,
            )

        msg = (f"SKU {old_product.sku if old_product else '?'}"
               f" → {line.product.sku if line.product else '?'}, "
               f"qty {old_qty} → {line.qty}")
        logger.info(
            "Inventory: corrected line %s of order %s:%s — %s",
            line.pk, order.source, order.order_number, msg,
        )
        return msg

    except Exception as e:
        logger.error(
            "Inventory: correction failed for line %s order %s:%s — %s",
            line.pk, order.source, order.order_number, e,
        )
        return None


def _salesorderline_post_save(sender, instance, created, **kwargs):
    """
    CREATION mode / Reservation: транзакція при збереженні рядка замовлення.

    - deduct_on='creation' + use_reservation=False → OUTGOING одразу
    - use_reservation=True (будь-який deduct_on)    → RESERVED (бронювання)

    Для нових рядків (created=True): стандартне списання/бронювання.
    Для оновлених (created=False): перевіряє зміну SKU/кількості й авто-коригує склад.
    """
    order = instance.order
    if not order.affects_stock:
        return

    try:
        settings = InventorySettings.get()
    except Exception:
        return

    use_reservation = getattr(settings, 'use_reservation', False)

    if created:
        # ── Нова позиція ───────────────────────────────────────────────────────
        if not use_reservation and settings.deduct_on != InventorySettings.DeductOn.CREATION:
            return
        if instance.qty <= 0:
            return
        tx_type = (InventoryTransaction.TxType.RESERVED
                   if use_reservation
                   else InventoryTransaction.TxType.OUTGOING)
        try:
            location = _get_or_create_location(settings)
            _deduct_line(instance, order, location, tx_type=tx_type)
        except Exception as e:
            logger.error(
                "Inventory: failed to create %s transaction for line %s order %s:%s — %s",
                tx_type, instance.pk, order.source, order.order_number, e,
            )
        return

    # ── Оновлення існуючого рядка: перевіряємо чи змінився SKU або qty ────────
    old_product_id = getattr(instance, '_old_product_id', None)
    old_qty        = getattr(instance, '_old_qty', None)

    if old_product_id is None:
        return  # pre_save не захопив стан — нічого робити

    product_changed = (old_product_id != instance.product_id)
    qty_changed     = (old_qty is not None and old_qty != instance.qty)

    if not product_changed and not qty_changed:
        return

    try:
        location = _get_or_create_location(settings)
        correction = _correct_salesorderline_tx(instance, old_product_id, old_qty, order, location)
        if correction:
            # Store the correction message on the instance so admin can display it
            instance._inventory_correction_msg = correction
    except Exception as e:
        logger.error(
            "Inventory: SKU-change correction failed for line %s order %s:%s — %s",
            instance.pk, order.source, order.order_number, e,
        )
