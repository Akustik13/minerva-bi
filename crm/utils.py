"""
crm/utils.py — Утиліти для автоматичної синхронізації клієнтів
"""
from django.db.models import Q


def sync_customer_from_order(order):
    """
    Створює або оновлює клієнта в CRM на основі замовлення.
    
    Args:
        order: SalesOrder instance
        
    Returns:
        Customer instance або None
    """
    from crm.models import Customer
    
    # Мінімальні вимоги — email або ім'я
    if not order.email and not order.client:
        return None
    
    # Шукаємо існуючого клієнта
    customer = None
    
    if order.email:
        customer = Customer.objects.filter(email__iexact=order.email).first()
    
    if not customer and order.client:
        customer = Customer.objects.filter(name__iexact=order.client).first()
    
    # Якщо знайшли — оновлюємо дані
    if customer:
        updated = False
        
        # Оновлюємо email якщо його не було
        if order.email and not customer.email:
            customer.email = order.email
            updated = True
        
        # Оновлюємо телефон якщо його не було
        if order.phone and not customer.phone:
            customer.phone = order.phone
            updated = True
        
        # Оновлюємо країну з shipping_region
        if order.shipping_region and not customer.country:
            customer.country = order.shipping_region
            updated = True
        
        if updated:
            customer.save()
        
        return customer
    
    # Створюємо нового клієнта
    customer_data = {
        'name': order.client or order.email.split('@')[0],
        'email': order.email or '',
        'phone': order.phone or '',
        'country': order.shipping_region or '',
        'company': order.client if '@' not in (order.client or '') else '',
    }
    
    customer = Customer.objects.create(**customer_data)
    return customer


def bulk_sync_all_customers():
    """
    Синхронізує ВСІ замовлення з CRM.
    Створює клієнтів для всіх замовлень де їх немає.
    
    Returns:
        dict з статистикою
    """
    from sales.models import SalesOrder
    
    stats = {
        'processed': 0,
        'created': 0,
        'updated': 0,
        'skipped': 0,
    }
    
    orders = SalesOrder.objects.all()
    
    for order in orders:
        stats['processed'] += 1
        
        if not order.email and not order.client:
            stats['skipped'] += 1
            continue
        
        # Перевіряємо чи вже є клієнт
        from crm.models import Customer
        existing = None
        
        if order.email:
            existing = Customer.objects.filter(email__iexact=order.email).first()
        if not existing and order.client:
            existing = Customer.objects.filter(name__iexact=order.client).first()
        
        if existing:
            stats['skipped'] += 1
            continue
        
        # Створюємо
        customer = sync_customer_from_order(order)
        if customer:
            stats['created'] += 1

    return stats


# ── Замовлення клієнта: постійний чи новий (фільтр «Тип клієнта», колонка «Тип»), RFM ─────────────────────
#
# Рахуються лише продажі — sales.revenue.sale_orders(): тип SALE, не скасовані, з «Affects stock» (як на Дашборді).
# Запити пропозицій (QUOTE), зразки, гарантія, переміщення, скасовані й замовлення тестового магазину
# (у них «Affects stock» вимкнено) — не замовлення клієнта ні для «Тип», ні для RFM.
# Сума (M) — sales.revenue: total_price замовлення (порожня — рядки) у валюті Minerva за курсами магазину;
# валюта без курсу в M не додається (видно в Money.missing).

REPEAT_MIN_ORDERS = 2   # від 2 замовлень — постійний (як F ≥ 2 в RFM); 0–1 — новий

# Межі балів RFM (від більшого): R — днів від останнього замовлення, F — замовлень, M — сума у валюті Minerva
RFM_RECENCY_DAYS = ((30, 5), (60, 4), (120, 3), (365, 2))
RFM_FREQUENCY = ((10, 5), (5, 4), (3, 3), (2, 2))
RFM_MONETARY = ((5000, 5), (1000, 4), (500, 3), (100, 2))


def _norm_email(email) -> str:
    return (email or "").strip().lower()


def _matching(customers):
    """Як замовлення веде на клієнта (так само, як у Продажах): спершу ключ (SalesOrder.customer_key =
    Customer.external_key), а замовлення без ключа клієнта CRM (порожній ключ або клієнта з таким ключем уже
    немає) — за email. → (rows [(pk, ключ, email)], продажі з ключем, продажі за email з анотацією email_norm)."""
    from django.db.models.functions import Lower, Trim
    from crm.models import Customer
    from sales.revenue import sale_orders

    if customers is None:
        rows = list(Customer.objects.values_list("pk", "external_key", "email"))
    else:
        rows = [(c.pk, c.external_key, c.email) for c in customers]
    orders = sale_orders().order_by()
    keyed = orders.exclude(customer_key="")
    if customers is not None:
        keyed = keyed.filter(customer_key__in={k for _, k, _ in rows if k})
    emails = {_norm_email(e) for _, _, e in rows if _norm_email(e)}
    loose = None
    if emails:
        loose = (orders.exclude(email="")
                 .exclude(customer_key__in=Customer.objects.exclude(external_key="").values("external_key"))
                 .annotate(email_norm=Lower(Trim("email"))))
        if customers is not None:
            loose = loose.filter(email_norm__in=emails)
    return rows, keyed, loose


def order_counts(customers=None) -> dict:
    """{pk клієнта: кількість його продажів} (див. _matching).

    customers — клієнти (список / queryset) або None — усі (фільтр списку). Два групові запити замість
    підзапиту на кожен рядок."""
    from django.db.models import Count

    rows, keyed, loose = _matching(customers)
    if not rows:
        return {}
    by_key = dict(keyed.values_list("customer_key").annotate(n=Count("id")))
    by_email = dict(loose.values_list("email_norm").annotate(n=Count("id"))) if loose is not None else {}
    counts = {}
    for pk, key, email in rows:
        email = _norm_email(email)
        counts[pk] = by_key.get(key, 0) + (by_email.get(email, 0) if email else 0)
    return counts


def is_repeat(order_count) -> bool:
    return (order_count or 0) >= REPEAT_MIN_ORDERS


def repeat_customer_ids() -> set:
    """Постійні клієнти (від REPEAT_MIN_ORDERS замовлень) — для фільтра «Тип клієнта»."""
    return {pk for pk, n in order_counts().items() if is_repeat(n)}


class OrderStats:
    """Продажі клієнта: кількість, остання дата, виручка (Money, валюта Minerva), середній чек."""

    __slots__ = ("count", "last", "revenue", "known")

    def __init__(self, home: str):
        from sales.revenue import Money
        self.count = 0
        self.last = None
        self.revenue = Money(home)
        self.known = 0          # замовлень у валютах з курсом — для середнього чека

    def add_orders(self, n, last, known):
        self.count += n
        self.known += known
        if last and (self.last is None or last > self.last):
            self.last = last

    @property
    def avg(self):
        from decimal import Decimal
        if not self.known or not self.revenue.total:
            return Decimal("0")
        return (self.revenue.total / self.known).quantize(Decimal("0.01"))


def order_stats(customers=None, rates=None) -> dict:
    """{pk клієнта: OrderStats} — кількома груповими запитами на всіх (сторінка списку, фільтр RFM)."""
    from django.db.models import Count, Max
    from django.db.models.functions import Lower, Trim
    from sales import revenue as rv

    rates = rates or rv.Rates()
    rows, keyed, loose = _matching(customers)
    if not rows:
        return {}

    def grouped(qs, field):
        out = {}
        for r in qs.values(field, "currency").annotate(n=Count("id"), last=Max("order_date")):
            o = out.setdefault(r[field], [0, None, 0])
            o[0] += r["n"]
            o[2] += r["n"] if rates.rate(r["currency"]) is not None else 0
            if r["last"] and (o[1] is None or r["last"] > o[1]):
                o[1] = r["last"]
        return out

    key_orders = grouped(keyed, "customer_key")
    key_money = rv.revenue(keyed, by=["customer_key"], rates=rates)
    email_orders, email_money = {}, {}
    if loose is not None:
        email_orders = grouped(loose, "email_norm")
        email_money = rv.revenue(loose, by={"email_norm": lambda p: Lower(Trim(p + "email"))}, rates=rates)

    stats = {}
    for pk, key, email in rows:
        email = _norm_email(email)
        s = OrderStats(rates.home)
        parts = [(key_orders.get(key), key_money.get((key,)))] if key else []
        if email:
            parts.append((email_orders.get(email), email_money.get((email,))))
        for orders, money in parts:
            if orders:
                s.add_orders(orders[0], orders[1], orders[2])
            if money:
                s.revenue = s.revenue + money
        stats[pk] = s
    return stats


def prefetch_stats(customers) -> list:
    """Рахує OrderStats для клієнтів сторінки одним махом (customer._order_stats) — RFM, «Тип», виручка."""
    customers = list(customers)
    stats = order_stats(customers)
    for c in customers:
        c._order_stats = stats.get(c.pk)
    return customers


def _score(value, bounds, at_least=True) -> int:
    for bound, points in bounds:
        if (value >= bound) if at_least else (value <= bound):
            return points
    return 1


def rfm_segment(r, f, m) -> str:
    if r >= 4 and f >= 4 and m >= 4:
        return "🏆 Champions"
    elif r >= 3 and f >= 3:
        return "💎 Loyal"
    elif r >= 4:
        return "⭐ Potential"
    elif f >= 4:
        return "🔄 Regular"
    elif r <= 2 and f >= 2:
        return "😴 At Risk"
    elif r <= 2 and f <= 2:
        return "💤 Hibernating"
    else:
        return "🆕 New"


def rfm_from(stats, today=None) -> dict:
    """RFM з OrderStats: R — давність останнього продажу, F — кількість продажів, M — сума у валюті Minerva."""
    from django.utils import timezone

    today = today or timezone.localdate()
    last = stats.last if stats else None
    if last is None:
        r = 1
    else:
        if hasattr(last, "date") and callable(last.date):
            last = last.date()
        r = _score((today - last).days, RFM_RECENCY_DAYS, at_least=False)
    f = _score(stats.count if stats else 0, RFM_FREQUENCY)
    m = _score(float(stats.revenue.total) if stats else 0.0, RFM_MONETARY)
    return {"R": r, "F": f, "M": m, "score": r + f + m, "segment": rfm_segment(r, f, m)}


def rfm_score(customer) -> dict:
    """RFM клієнта (Customer.rfm_score() — сюди)."""
    return rfm_from(customer.order_stats())


def rfm_scores(customers) -> dict:
    """{pk: RFM} для багатьох клієнтів — груповими запитами (фільтр «RFM Сегмент»)."""
    stats = order_stats(list(customers))
    return {pk: rfm_from(s) for pk, s in stats.items()}
