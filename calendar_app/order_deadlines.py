"""📦 Дедлайни замовлень у календарі — подія ↔ замовлення і правило, що в календарі лишати.

Події «⏰ Дедлайн» з листів (AI з листа «Minerva: Нове замовлення …», «🔎 Терміни з пошти», правило пошти
«Додати в календар») самі не знають свого замовлення, тож відправлене замовлення лишалось у календарі назавжди.

• `link(event)` — замовлення події: номер у назві чи описі події → лист події (подія хронології CRM з
  `related_order_id`, номер у темі / тексті / вкладенні — `email_assistant.crm_link.find_order`). Результат —
  `CalendarEvent.sales_order` і `order_state` ('' — ще не перевірено, none — замовлення немає, open, closed);
• `apply_rule(events)` — за `CalendarSettings.order_deadlines` людини: open (за замовчуванням) і critical —
  замовлення відправили / скасували (`sales.revenue.to_ship` його вже не бачить) → подія ✓ виконана й closed
  (з календаря й підписки зникає), повернули в роботу → знову відкрита; all — як раніше, подія не змінюється;
• `visible(qs, user)` — для critical у календарі лише дедлайни, до яких ≤ `order_deadline_days` днів, і прострочені;
• `feed_q()` — у підписку ICS закриті не йдуть.

Хто викликає: сигнали `calendar_app/signals.py` (нова подія, збережене замовлення), `sweep` — cron `calendar_sync`
(замовлення, змінені `.update()` без сигналу, і старі події) і «🧹 Перевірити зараз» у налаштуваннях календаря.
"""
import datetime
import logging

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

SWEEP_LINK_LIMIT = 300     # стільки ще не перевірених подій зв'язує один прохід
SWEEP_EVERY = 600          # cron (`calendar_sync --if-due` щохвилини) дочищає не частіше, с


def _models():
    from calendar_app.models import CalendarEvent, CalendarSettings
    return CalendarEvent, CalendarSettings


def find_order(event):
    """Замовлення, про яке подія: номер у назві / описі → лист події. None — не знайдено."""
    from email_assistant.crm_link import find_order as in_text
    from sales.models import SalesOrder
    if event.sales_order_id:
        return event.sales_order
    order = in_text(event.title or '', event.description or '')
    if order is not None:
        return order
    em = event.email_message
    if em is None:
        return None
    from crm.models import CustomerTimeline
    oid = (CustomerTimeline.objects.filter(email_message=em, related_order_id__isnull=False)
           .values_list('related_order_id', flat=True).first())
    if oid:
        order = SalesOrder.objects.filter(pk=oid).first()
        if order is not None:
            return order
    return in_text(em.subject or '', em.body_text or '', message=em)


def open_ids(order_ids) -> set:
    """Які з цих замовлень ще чекають відправки (як дашборд і звіти — `sales.revenue.to_ship`)."""
    from sales.models import SalesOrder
    from sales.revenue import to_ship
    ids = {i for i in order_ids if i}
    if not ids:
        return set()
    return set(to_ship(SalesOrder.objects.filter(pk__in=ids)).values_list('pk', flat=True))


def link(event) -> dict:
    """Зв'язати подію-дедлайн із замовленням (раз; '' → open / none) і застосувати правило людини.
    → {'closed': n, 'reopened': n} (як apply_rule)."""
    CalendarEvent, _ = _models()
    if event.event_type != CalendarEvent.TYPE_DEADLINE:
        return {'closed': 0, 'reopened': 0}
    try:
        order = find_order(event)
    except Exception as e:  # noqa: BLE001 — без замовлення подія лишається, як була
        logger.warning('calendar event %s: order lookup failed: %s', event.pk, e)
        return {'closed': 0, 'reopened': 0}
    event.sales_order = order
    event.order_state = CalendarEvent.ORDER_OPEN if order is not None else CalendarEvent.ORDER_NONE
    CalendarEvent.objects.filter(pk=event.pk).update(sales_order=order, order_state=event.order_state)
    return apply_rule([event]) if order is not None else {'closed': 0, 'reopened': 0}


def apply_rule(events) -> dict:
    """Закрити дедлайни відправлених / скасованих замовлень і відкрити знову ті, що повернули в роботу.
    Подія, яку людина сама позначила ✓ (state open), не відкривається. → {'closed': n, 'reopened': n}"""
    CalendarEvent, CalendarSettings = _models()
    events = [e for e in events if e.sales_order_id and e.order_state in (CalendarEvent.ORDER_OPEN,
                                                                           CalendarEvent.ORDER_CLOSED)]
    out = {'closed': 0, 'reopened': 0}
    if not events:
        return out
    modes = dict(CalendarSettings.objects.filter(user_id__in={e.user_id for e in events})
                 .values_list('user_id', 'order_deadlines'))
    alive = open_ids(e.sales_order_id for e in events)
    close, reopen = [], []
    for e in events:
        if modes.get(e.user_id, CalendarSettings.ORDER_DEADLINES_OPEN) == CalendarSettings.ORDER_DEADLINES_ALL:
            continue
        if e.sales_order_id not in alive and e.order_state == CalendarEvent.ORDER_OPEN and not e.is_done:
            close.append(e.pk)
        elif e.sales_order_id in alive and e.order_state == CalendarEvent.ORDER_CLOSED:
            reopen.append(e.pk)
    if close:
        out['closed'] = CalendarEvent.objects.filter(pk__in=close).update(
            is_done=True, order_state=CalendarEvent.ORDER_CLOSED)
    if reopen:
        out['reopened'] = CalendarEvent.objects.filter(pk__in=reopen).update(
            is_done=False, order_state=CalendarEvent.ORDER_OPEN)
    return out


def order_changed(order) -> dict:
    """Замовлення збережено (статус, відправка) — його дедлайни в календарях усіх людей."""
    CalendarEvent, _ = _models()
    events = list(CalendarEvent.objects.filter(sales_order=order, event_type=CalendarEvent.TYPE_DEADLINE)
                  .exclude(order_state__in=(CalendarEvent.ORDER_UNCHECKED, CalendarEvent.ORDER_NONE)))
    return apply_rule(events)


def sweep(user=None, link_limit=SWEEP_LINK_LIMIT) -> dict:
    """Дочистити: зв'язати ще не перевірені дедлайни (до link_limit) і застосувати правило до всіх зв'язаних —
    так закриваються й ті, чиє замовлення змінили без сигналу (масове `.update()`), і старі події."""
    CalendarEvent, _ = _models()
    base = CalendarEvent.objects.filter(event_type=CalendarEvent.TYPE_DEADLINE)
    if user is not None:
        base = base.filter(user=user)
    linked, first = 0, {'closed': 0, 'reopened': 0}
    for ev in (base.filter(order_state=CalendarEvent.ORDER_UNCHECKED)
               .select_related('sales_order', 'email_message').order_by('-start_at')[:link_limit]):
        for k, v in link(ev).items():
            first[k] += v
        linked += ev.sales_order_id is not None
    events = list(base.filter(sales_order__isnull=False,
                              order_state__in=(CalendarEvent.ORDER_OPEN, CalendarEvent.ORDER_CLOSED)))
    out = apply_rule(events)
    out = {k: out[k] + first[k] for k in first}
    out['linked'] = linked
    out['left'] = base.filter(order_state=CalendarEvent.ORDER_UNCHECKED).count()
    return out


def sweep_due() -> bool:
    """Час для `sweep` з cron: раз на SWEEP_EVERY (спільний кеш для процесів web і cron)."""
    from core.shared_cache import cache
    return bool(cache.add('calendar-order-deadlines-sweep', 1, SWEEP_EVERY))


def visible(qs, user):
    """Події для показу в календарі людини: у режимі «Лише критичні» без дедлайнів замовлень, що чекають
    відправки, але строк ще далі за `order_deadline_days` днів (прийде час — з'являться самі)."""
    CalendarEvent, CalendarSettings = _models()
    cfg = CalendarSettings.for_user(user)
    if cfg.order_deadlines != CalendarSettings.ORDER_DEADLINES_CRITICAL:
        return qs
    limit = timezone.localdate() + datetime.timedelta(days=int(cfg.order_deadline_days) + 1)
    edge = timezone.make_aware(datetime.datetime.combine(limit, datetime.time.min))
    return qs.exclude(event_type=CalendarEvent.TYPE_DEADLINE, order_state=CalendarEvent.ORDER_OPEN,
                      is_done=False, start_at__gte=edge)


def hidden_count(user) -> int:
    """Скільки дедлайнів зараз сховано правилом «Лише критичні» (підказка в календарі)."""
    CalendarEvent, _ = _models()
    qs = CalendarEvent.objects.filter(user=user, event_type=CalendarEvent.TYPE_DEADLINE, is_done=False,
                                      order_state=CalendarEvent.ORDER_OPEN)
    return qs.count() - visible(qs, user).count()


def feed_q():
    """Підписка ICS: закритих дедлайнів відправлених замовлень там немає."""
    CalendarEvent, _ = _models()
    return ~Q(order_state=CalendarEvent.ORDER_CLOSED)
