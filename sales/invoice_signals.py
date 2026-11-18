"""🧾 Рахунок покупцю автоматично (InvoiceSettings.auto_send) — коли замовлення створено або стало оплаченим.

Сигнали лише ставлять `invoice_service.start_auto_send` після commit (тоді вже є ShopOrder, позиції й адреса з
сайту); що надсилати й чи взагалі — вирішує `invoice_service.auto_send` (вимкнено, не з сайту, онлайн-оплата ще не
оплачена, рахунок уже надіслано — нічого).
"""
from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import SalesOrder


@receiver(pre_save, sender=SalesOrder, dispatch_uid="invoice_auto_send_pre")
def _remember_payment_status(sender, instance, raw=False, update_fields=None, **kwargs):
    instance._invoice_paid_before = None
    if raw or not instance.pk or (update_fields is not None and "payment_status" not in update_fields):
        return
    instance._invoice_paid_before = (
        SalesOrder.objects.filter(pk=instance.pk).values_list("payment_status", flat=True).first() == "paid")


@receiver(post_save, sender=SalesOrder, dispatch_uid="invoice_auto_send_post")
def _auto_send_invoice(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    became_paid = instance.payment_status == "paid" and getattr(instance, "_invoice_paid_before", None) is False
    if created or became_paid:
        from . import invoice_service
        pk = instance.pk
        transaction.on_commit(lambda: invoice_service.start_auto_send(pk))
