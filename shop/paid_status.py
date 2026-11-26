"""Оплачене замовлення інтернет-магазину → «В обробці» (`Shop.paid_order_status`).

Сигнал (`shop/signals.py`) лише помічає, що замовлення стало «Оплачено» (або створене вже оплаченим), і після commit
кличе `apply` — тоді вже є `ShopOrder` (зв'язок із магазином). Будь-який шлях оплати: вебхук / перевірка Stripe,
позначка в картці замовлення, PATCH сайту. Змінюється лише «Отримано»: що людина вже перевела далі (або назад
у «Отримано» після оплати) — не чіпаємо. Збереження — звичайне `save(update_fields=[status, …])`, тож склад,
сповіщення про зміну статусу й вебхук `order.status_changed` спрацьовують як від картки.
"""
import logging

logger = logging.getLogger(__name__)

SOURCE = "Оплата"     # «Джерело статусу» замовлення
FROM_STATUS = "received"


def apply(order_pk) -> str:
    """Повертає: not_shop / off / gone / skip / done (і пише в журнал, якщо змінено)."""
    from django.db import transaction
    from sales.models import SalesOrder
    from .models import ShopOrder
    info = ShopOrder.objects.filter(order_id=order_pk).select_related("shop").first()
    if info is None:
        return "not_shop"
    target = info.shop.paid_order_status
    if not target:
        return "off"
    with transaction.atomic():
        order = SalesOrder.objects.select_for_update().filter(pk=order_pk).first()
        if order is None:
            return "gone"
        if (order.payment_status != "paid" or order.status != FROM_STATUS
                or (order.document_type or "SALE") != "SALE"):
            return "skip"
        order.status, order.status_source = target, SOURCE
        order.save(update_fields=["status", "status_source"])
    logger.info("Shop order %s paid → status %s", order.order_number, target)
    return "done"


def safe_apply(order_pk) -> None:
    try:
        apply(order_pk)
    except Exception as e:  # noqa: BLE001 — оплата вже записана; статус людина змінить сама
        logger.warning("Paid order %s → status failed: %s", order_pk, e)
