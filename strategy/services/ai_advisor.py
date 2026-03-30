"""
strategy/services/ai_advisor.py

AI Advisor — calls Claude API when a UserProfile with ai_enabled=True is configured.
Falls back to placeholder responses when AI is not configured.
"""


class AIAdvisor:
    """
    AI advisor backed by Claude API (when configured via UserProfile).
    Interface is stable — callers don't change when AI backend is swapped.
    """

    def suggest_next_action(self, customer_strategy, user=None) -> dict:
        """Return a suggested next step for an active customer strategy."""
        if user:
            result = self._call(
                user=user,
                prompt=self._build_strategy_prompt(customer_strategy),
                customer=customer_strategy.customer,
                strategy=customer_strategy,
            )
            if result:
                return result

        return {
            "suggested_step_type": None,
            "suggested_text": "",
            "reasoning": "AI не підключений — налаштуйте UserProfile",
            "confidence": 0.0,
        }

    def analyze_response(self, step_log, user=None) -> dict:
        """Analyze a logged interaction and suggest follow-up."""
        if user:
            result = self._call(
                user=user,
                prompt=self._build_log_prompt(step_log),
                customer=step_log.step.strategy.customer if step_log.step else None,
            )
            if result:
                return result

        return {
            "sentiment": "neutral",
            "suggested_follow_up": "",
            "reasoning": "AI не підключений — налаштуйте UserProfile",
            "confidence": 0.0,
        }

    def _call(self, user, prompt: str, customer=None, strategy=None) -> dict | None:
        """
        Call Claude API using user's AI settings from UserProfile.
        Returns parsed dict or None if AI is not configured / call fails.
        """
        try:
            profile = user.profile
        except Exception:
            return None

        if not profile.ai_enabled:
            return None

        try:
            import anthropic
            from core.utils import build_ai_system_prompt

            client = anthropic.Anthropic()
            system = build_ai_system_prompt(profile, customer=customer, strategy=strategy)

            message = client.messages.create(
                model=profile.ai_model or 'claude-sonnet-4-6',
                max_tokens=512,
                temperature=profile.ai_temperature,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            text = message.content[0].text if message.content else ""
            return {
                "suggested_text": text,
                "reasoning": f"AI ({profile.ai_model})",
                "confidence": 0.8,
            }
        except ImportError:
            return None
        except Exception:
            return None

    @staticmethod
    def _build_strategy_prompt(strategy) -> str:
        customer = strategy.customer
        step = strategy.current_step
        return (
            f"Клієнт: {customer.company or customer.name}\n"
            f"Стратегія: {strategy.name or strategy.template}\n"
            f"Поточний крок: {step.title if step else 'немає'}\n\n"
            "Яку наступну дію рекомендуєш для цього клієнта? "
            "Відповідь до 3 речень."
        )

    @staticmethod
    def _build_log_prompt(step_log) -> str:
        return (
            f"Крок: {step_log.step.title if step_log.step else '?'}\n"
            f"Результат: {step_log.outcome}\n"
            f"Нотатки: {step_log.notes or 'немає'}\n\n"
            "Проаналізуй результат та запропонуй подальші дії. "
            "Відповідь до 3 речень."
        )
