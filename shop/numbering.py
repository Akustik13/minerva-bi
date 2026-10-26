"""Kundennummer і номери документів веб-магазину.

- Kundennummer — одна на клієнта CRM (B2B — компанія), спільна для всіх магазинів; перша — ShopSettings.
  customer_number_start (31728), кожна наступна +1. Видається з першим замовленням з сайту або з підтвердженням
  реєстрації в кабінеті (акаунт без замовлень отримує номер, який перейде до клієнта CRM з першим замовленням).
- Номери документів — <вид>-<Kundennummer>-NNN, NNN рахується окремо для кожного виду й клієнта:
  PO — Bestellnummer (замовлення з сайту, видає Minerva при створенні), OC — Auftragsbestätigung,
  CI — Rechnung (Commercial Invoice), PI — Proforma-Rechnung, PL — Lieferschein (Packing List).
  Номер документа замовлення видається один раз (ShopOrder.doc_numbers) — повторне створення документа бере той
  самий. Діє лише для замовлень з веб-магазинів (є ShopOrder); решта замовлень — як раніше.
- Тестовий магазин (Shop.is_test) — окрема нумерація без Kundennummer: TEST-PO-0001, TEST-OC-0001 …, акаунти
  TEST-1 (лічильники Shop.test_counters); справжні номери не витрачаються.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Max

from .models import CustomerNumber, Shop, ShopCustomer, ShopOrder, ShopSettings

KINDS = {
    "PO": "Bestellnummer",
    "OC": "Auftragsbestätigung",
    "CI": "Rechnung",
    "PI": "Proforma-Rechnung",
    "PL": "Lieferschein",
}


def _allocate(crm_customer=None) -> CustomerNumber:
    """Новий номер (або наявний номер клієнта CRM). Видача серіалізована блокуванням рядка налаштувань."""
    ShopSettings.get()
    with transaction.atomic():
        start = ShopSettings.objects.select_for_update().get(pk=1).customer_number_start
        if crm_customer is not None:
            existing = CustomerNumber.objects.filter(crm_customer=crm_customer).first()
            if existing is not None:
                return existing
        top = CustomerNumber.objects.aggregate(m=Max("number"))["m"] or 0
        return CustomerNumber.objects.create(number=max(top + 1, start), crm_customer=crm_customer)


def crm_of(order):
    """Клієнт CRM замовлення (сигнал crm ставить customer_key оновленням — читаємо з бази)."""
    from crm.models import Customer
    from sales.models import SalesOrder
    key = SalesOrder.objects.filter(pk=order.pk).values_list("customer_key", flat=True).first()
    return Customer.objects.filter(external_key=key).first() if key else None


def for_crm(customer) -> CustomerNumber | None:
    if customer is None:
        return None
    found = CustomerNumber.objects.filter(crm_customer=customer).first()
    return found or _allocate(customer)


def _test_next(shop, kind: str) -> int:
    """Тестовий магазин: наступний номер виду (Shop.test_counters, під блокуванням рядка магазину)."""
    with transaction.atomic():
        counters = dict(Shop.objects.select_for_update().values_list("test_counters", flat=True).get(pk=shop.pk) or {})
        counters[kind] = int(counters.get(kind) or 0) + 1
        Shop.objects.filter(pk=shop.pk).update(test_counters=counters)
    return counters[kind]


def test_number(shop, kind: str) -> str:
    return f"TEST-{kind}-{_test_next(shop, kind):04d}"


def display_number(account: ShopCustomer):
    """Kundennummer для кабінету: число (справжня) або «TEST-N» (тестовий магазин)."""
    if account.shop.is_test:
        if account.test_number is None:
            n = _test_next(account.shop, "KN")
            ShopCustomer.objects.filter(pk=account.pk, test_number__isnull=True).update(test_number=n)
            account.test_number = ShopCustomer.objects.values_list("test_number", flat=True).get(pk=account.pk)
        return f"TEST-{account.test_number}"
    return for_account(account).number


def for_account(account: ShopCustomer) -> CustomerNumber:
    """Kundennummer акаунта кабінету: свій, клієнта CRM або новий."""
    sync_account(account)
    if account.customer_number_id is None:
        number = for_crm(account.crm_customer) if account.crm_customer_id else _allocate()
        ShopCustomer.objects.filter(pk=account.pk).update(customer_number=number)
        account.customer_number = number
    return account.customer_number


def sync_account(account: ShopCustomer) -> None:
    """Акаунт отримав клієнта CRM: номер клієнта має пріоритет; номер акаунта без клієнта переходить до клієнта."""
    if not account.crm_customer_id or account.shop.is_test:   # тестовий магазин — без справжніх номерів
        return
    crm_number = CustomerNumber.objects.filter(crm_customer_id=account.crm_customer_id).first()
    own = account.customer_number
    if crm_number is None and own is not None and own.crm_customer_id is None:
        CustomerNumber.objects.filter(pk=own.pk, crm_customer__isnull=True).update(crm_customer=account.crm_customer_id)
        own.crm_customer_id = account.crm_customer_id
        return
    if crm_number is not None and account.customer_number_id != crm_number.pk:
        ShopCustomer.objects.filter(pk=account.pk).update(customer_number=crm_number)
        account.customer_number = crm_number


def _next(number: CustomerNumber, kind: str) -> str:
    with transaction.atomic():
        row = CustomerNumber.objects.select_for_update().get(pk=number.pk)
        counters = dict(row.counters or {})
        counters[kind] = int(counters.get(kind) or 0) + 1
        row.counters = counters
        row.save(update_fields=["counters"])
    return f"{kind}-{row.number}-{counters[kind]:03d}"


def assign_order_number(order, shop, site_ref: str) -> str:
    """Нове замовлення з сайту (у транзакції створення, до рядків — ключі складу беруть номер замовлення):
    ShopOrder з номером сайту (дубль → IntegrityError) і номер PO-<Kundennummer>-NNN.
    Без клієнта CRM (немає email і назви) лишається номер сайту."""
    from sales.models import SalesOrder
    ShopOrder.objects.create(order=order, shop=shop, site_ref=site_ref[:64])
    if shop.is_test:
        new_number = lambda: test_number(shop, "PO")  # noqa: E731
    else:
        number = for_crm(crm_of(order))
        if number is None:
            return order.order_number
        new_number = lambda: _next(number, "PO")  # noqa: E731
    new = new_number()
    while SalesOrder.objects.filter(source=order.source, order_number=new).exclude(pk=order.pk).exists():
        new = new_number()   # номер зайнятий (лічильник змінювали вручну) — наступний
    SalesOrder.objects.filter(pk=order.pk).update(order_number=new)
    order.order_number = new
    return new


def find_by_site_ref(shop, site_ref: str):
    info = ShopOrder.objects.filter(shop=shop, site_ref=site_ref).select_related("order").first() if site_ref else None
    return info.order if info else None


def doc_number(order, kind: str) -> str:
    """Номер документа замовлення магазину: виданий раніше або наступний для клієнта; '' — не замовлення магазину
    (тоді документ нумерується як раніше)."""
    assert kind in KINDS
    info = ShopOrder.objects.filter(order=order).select_related("shop").first() if getattr(order, "pk", None) else None
    if info is None:
        return ""
    if info.doc_numbers.get(kind):
        return info.doc_numbers[kind]
    if info.shop.is_test:
        issue = lambda: test_number(info.shop, kind)  # noqa: E731
    else:
        number = for_crm(crm_of(order))
        if number is None:
            return ""
        issue = lambda: _next(number, kind)  # noqa: E731
    with transaction.atomic():
        info = ShopOrder.objects.select_for_update().get(pk=info.pk)
        if info.doc_numbers.get(kind):
            return info.doc_numbers[kind]
        value = issue()
        info.doc_numbers = {**info.doc_numbers, kind: value}
        info.save(update_fields=["doc_numbers"])
    return value

