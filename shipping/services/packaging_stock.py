"""Запас коробок (пакувальних матеріалів) — одна логіка для картки коробки, відправлень і повідомлень.

- Облік — лише для коробок з «Вести облік на складі» (`PackagingMaterial.track_stock`); залишок — `stock_qty`,
  кожна зміна — рядок `PackagingMovement` з балансом після неї.
- Відправлення від нас, оформлене (`USED`: передано перевізнику, етикетка, в дорозі, доставлено), списує свої
  коробки: рядки «Коробки» (кожен × «однакових коробок») або розміри самого відправлення; яка це коробка — за
  розмірами (±1 см у будь-якій орієнтації, `packaging.material_for` серед активних). Скасоване, з помилкою чи знову
  чернетка — повертає. `sync_shipment` рахує, скільки має бути списано, і пише лише різницю з уже списаним цим
  відправленням — повтор нічого не змінює.
- Відправлення, оформлене раніше, ніж почали вести облік коробки («Облік з»), її не списує: воно вже враховане в
  кількості, яку людина вписала.
- Прихід — `receive`, інвентаризація («фактично є») — `count`.
- Мало (`PackagingMaterial.is_low`: залишок ≤ «Мінімальний запас») — `check_low` один раз повідомляє (кому й куди —
  «🗺 Хто що отримує», вид packaging; ✉️ / 💬 — «Налаштування доставки»), знову — `remind_due` (cron) через
  «Нагадувати кожні N днів»; запас знову вище мінімуму — позначка знімається, наступного разу попередимо знову.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

USED = ("submitted", "label_ready", "in_transit", "delivered")


def tracked_exist() -> bool:
    from shipping.models import PackagingMaterial
    return PackagingMaterial.objects.filter(track_stock=True).exists()


def _when(shipment):
    return shipment.submitted_at or shipment.created_at


def needed(shipment, pool=None) -> dict:
    """{коробка pk: скільки списати} для відправлення зараз (коробки з обліком)."""
    from shipping.services import packaging
    if getattr(shipment, "direction", "out") == "in" or shipment.status not in USED:
        return {}
    out = {}
    when = _when(shipment)
    for box in packaging.shipment_boxes(shipment, pool):
        m = box.packaging
        if m is None or not m.track_stock:
            continue
        if m.stock_since and when and when < m.stock_since:
            continue
        out[m.pk] = out.get(m.pk, 0) + max(1, int(box.qty or 1))
    return out


def _move(material_pk, delta, kind, *, shipment=None, note="", user=None):
    """Змінити залишок на delta (під блокуванням рядка коробки) і записати рух. → рух."""
    from shipping.models import PackagingMaterial, PackagingMovement
    with transaction.atomic():
        m = PackagingMaterial.objects.select_for_update().get(pk=material_pk)
        balance = m.stock_qty + delta
        PackagingMaterial.objects.filter(pk=m.pk).update(stock_qty=balance)
        mv = PackagingMovement.objects.create(material=m, kind=kind, qty=delta, balance=balance,
                                              shipment=shipment, note=note[:255], user=user)
        transaction.on_commit(lambda: check_low(material_pk))
    return mv


def sync_shipment(shipment) -> list:
    """Списати / повернути коробки відправлення: лише різниця між потрібним (`needed`) і вже списаним ним.
    → [рухи]."""
    from shipping.models import PackagingMaterial, PackagingMovement, Shipment
    done = []
    with transaction.atomic():
        s = Shipment.objects.select_for_update().filter(pk=shipment.pk).first()
        if s is None:
            return done
        want = needed(s)
        have = {r["material"]: -(r["q"] or 0) for r in PackagingMovement.objects.filter(shipment=s)
                .values("material").annotate(q=Sum("qty"))}
        tracked = set(PackagingMaterial.objects.filter(pk__in=set(want) | set(have), track_stock=True)
                      .values_list("pk", flat=True))
        label = s.tracking_number or f"#{s.pk}"
        order = getattr(s.order, "order_number", "") if s.order_id else ""
        for pk in sorted(tracked):
            more = want.get(pk, 0) - have.get(pk, 0)
            if not more:
                continue
            if more > 0:
                note = f"Відправлення {label}" + (f" · замовлення {order}" if order else "")
                done.append(_move(pk, -more, "shipment", shipment=s, note=note))
            else:
                note = f"Відправлення {label}: {s.get_status_display().lower()}"
                done.append(_move(pk, -more, "return", shipment=s, note=note))
    return done


def receive(material, qty: int, *, user=None, note="") -> object:
    """📥 Прийшло (купили) qty коробок."""
    qty = int(qty)
    if qty <= 0:
        raise ValueError("Кількість має бути більшою за 0")
    return _move(material.pk, qty, "purchase", note=note or "Прихід", user=user)


def count(material, actual: int, *, user=None, note="") -> object | None:
    """🧮 Інвентаризація: фактично є actual коробок → рух на різницю (None — уже стільки)."""
    from shipping.models import PackagingMaterial
    actual = int(actual)
    if actual < 0:
        raise ValueError("Кількість не може бути від'ємною")
    current = PackagingMaterial.objects.filter(pk=material.pk).values_list("stock_qty", flat=True).first() or 0
    if actual == current:
        return None
    return _move(material.pk, actual - current, "adjust", note=note or f"Інвентаризація: є {actual}", user=user)


# ── Мало коробок ──────────────────────────────────────────────────────────────

def low_materials():
    from shipping.models import PackagingMaterial
    return [m for m in PackagingMaterial.objects.filter(track_stock=True, min_stock__isnull=False).order_by("name")
            if m.is_low]


def check_low(material_pk) -> bool:
    """Після руху: мало — повідомити (один раз), вистачає — зняти позначку. → True, якщо надіслали."""
    from shipping.models import PackagingMaterial, ShippingSettings
    m = PackagingMaterial.objects.filter(pk=material_pk).first()
    if m is None:
        return False
    if not m.is_low:
        if m.low_notified_at:
            PackagingMaterial.objects.filter(pk=m.pk).update(low_notified_at=None)
        return False
    if m.low_notified_at or not ShippingSettings.get().packaging_alert_enabled:
        return False
    claimed = PackagingMaterial.objects.filter(pk=m.pk, low_notified_at__isnull=True).update(
        low_notified_at=timezone.now())
    if not claimed:
        return False
    notify([m])
    return True


def remind_due(now=None) -> list:
    """Cron: коробки, яких досі мало, а останнє повідомлення старіше за «Нагадувати кожні N днів». → [коробки]."""
    from shipping.models import PackagingMaterial, ShippingSettings
    s = ShippingSettings.get()
    if not s.packaging_alert_enabled or not s.packaging_alert_repeat_days:
        return []
    now = now or timezone.now()
    edge = now - timedelta(days=s.packaging_alert_repeat_days)
    due = []
    for m in low_materials():
        if m.low_notified_at and m.low_notified_at > edge:
            continue
        if PackagingMaterial.objects.filter(pk=m.pk, low_notified_at=m.low_notified_at).update(low_notified_at=now):
            due.append(m)
    if due:
        notify(due)
    return due


def notify(materials) -> list:
    """Надіслати «📦 Мало коробок» (лист і / або Telegram). → куди надіслано ['лист', 'Telegram']."""
    from dashboard.notifications import notify_packaging_low
    from shipping.models import ShippingSettings
    s = ShippingSettings.get()
    return notify_packaging_low(list(materials), email=s.packaging_alert_email, telegram=s.packaging_alert_telegram)


def send_now() -> tuple:
    """«🔔 Надіслати зараз»: усі коробки, яких мало, незалежно від того, чи вже попереджали. → (коробки, куди)."""
    from shipping.models import PackagingMaterial
    low = low_materials()
    if not low:
        return [], []
    sent = notify(low)
    PackagingMaterial.objects.filter(pk__in=[m.pk for m in low]).update(low_notified_at=timezone.now())
    return low, sent
