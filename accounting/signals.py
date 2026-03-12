"""
accounting/signals.py — Автоматичне оновлення статусу Invoice при збереженні Payment.
"""
from datetime import date

from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver


@receiver(post_save, sender="accounting.Payment")
def update_invoice_status_on_save(sender, instance, **kwargs):
    _refresh_invoice_status(instance.invoice)


@receiver(post_delete, sender="accounting.Payment")
def update_invoice_status_on_delete(sender, instance, **kwargs):
    _refresh_invoice_status(instance.invoice)


def _refresh_invoice_status(invoice):
    from accounting.models import Invoice

    # Не чіпаємо скасовані рахунки
    if invoice.status == Invoice.Status.CANCELLED:
        return

    paid = invoice.paid_amount
    total = invoice.total

    if total > 0 and paid >= total:
        new_status = Invoice.Status.PAID
    elif invoice.due_date and invoice.due_date < date.today() and paid < total:
        new_status = Invoice.Status.OVERDUE
    else:
        # Якщо є часткова оплата — залишаємо "sent", якщо не draft
        if invoice.status in (Invoice.Status.DRAFT,):
            return
        new_status = invoice.status

    if new_status != invoice.status:
        Invoice.objects.filter(pk=invoice.pk).update(status=new_status)
