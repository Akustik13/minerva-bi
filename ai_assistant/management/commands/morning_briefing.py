"""
ai_assistant/management/commands/morning_briefing.py
Надіслати ранковий брифінг кожному юзеру у Telegram.

    python manage.py morning_briefing            # зараз (кнопка «📤 Тестова відправка»)
    python manage.py morning_briefing --if-due   # з cron кожну хвилину: раз на день у «Час відправки»
                                                 # з «Налаштувань брифінгу» (env BRIEFING_HOUR — лише запасний)
"""
import datetime
import logging
import os

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger('ai_assistant')

# Сервер не працював у «Час відправки» — брифінг ще піде протягом стількох годин; пізніше — уже завтра
CATCH_UP_HOURS = 3


def send_time_of(bs) -> datetime.time:
    """«Час відправки» з BriefingSettings; якщо його немає — година з env BRIEFING_HOUR (за замовчуванням 08)."""
    t = getattr(bs, 'send_time', None)
    if isinstance(t, str):   # щойно створений рядок налаштувань: default "08:00" ще не перетворено
        try:
            t = datetime.time.fromisoformat(t)
        except ValueError:
            t = None
    if t:
        return t
    try:
        hour = int(os.environ.get('BRIEFING_HOUR', '8'))
    except (TypeError, ValueError):
        hour = 8
    return datetime.time(min(max(hour, 0), 23), 0)


def briefing_due(bs, now=None):
    """(чи час надсилати, причина) — за часом Берліна (TIME_ZONE): після «Часу відправки», лише раз на день."""
    if not bs.enabled:
        return False, 'Брифінг вимкнено в налаштуваннях.'
    local = timezone.localtime(now or timezone.now())
    t = send_time_of(bs)
    start = local.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if local < start:
        return False, f'Ще не час (налаштовано {t:%H:%M}).'
    if bs.last_sent_at and timezone.localtime(bs.last_sent_at).date() >= local.date():
        return False, 'Сьогодні вже надіслано.'
    if local >= start + datetime.timedelta(hours=CATCH_UP_HOURS):
        return False, f'Пропущено: сервер не працював о {t:%H:%M} — наступний брифінг завтра.'
    return True, ''


def claim_today(now=None) -> bool:
    """Атомарно позначити «сьогодні надіслано» ДО відправки: другий запуск (паралельний чи повторний) — ні."""
    from config.models import BriefingSettings
    now = now or timezone.now()
    day_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return BriefingSettings.objects.filter(pk=1).filter(
        Q(last_sent_at__isnull=True) | Q(last_sent_at__lt=day_start)).update(last_sent_at=now) == 1


class Command(BaseCommand):
    help = 'Надіслати ранковий брифінг кожному юзеру в Telegram'

    def add_arguments(self, parser):
        parser.add_argument(
            '--if-due', action='store_true',
            help='Лише в «Час відправки» з налаштувань брифінгу і не частіше ніж раз на день (для cron)')

    def handle(self, *args, **options):
        from core.models import UserProfile
        from config.models import BriefingSettings

        bs = BriefingSettings.get()
        if options.get('if_due'):
            ok, reason = briefing_due(bs)
            if not ok:
                if options.get('verbosity', 1) > 1:
                    self.stdout.write(reason)
                return
        elif not bs.enabled:
            self.stdout.write('Брифінг вимкнено в налаштуваннях.')
            return

        profiles = (UserProfile.objects
                    .filter(telegram_id__isnull=False)
                    .exclude(telegram_id=0)
                    .select_related('user'))

        if options.get('if_due'):
            if not profiles.exists():
                return   # за розкладом без отримувачів AI не викликається
            if not claim_today():
                return   # інший запуск уже надсилає сьогоднішній брифінг

        if not profiles.exists():
            self.stdout.write('Немає юзерів з Telegram ID — виводимо брифінг в stdout')
            fallback = UserProfile.objects.select_related('user').first()
            if fallback:
                text = self._generate_briefing(fallback, bs)
                self.stdout.write('\n=== BRIEFING (stdout) ===\n' + (text or '(пусто)'))
            return

        sent = errors = 0
        for profile in profiles:
            try:
                briefing = self._generate_briefing(profile, bs)
                if briefing:
                    self._send_telegram(profile.telegram_id, briefing)
                    sent += 1
                    self.stdout.write(f'✅ {profile.user.username}')
            except Exception as e:
                errors += 1
                logger.error(f'Briefing error for {profile.user.username}: {e}')
                self.stdout.write(f'❌ {profile.user.username}: {e}')

        self.stdout.write(f'\nРезультат: {sent} надіслано, {errors} помилок')

    def _generate_briefing(self, profile, bs=None) -> str:
        from ai_assistant.tools import execute_tool
        from ai_assistant.service import chat
        from crm.models import CustomerTimeline
        from config.models import BriefingSettings

        if bs is None:
            bs = BriefingSettings.get()

        overview  = execute_tool('get_system_overview',    {}, profile=profile)
        financial = execute_tool('get_financial_overview', {}, profile=profile)

        today = timezone.now().date()
        reminders = (CustomerTimeline.objects
                     .filter(remind_at__date=today, remind_sent=False)
                     .select_related('customer')
                     .values('customer__name', 'title')[:5])

        reminder_text = ''
        if reminders and bs.include_reminders:
            items = [f"• {r['customer__name']}: {r['title']}" for r in reminders]
            reminder_text = '\n\nНАГАДУВАННЯ НА СЬОГОДНІ:\n' + '\n'.join(items)

        rev_month    = overview.get('revenue_this_month', 0)   if isinstance(overview, dict)  else 0
        orders_month = overview.get('orders_this_month', 0)    if isinstance(overview, dict)  else 0
        overdue_count = financial.get('overdue_count', 0)      if isinstance(financial, dict) else 0

        # Build data section based on BriefingSettings
        data_lines = []
        if bs.include_orders:
            data_lines.append(f"- Замовлень цього місяця: {orders_month}")
        if bs.include_revenue:
            data_lines.append(f"- Виручка: €{float(rev_month):.0f}")
        if bs.include_overdue:
            data_lines.append(f"- Прострочених дедлайнів: {overdue_count}")

        if bs.include_stock_alerts:
            try:
                from dashboard.notifications import _get_critical_stock
                critical = _get_critical_stock()
                if critical:
                    data_lines.append(f"- Критичний залишок: {len(critical)} товарів")
            except Exception:
                pass

        if bs.include_new_emails:
            try:
                from crm.models import CustomerTimeline
                incoming = (CustomerTimeline.objects
                            .filter(event_type='email_in', created_at__date=today)
                            .select_related('customer')
                            .order_by('-created_at')[:10])
                if incoming:
                    senders = []
                    seen = set()
                    for ev in incoming:
                        label = (ev.customer.company or ev.customer.name
                                 if ev.customer else ev.title[:40])
                        if label not in seen:
                            seen.add(label)
                            senders.append(label)
                        if len(senders) >= 3:
                            break
                    extra = len(incoming) - len(senders)
                    sender_str = ', '.join(senders)
                    if extra > 0:
                        sender_str += f' та ще {extra}'
                    data_lines.append(
                        f"- Нових листів від клієнтів: {len(incoming)} ({sender_str})"
                    )
            except Exception:
                pass

        data_section = '\n'.join(data_lines) if data_lines else '— немає даних —'

        projects = self._projects_text(profile.user) if bs.include_projects else ''
        projects_prompt = (f"\n\nПРОЄКТИ, ЩО ПОТРЕБУЮТЬ УВАГИ (відповідальний — цей користувач):\n{projects}"
                           if projects else '')

        prompt = (
            f"Зроби короткий ранковий брифінг для "
            f"{profile.user.get_full_name() or profile.user.username}.\n\n"
            f"Дані системи:\n{data_section}"
            f"{reminder_text}{projects_prompt}\n\n"
            "Напиши брифінг для Telegram:\n"
            "- Максимум 5-7 рядків\n"
            "- Ключові цифри дня\n"
            "- 1-2 конкретні дії які треба зробити сьогодні"
            + (" (насамперед — найтерміновіше з проєктів)" if projects else "") + "\n"
            "- Легкий тон, але по ділу\n"
            "- Без зайвих слів"
        )

        if bs.custom_instructions:
            prompt += f"\n\nДодаткові інструкції: {bs.custom_instructions}"

        text = chat(prompt, profile=profile, channel='system_briefing')
        if projects:   # повний список — під текстом AI, щоб нічого не загубилось
            from html import escape
            text = (text or '').rstrip() + '\n\n📁 <b>Потребує уваги</b>\n' + escape(projects)
        return text

    def _projects_text(self, user) -> str:
        try:
            from projects.overview import digest_text
            return digest_text(user)
        except Exception as e:  # noqa: BLE001 — брифінг не має ламатися через проєкти
            logger.warning('briefing projects for %s: %s', user, e)
            return ''

    def _send_telegram(self, chat_id: int, text: str):
        import requests
        from strategy.models import AISettings

        token = AISettings.get().telegram_bot_token
        if not token:
            raise ValueError('Telegram bot token не налаштований')

        r = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={
                'chat_id':    chat_id,
                'text':       text,
                'parse_mode': 'HTML',
            },
            timeout=10,
        )
        if not r.ok:
            raise ValueError(f'Telegram API error: {r.text[:200]}')
