"""⏰ Регулярні завдання агенту (AgentSchedule): «щодня о 8:00 перевір, що не відправлено й чому», «зранку — що
прийшло в пошту, що важливе, що на сьогодні» …

За розкладом (`run_due`, cron `agent_runs` щохвилини) — нове AgentTask з інструкцією + `NOTE` (підсумок = звіт),
модель і ліміт — з розкладу; виконує його та сама черга (`tasks.run_due`). Звіт — `agent/notify.py` (для завдань з
розкладом — завжди, повним підсумком, у вибрані канали). Раз на день: атомарний UPDATE `last_run_at` (`_claim`), не
пізніше за `CATCH_UP_HOURS` після часу (сервер лежав зранку — увечері вже не надсилає). «▶ Запустити зараз» —
`start(schedule, manual=True)`: одразу у фоні, розклад на сьогодні не змінює.
"""
import datetime
import logging

from django.utils import timezone

from agent.models import AgentSchedule, AgentTask

logger = logging.getLogger(__name__)

CATCH_UP_HOURS = 3
NOTE = ('\n\n(Регулярне завдання за розкладом «{name}». Твій підсумок — це звіт людині в Telegram і на пошту: '
        'коротко й по пунктах, найважливіше першим, що потребує її дій — окремим блоком; без вступів. Зміни '
        'пропонуй лише якщо про це просить завдання.)')

PRESETS = {
    'ship': {
        'name': '📦 Що не відправлено й чому',
        'instruction': 'Перевір замовлення, які ще не відправлені (orders_to_ship), і для кожного — чому: немає '
                       'на складі (чого й скільки бракує, чи чекаємо поставку), немає адреси чи телефону, немає '
                       'HS-коду чи ваги, чекає оплати, вже є чернетка відправлення. Окремо — прострочені й ті, у кого '
                       'дедлайн сьогодні-завтра. Наприкінці — що можна відправити вже сьогодні.',
    },
    'morning': {
        'name': '☀️ Ранковий огляд пошти й дня',
        'instruction': 'Переглянь пошту за останні 24 години (list_mail; потрібне — open_mail) і календар на '
                       'сьогодні (calendar_days). Звіт: 1) що важливе й термінове — хто, що хоче, до коли; 2) на що '
                       'чекають нашої відповіді; 3) що заплановано на сьогодні — зустрічі, дедлайни, прострочене; '
                       '4) мої листи без реакції. Розсилки й сповіщення сервісів — лише одним рядком кількістю.',
    },
}


def days_of(schedule) -> list:
    days = [d for d in (schedule.days or []) if isinstance(d, int) and 0 <= d <= 6]
    return sorted(set(days)) or list(range(7))


def days_text(schedule) -> str:
    days = days_of(schedule)
    if len(days) == 7:
        return 'щодня'
    if days == [0, 1, 2, 3, 4]:
        return 'у будні'
    names = dict(AgentSchedule.WEEKDAYS)
    return ', '.join(names[d] for d in days)


def _at(day, time):
    return timezone.make_aware(datetime.datetime.combine(day, time))


def due(schedule, now=None) -> bool:
    now = timezone.localtime(now or timezone.now())
    if not schedule.enabled or now.weekday() not in days_of(schedule):
        return False
    start = _at(now.date(), schedule.time)
    if now < start or now - start > datetime.timedelta(hours=CATCH_UP_HOURS):
        return False
    return not (schedule.last_run_at and timezone.localtime(schedule.last_run_at).date() >= now.date())


def next_run(schedule, now=None):
    """Коли наступний запуск (aware datetime) або None — вимкнено."""
    if not schedule.enabled:
        return None
    now = timezone.localtime(now or timezone.now())
    days = days_of(schedule)
    for shift in range(8):
        day = now.date() + datetime.timedelta(days=shift)
        if day.weekday() not in days:
            continue
        at = _at(day, schedule.time)
        if shift == 0 and (at < now - datetime.timedelta(hours=CATCH_UP_HOURS) or (
                schedule.last_run_at and timezone.localtime(schedule.last_run_at).date() >= day)):
            continue
        return max(at, now) if shift == 0 else at
    return None


def _claim(schedule, now) -> bool:
    """Раз на день: лише один запуск (два процеси cron одночасно — другий нічого не робить)."""
    day_start = _at(timezone.localtime(now).date(), datetime.time.min)
    from django.db.models import Q
    return bool(AgentSchedule.objects.filter(Q(last_run_at__isnull=True) | Q(last_run_at__lt=day_start),
                                             pk=schedule.pk).update(last_run_at=now))


def allowed(schedule) -> str:
    """Чому розклад не може запускатися ('' — може)."""
    owner = schedule.owner
    if not owner.is_active:
        return 'власник неактивний'
    if not owner.has_perm('agent.add_agenttask'):
        return 'у власника немає права давати завдання агенту'
    return ''


def make_task(schedule, *, quiet=False) -> AgentTask:
    today = timezone.localdate()
    return AgentTask.objects.create(
        owner=schedule.owner, schedule=schedule, quiet=quiet,
        title=(f'🧪 {schedule.name} · пробний · {today:%d.%m.%Y}' if quiet
               else f'{schedule.name} · {today:%d.%m.%Y}')[:200],
        instruction=schedule.instruction.strip() + NOTE.format(name=schedule.name),
        model=schedule.model, budget_usd=schedule.budget_usd)


def start(schedule, *, manual=False, at=None, quiet=False):
    """manual — «▶ Запустити зараз» (у фоні, розклад не змінює); quiet — «🧪 Спробувати без звіту» (те саме, звіт
    нікуди не йде); інакше — запуск cron (черга завдань), at — «зараз» для запису запуску. → AgentTask або None
    (не можна / уже запущено сьогодні)."""
    why = allowed(schedule)
    if why:
        logger.info('agent schedule %s skipped: %s', schedule.pk, why)
        return None
    manual = manual or quiet
    if not manual and not _claim(schedule, at or timezone.now()):
        return None
    task = make_task(schedule, quiet=quiet)
    if manual:
        from agent import tasks
        tasks.start(task)
    return task


def preview(schedule) -> dict:
    """🧪 Як виконуватиметься: коли, що агент бачитиме (✓ / ✗ з причиною), куди піде звіт. Лише читання бази."""
    from agent import inbox_tools, purchases, rag, shipments
    from agent.notify import _email_to
    owner = schedule.owner
    at = next_run(schedule)
    when = [f'{days_text(schedule)} о {schedule.time:%H:%M}',
            'наступний запуск: ' + (timezone.localtime(at).strftime('%d.%m.%Y %H:%M') if at else 'вимкнено')]
    see = [(True, '📋 Склад, товари, даташити, ціни, магазини')]
    see.append((shipments.can_read(owner), '📦 Замовлення й відправлення — що не відправлено, склад позицій, адреса'
                if shipments.can_read(owner) else '📦 Замовлення — немає права бачити замовлення (Продажі)'))
    see.append((purchases.can_read(owner), '🛒 Закупівлі й вхідні посилки' if purchases.can_read(owner)
                else '🛒 Закупівлі — немає права бачити закупівлі'))
    if inbox_tools.can_read(owner):
        from email_assistant.models import EmailMessage
        day = timezone.now() - datetime.timedelta(hours=24)
        mails = EmailMessage.objects.filter(account__user=owner, folder=EmailMessage.FOLDER_INBOX, is_deleted=False,
                                            is_spam=False, sent_at__gte=day)
        see.append((True, f'📬 Ваша пошта — за останню добу {mails.count()} листів, непрочитаних '
                          f'{mails.filter(is_read=False).count()}'))
        from calendar_app.models import CalendarEvent
        start = _at(timezone.localdate(), datetime.time.min)
        events = CalendarEvent.objects.filter(user=owner, start_at__gte=start,
                                              start_at__lt=start + datetime.timedelta(days=1)).count()
        see.append((True, f'📅 Ваш календар — сьогодні подій: {events}'))
    else:
        see.append((False, '📬 Пошта й 📅 календар — у вас немає скриньки в Minerva'))
    if rag.tools_for(owner):
        see.append((True, '🔎 Пошук у RAG (даташити, історія листування)'))
    report = []
    if schedule.notify_telegram:
        tg = getattr(getattr(owner, 'profile', None), 'telegram_id', None)
        try:
            from config.models import NotificationSettings
            bot = bool(NotificationSettings.get().telegram_bot_token)
        except Exception:  # noqa: BLE001
            bot = False
        if not tg:
            report.append((False, '💬 Telegram — у «Мої налаштування» не вказано Telegram ID'))
        elif not bot:
            report.append((False, '💬 Telegram — бот не налаштований («Налаштування → Сповіщення»)'))
        else:
            report.append((True, f'💬 Telegram — на ваш ID {tg}'))
    if schedule.notify_email:
        to = _email_to(owner)
        report.append((bool(to), f'✉️ Пошта — на {to}' if to else '✉️ Пошта — немає вашої адреси'))
    if not report:
        report.append((False, 'Звіт нікуди не надсилається — увімкніть Telegram чи пошту'))
    model = dict(AgentSchedule._meta.get_field('model').choices).get(schedule.model, schedule.model)
    return {'when': when, 'see': see, 'report': report, 'blocked': allowed(schedule),
            'cost': f'{model}, до {schedule.budget_usd} $ за запуск'}


def run_due(now=None) -> int:
    """Прохід cron: розклади, час яких настав, → завдання в черзі. → скільки поставлено."""
    n = 0
    for schedule in AgentSchedule.objects.filter(enabled=True).select_related('owner'):
        if due(schedule, now) and start(schedule, at=now):
            n += 1
    return n
