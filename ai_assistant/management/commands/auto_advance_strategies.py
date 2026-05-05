"""
ai_assistant/management/commands/auto_advance_strategies.py

Рівень 3 — Авто-просування кроків стратегії на основі аналізу листування.
Запускається з cron_runner.sh кожні REMINDER_INTERVAL секунд.
"""
import requests
from django.core.management.base import BaseCommand
from django.utils import timezone

from strategy.models import CustomerStrategy, AISettings
from strategy.services.engine import advance_step
from core.models import UserProfile


class Command(BaseCommand):
    help = 'Авто-просування кроків стратегії на основі листування клієнта'

    def handle(self, *args, **options):
        now = timezone.now()
        advanced = 0
        skipped = 0

        strategies = (
            CustomerStrategy.objects
            .filter(status='active', current_step__outcome='pending')
            .select_related('current_step__template_step', 'customer')
        )

        for cs in strategies:
            step = cs.current_step
            if step is None:
                continue
            result = self._try_advance(cs, step, now)
            if result:
                advanced += 1
            else:
                skipped += 1

        self.stdout.write(
            f'auto_advance: {advanced} просунуто, {skipped} пропущено'
        )

    # ──────────────────────────────────────────────────────────────
    def _try_advance(self, cs, step, now):
        """
        Перевіряє умови і просуває крок якщо можливо.
        Повертає True якщо крок просунуто.
        """
        step_type = step.step_type

        # pause — автоматично якщо час настав
        if step_type == 'pause':
            if step.scheduled_at and step.scheduled_at <= now:
                new_step = advance_step(step, 'done_pos', 'Авто-пауза завершена', user=None)
                self._notify(step, 'done_pos', new_step)
                return True
            return False

        # email — шукаємо відповідь клієнта після початку кроку
        if step_type == 'email':
            email_in = self._find_reply(cs.customer, step)
            if email_in:
                outcome = self._ai_classify(cs.customer, step, email_in)
                new_step = advance_step(step, outcome, f'AI класифікація: {outcome}', user=None)
                self._notify(step, outcome, new_step)
                return True
            # Таймаут — 7 днів без відповіді
            if step.scheduled_at and (step.scheduled_at + timezone.timedelta(days=7)) <= now:
                new_step = advance_step(step, 'no_response', 'Таймаут 7 днів', user=None)
                self._notify(step, 'no_response', new_step)
                return True
            return False

        # decision — шукаємо лист-відповідь для вибору гілки
        if step_type == 'decision':
            email_in = self._find_reply(cs.customer, step)
            if email_in:
                outcome = self._ai_classify(cs.customer, step, email_in)
                new_step = advance_step(step, outcome, f'AI рішення: {outcome}', user=None)
                self._notify(step, outcome, new_step)
                return True
            return False

        # call та інші — пропустити (потребує ручного виконання)
        return False

    def _find_reply(self, customer, step):
        """Шукає вхідний email клієнта після початку кроку."""
        try:
            from crm.models import CustomerTimeline
            since = step.scheduled_at or (timezone.now() - timezone.timedelta(days=30))
            email = (
                CustomerTimeline.objects
                .filter(
                    customer=customer,
                    event_type='email_in',
                    created_at__gt=since,
                )
                .order_by('created_at')
                .first()
            )
            return email
        except Exception:
            return None

    def _ai_classify(self, customer, step, email_event):
        """Класифікує відповідь клієнта через AI."""
        try:
            email_body = email_event.body or email_event.title or ''
            prompt = (
                f"Клієнт '{customer.name}' відповів на лист.\n"
                f"Крок стратегії: '{step.title}'\n"
                f"Текст відповіді: {email_body[:500]}\n\n"
                "Визнач результат ОДНИМ СЛОВОМ без пояснень:\n"
                "done_pos — позитивна відповідь, зацікавленість\n"
                "done_neg — відмова, негатив\n"
                "no_response — нейтрально або незрозуміло"
            )
            from ai_assistant.service import chat, reset_conversation
            reset_conversation(None, 'strategy_auto')
            raw = chat(prompt, profile=None, channel='strategy_auto')
            outcome = raw.strip().split()[0].lower()
            if outcome not in ('done_pos', 'done_neg', 'no_response'):
                outcome = 'no_response'
            return outcome
        except Exception:
            return 'no_response'

    def _notify(self, step, outcome, new_step):
        """Відправляє Telegram-сповіщення про просування кроку."""
        try:
            token = AISettings.get().telegram_bot_token
            if not token:
                return
            text = (
                f"🔄 <b>Стратегія #{step.strategy.pk}</b>\n"
                f"Клієнт: {step.strategy.customer.name}\n"
                f"Крок: {step.title} → {outcome}\n"
                f"Наступний: {new_step.title if new_step else '✅ стратегія завершена'}"
            )
            for p in UserProfile.objects.filter(telegram_id__isnull=False).exclude(telegram_id=0):
                try:
                    requests.post(
                        f'https://api.telegram.org/bot{token}/sendMessage',
                        json={'chat_id': p.telegram_id, 'text': text, 'parse_mode': 'HTML'},
                        timeout=5,
                    )
                except Exception:
                    pass
        except Exception:
            pass
