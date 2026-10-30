"""Номери EU Invoice (`SalesOrder.eu_invoice_number`): видає сервер, кожному замовленню — один раз.

Сам PDF — sales/eu_invoice_pdf.py; тут лише номери, без reportlab.
"""
from django.db import transaction
from django.db.models import Max


def get_next_invoice_number() -> int:
    """Лише підказка для форми — нічого не резервує; номер видає `assign_invoice_number`."""
    from sales.models import SalesOrder
    result = SalesOrder.objects.aggregate(m=Max("eu_invoice_number"))["m"]
    return (result or 0) + 1


class InvoiceNumberTaken(ValueError):
    """Номер EU Invoice уже належить іншому замовленню."""

    def __init__(self, number: int, order):
        self.number, self.order = number, order
        super().__init__(f"Номер рахунку {number} уже має замовлення {order.order_number or order.pk} — "
                         f"номери рахунків не повторюються. Залиште поле порожнім: Minerva видасть наступний вільний.")


def assign_invoice_number(order, requested=None) -> int:
    """Номер EU Invoice для замовлення.

    Під блокуванням рядка `SalesSettings` (pk=1): два одночасні запити видають номери по черзі, тож
    «максимум + 1» не дістанеться обом. Замовлення вже має номер — повторна генерація бере його ж
    (`requested` виданого номера не змінює). Номер з форми (`requested`) приймається лише вільний,
    інакше `InvoiceNumberTaken`. Унікального обмеження в БД немає: на робочій базі можуть бути давні
    повтори, а міграція з ним зупинила б запуск сайту.
    """
    from sales.models import SalesOrder, SalesSettings

    SalesSettings.get()                                           # рядок-замок має існувати
    with transaction.atomic():
        SalesSettings.objects.select_for_update().get(pk=1)
        locked = SalesOrder.objects.select_for_update().get(pk=order.pk)
        if locked.eu_invoice_number:
            number = locked.eu_invoice_number
        elif requested:
            number = int(requested)
            other = (SalesOrder.objects.filter(eu_invoice_number=number)
                     .exclude(pk=order.pk).only("pk", "order_number").first())
            if other:
                raise InvoiceNumberTaken(number, other)
        else:
            number = (SalesOrder.objects.aggregate(m=Max("eu_invoice_number"))["m"] or 0) + 1
        if locked.eu_invoice_number != number:
            SalesOrder.objects.filter(pk=order.pk).update(eu_invoice_number=number)   # без сигналів замовлення
    order.eu_invoice_number = number
    return number
