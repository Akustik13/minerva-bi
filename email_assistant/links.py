"""
email_assistant/links.py — зв'язки листа з даними Minerva.

Шукає в темі й тексті листа номери замовлень і трек-номери (UPS 1Z…, DHL, FedEx — будь-які,
що збережені в замовленні чи відправленні) і повертає замовлення з клієнтом і відправленнями.
Так лист від UPS «REF: 102037400 … 1Z0R6B300426098447» показує замовлення digikey:102037400,
клієнта AEVEX і статус відправлення, навіть якщо його переслав колега.
"""
from django.urls import reverse

from email_assistant.crm_link import _TOKEN_RE

MAX_ORDERS = 6


def tokens_in(texts) -> set:
    """Кандидати в номери: слова з літер/цифр, де щонайменше 5 цифр (+ варіант у верхньому регістрі)."""
    tokens = set()
    for text in texts:
        for tok in _TOKEN_RE.findall(text or ''):
            if sum(ch.isdigit() for ch in tok) >= 5:
                tokens.add(tok)
                tokens.add(tok.upper())
    return tokens


def orders_for_tokens(tokens):
    """{order_pk: (SalesOrder, причина)} — за номером замовлення, трек-номером замовлення або відправлення."""
    from sales.models import SalesOrder
    from shipping.models import Shipment

    found = {}
    if not tokens:
        return found
    for order in SalesOrder.objects.filter(order_number__in=tokens)[:MAX_ORDERS]:
        found.setdefault(order.pk, (order, 'номер замовлення'))
    for order in SalesOrder.objects.filter(tracking_number__in=tokens).exclude(tracking_number='')[:MAX_ORDERS]:
        found.setdefault(order.pk, (order, 'трек-номер'))
    for shipment in (Shipment.objects.filter(tracking_number__in=tokens).exclude(tracking_number='')
                     .exclude(order=None).select_related('order')[:MAX_ORDERS]):
        found.setdefault(shipment.order_id, (shipment.order, 'трек-номер'))
    return found


def find_links(texts) -> list:
    """Зв'язки для прев'ю листа: замовлення → клієнт, відправлення (статус, трек)."""
    from shipping.models import Shipment

    found = orders_for_tokens(tokens_in(texts))
    if not found:
        return []
    shipments = {}
    for s in (Shipment.objects.filter(order_id__in=found.keys())
              .select_related('carrier').order_by('-pk')):
        shipments.setdefault(s.order_id, []).append(s)

    links = []
    for order, reason in list(found.values())[:MAX_ORDERS]:
        customer = order.crm_customer
        links.append({
            'order':    order,
            'label':    f'{order.source}:{order.order_number}' if order.source else order.order_number,
            'url':      reverse('admin:sales_salesorder_change', args=[order.pk]),
            'reason':   reason,
            'customer': customer,
            'customer_url': reverse('admin:crm_customer_change', args=[customer.pk]) if customer else '',
            'shipments': [{
                'tracking': s.tracking_number,
                'status':   s.get_status_display(),
                'carrier':  str(s.carrier) if s.carrier_id else '',
                'url':      reverse('admin:shipping_shipment_change', args=[s.pk]),
            } for s in shipments.get(order.pk, [])[:3]],
        })
    return links


def find_order_by_tracking(tokens):
    """Замовлення за трек-номером (для прив'язки листа до клієнта в CRM)."""
    for order, reason in orders_for_tokens(tokens).values():
        if reason == 'трек-номер':
            return order
    return None

