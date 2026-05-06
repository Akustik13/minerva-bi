"""
ai_assistant/management/commands/morning_briefing.py
Надіслати ранковий брифінг кожному юзеру у Telegram.
"""
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger('ai_assistant')


class Command(BaseCommand):
    help = 'Надіслати ранковий брифінг кожному юзеру в Telegram'

    def handle(self, *args, **options):
        from core.models import UserProfile
        from config.models import BriefingSettings

        bs = BriefingSettings.get()
        if not bs.enabled:
            self.stdout.write('Брифінг вимкнено в налаштуваннях.')
            return

        profiles = (UserProfile.objects
                    .filter(telegram_id__isnull=False)
                    .exclude(telegram_id=0)
                    .select_related('user'))

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
                new_emails = CustomerTimeline.objects.filter(
                    event_type='email_in',
                    created_at__date=today,
                ).count()
                if new_emails:
                    data_lines.append(f"- Нових листів від клієнтів сьогодні: {new_emails}")
            except Exception:
                pass

        data_section = '\n'.join(data_lines) if data_lines else '— немає даних —'

        prompt = (
            f"Зроби короткий ранковий брифінг для "
            f"{profile.user.get_full_name() or profile.user.username}.\n\n"
            f"Дані системи:\n{data_section}"
            f"{reminder_text}\n\n"
            "Напиши брифінг для Telegram:\n"
            "- Максимум 5-7 рядків\n"
            "- Ключові цифри дня\n"
            "- 1-2 конкретні дії які треба зробити сьогодні\n"
            "- Легкий тон, але по ділу\n"
            "- Без зайвих слів"
        )

        if bs.custom_instructions:
            prompt += f"\n\nДодаткові інструкції: {bs.custom_instructions}"

        return chat(prompt, profile=profile, channel='system_briefing')

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
