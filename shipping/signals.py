from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from .models import Shipment


@receiver(pre_save, sender=Shipment)
def _capture_old_shipment_status(sender, instance, **kwargs):
    if instance.pk:
        try:
            instance._old_ship_status = sender.objects.filter(
                pk=instance.pk).values_list('status', flat=True).first()
        except Exception:
            instance._old_ship_status = None
    else:
        instance._old_ship_status = None


@receiver(post_save, sender=Shipment)
def _notify_shipment_events(sender, instance, created, **kwargs):
    try:
        old_status = getattr(instance, '_old_ship_status', None)
        new_status = instance.status
        if old_status and old_status != new_status:
            from dashboard.notifications import notify_shipment_status
            notify_shipment_status(instance, old_status, new_status)
            if new_status == 'delivered' and getattr(instance, 'direction', 'out') == 'in':
                from django.db import transaction
                from shipping.services.incoming import on_delivered
                pk = instance.pk   # 📥 посилку доставлено → закупівля (після commit: трекінг уже записав статус)
                transaction.on_commit(lambda: on_delivered(Shipment.objects.get(pk=pk)))
    except Exception:
        pass


# ── 📦 Відправлення частинами (shipping/services/parcels.py) ───────────────────

import logging

from django.db.models.signals import post_save as _post_save, pre_save as _pre_save

_log = logging.getLogger(__name__)


def _hold_partial_order(sender, instance, update_fields=None, **kwargs):
    """Замовлення з невідправленим залишком посилок не стає «Відправлено» / «Доставлено» — хоч би хто це робив
    (трекінг, DigiKey, підтвердження в магазині, ручний статус відправлення, картка): лишається «В обробці»
    (parcels.hold_status). Без посилок — нічого не змінюється."""
    if not instance.pk or instance.status not in ("shipped", "delivered"):
        return
    if update_fields is not None and "status" not in update_fields:
        return
    try:
        from shipping.services import parcels
        old = sender.objects.filter(pk=instance.pk).values("status", "delivered_at").first()
        if not old or old["status"] == instance.status:
            return
        held = parcels.hold_status(instance, instance.status, current=old["status"])
        if held != instance.status:
            instance._parcels_held = instance.status
            instance.status = held
            if held != "delivered":
                instance.delivered_at = old["delivered_at"]
    except Exception:
        _log.exception("Parcels: status hold failed for order %s", instance.pk)


@receiver(_post_save, sender=Shipment)
def _parcel_shipment_saved(sender, instance, created, **kwargs):
    """Посилка створена або змінила стан → статус замовлення й склад (parcels.sync). Нове відправлення
    замовлення, яке вже має посилки, без вказаного вмісту (швидке створення, «Відправка сьогодні») — залишок,
    який ще не лежить в інших посилках."""
    order_id = instance.order_id
    if not order_id or getattr(instance, "direction", "out") == "in":
        return
    old_status = getattr(instance, "_old_ship_status", None)
    if not created and old_status == instance.status:
        return
    try:
        from shipping.services import parcels
        order = instance.order
        if not parcels.has_parcels(order):
            return
        if created and not instance.lines.exists():
            todo = {r["pk"]: r["to_pack"] for r in parcels.progress(order, exclude=instance.pk).lines}
            if any(q > 0 for q in todo.values()):
                parcels.set_contents(instance, todo)
                return
        parcels.sync(order)
    except Exception:
        _log.exception("Parcels: shipment %s change not applied to order %s", instance.pk, order_id)


def _parcel_line_saved(sender, instance, created, **kwargs):
    """Змінилась кількість позиції замовлення з посилками (DigiKey скасував частину, людина виправила) — статус
    замовлення за посилками (склад перераховує сигнал складу)."""
    if created:
        return
    try:
        from shipping.services import parcels
        if parcels.has_parcels(instance.order):
            parcels.settle(instance.order)
    except Exception:
        _log.exception("Parcels: order line %s change not applied", instance.pk)


def connect_parcel_signals():
    from sales.models import SalesOrder, SalesOrderLine
    _pre_save.connect(_hold_partial_order, sender=SalesOrder, dispatch_uid="parcels_hold_status", weak=False)
    _post_save.connect(_parcel_line_saved, sender=SalesOrderLine, dispatch_uid="parcels_line_saved", weak=False)


connect_parcel_signals()


# ── 📦 Запас коробок (shipping/services/packaging_stock.py) ────────────────────

from django.db.models.signals import post_delete as _post_delete

from .models import ShipmentPackage


def _sync_boxes_later(shipment_pk):
    """Після commit (статус і коробки вже записані): списати / повернути коробки відправлення."""
    from django.db import transaction

    def run():
        try:
            from shipping.services import packaging_stock
            if packaging_stock.tracked_exist():
                s = Shipment.objects.filter(pk=shipment_pk).first()
                if s is not None:
                    packaging_stock.sync_shipment(s)
        except Exception:
            _log.exception("Packaging stock: shipment %s not synced", shipment_pk)
    transaction.on_commit(run)


@receiver(_post_save, sender=Shipment)
def _packaging_stock_shipment(sender, instance, created, **kwargs):
    """Оформили / скасували / змінили стан посилки від нас — коробки зі складу коробок."""
    from shipping.services.packaging_stock import USED
    old = getattr(instance, "_old_ship_status", None)
    if getattr(instance, "direction", "out") == "in":
        return
    if instance.status in USED or old in USED:
        _sync_boxes_later(instance.pk)


@receiver(_post_save, sender=ShipmentPackage)
@receiver(_post_delete, sender=ShipmentPackage)
def _packaging_stock_package(sender, instance, **kwargs):
    """Змінились коробки вже оформленої посилки — перерахувати списане."""
    from shipping.services.packaging_stock import USED
    s = Shipment.objects.filter(pk=instance.shipment_id).values("status", "direction").first()
    if s and s["direction"] != "in" and s["status"] in USED:
        _sync_boxes_later(instance.shipment_id)
