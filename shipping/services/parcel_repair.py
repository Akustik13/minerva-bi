"""
shipping/services/parcel_repair.py — посилки одного замовлення з однаковим трек-номером.

Так ставалося після «📦 Відстежувати» в картці замовлення (до 11.2026): номер першої посилки записувався в
найновіше відправлення. Справжній номер посилки — те, що перевізник повернув при оформленні:
carrier_shipment_id або номер у назві файлу етикетки (ups_<pk>_<номер>.pdf, fedex_<pk>_<номер>.pdf).
"""
import re
from collections import defaultdict

from django.db import transaction

_LABEL_RE = re.compile(r"(?:ups|fedex|dhl)_(\d+)_([A-Za-z0-9]{8,})\.(?:pdf|gif|png|zpl)$", re.I)


def own_number(shipment) -> tuple:
    """(номер, звідки) — що перевізник видав саме цьому відправленню, або ('', '')."""
    m = _LABEL_RE.search((shipment.label_url or "").rsplit("/", 1)[-1])
    if m and int(m.group(1)) == shipment.pk:
        return m.group(2).upper(), "файл етикетки"
    sid = (shipment.carrier_shipment_id or "").strip().upper()
    if re.fullmatch(r"1Z[A-Z0-9]{16}", sid):
        return sid, "ID відправлення UPS"
    return "", ""


def plan(order=None) -> list:
    """[{order, order_pk, pk, old, new, why}] — посилки з номером, що належить іншій посилці замовлення
    (order — лише це замовлення)."""
    from shipping.models import Shipment

    groups = defaultdict(list)
    qs = (Shipment.objects.exclude(order=None).exclude(tracking_number="")
          .exclude(status=Shipment.Status.CANCELLED).select_related("order"))
    if order is not None:
        qs = qs.filter(order=order)
    for s in qs:
        groups[(s.order_id, s.tracking_number.strip().upper())].append(s)
    rows = []
    for (_order, number), items in groups.items():
        if len(items) < 2:
            continue
        taken = {own_number(s)[0] for s in items}
        for s in sorted(items, key=lambda x: x.pk):
            new, why = own_number(s)
            if new == number:
                continue                        # номер справді його
            if not new and number not in taken:
                if s is min(items, key=lambda x: x.pk):
                    continue                    # ніхто не довів, що номер чужий — перша посилка лишає свій
            rows.append({"order": s.order.order_number, "order_pk": s.order_id, "pk": s.pk,
                         "old": s.tracking_number, "new": new, "why": why})
    return rows


def duplicated(order) -> bool:
    """Чи є в замовленні дві живі посилки з однаковим трек-номером (для кнопки «🔧 Автоперевірка»)."""
    from shipping.models import Shipment

    seen = set()
    for tn in (order.shipments.exclude(status=Shipment.Status.CANCELLED).exclude(tracking_number="")
               .values_list("tracking_number", flat=True)):
        key = tn.strip().upper()
        if key in seen:
            return True
        seen.add(key)
    return False


def apply(rows) -> int:
    """Записати знайдені номери (той самий план, що бачила людина: номер змінюється, лише якщо він досі старий)."""
    from shipping.models import Shipment

    done = 0
    with transaction.atomic():
        for row in rows:
            if row["new"]:
                done += Shipment.objects.filter(pk=row["pk"], tracking_number=row["old"]).update(
                    tracking_number=row["new"])
    return done


def apply_for(order=None, only=None) -> int:
    """Свіжий план (замовлення або все) → лише вибрані людиною посилки `only` (pk), якщо задано."""
    rows = plan(order)
    if only is not None:
        rows = [r for r in rows if r["pk"] in set(only)]
    return apply(rows)
