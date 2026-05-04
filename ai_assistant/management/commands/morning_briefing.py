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

        profiles = (UserProfile.objects
                    .filter(telegram_id__isnull=False)
                    .exclude(telegram_id=0)
                    .select_related('user'))

        if not profiles.exists():
            self.stdout.write('Немає юзерів з Telegram ID — виводимо брифінг в stdout')
            # Сформувати брифінг для першого суперадміна (для тесту)
            fallback = UserProfile.objects.select_related('user').first()
            if fallback:
                text = self._generate_briefing(fallback)
                self.stdout.write('\n=== BRIEFING (stdout) ===\n' + (text or '(пусто)'))
            return

        sent = errors = 0
        for profile in profiles:
            try:
                briefing = self._generate_briefing(profile)
                if briefing:
                    self._send_telegram(profile.telegram_id, briefing)
                    sent += 1
                    self.stdout.write(f'✅ {profile.user.username}')
            except Exception as e:
                errors += 1
                logger.error(f'Briefing error for {profile.user.username}: {e}')
                self.stdout.write(f'❌ {profile.user.username}: {e}')

        self.stdout.write(f'\nРезультат: {sent} надіслано, {errors} помилок')

    def _generate_briefing(self, profile) -> str:
        from ai_assistant.tools import execute_tool
        from ai_assistant.service import chat
        from crm.models import CustomerTimeline

        overview  = execute_tool('get_system_overview',   {}, profile=profile)
        financial = execute_tool('get_financial_overview', {}, profile=profile)
        orders    = execute_tool('get_recent_orders',
                                 {'limit': 5, 'status': 'new'}, profile=profile)

        today = timezone.now().date()
        reminders = (CustomerTimeline.objects
                     .filter(remind_at__date=today, remind_sent=False)
                     .select_related('customer')
                     .values('customer__name', 'title')[:5])

        reminder_text = ''
        if reminders:
            items = [f"• {r['customer__name']}: {r['title']}" for r in reminders]
            reminder_text = '\n\nНАГАДУВАННЯ НА СЬОГОДНІ:\n' + '\n'.join(items)

        new_orders_count = len(orders.get('orders', [])) if isinstance(orders, dict) else 0
        overdue_count    = financial.get('overdue_count', 0) if isinstance(financial, dict) else 0
        rev_month        = overview.get('revenue_this_month', 0) if isinstance(overview, dict) else 0
        orders_month     = overview.get('orders_this_month', 0) if isinstance(overview, dict) else 0

        prompt = (
            f"Зроби короткий ранковий брифінг для "
            f"{profile.user.get_full_name() or profile.user.username}.\n\n"
            f"Дані системи:\n"
            f"- Замовлень цього місяця: {orders_month}\n"
            f"- Виручка: €{float(rev_month):.0f}\n"
            f"- Нових замовлень зараз: {new_orders_count}\n"
            f"- Прострочених: {overdue_count}"
            f"{reminder_text}\n\n"
            "Напиши брифінг для Telegram:\n"
            "- Максимум 5-7 рядків\n"
            "- Ключові цифри дня\n"
            "- 1-2 конкретні дії які треба зробити сьогодні\n"
            "- Легкий тон, але по ділу\n"
            "- Без зайвих слів"
        )

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
