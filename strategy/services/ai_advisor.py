"""
strategy/services/ai_advisor.py

STUB — AI Advisor interface for Phase 2.
Returns fixed placeholder responses until a real AI backend is connected.
"""


class AIAdvisor:
    """
    Stub AI advisor. Phase 2 will replace methods with real LLM calls.
    Interface is fixed so callers don't need to change when AI is added.
    """

    def suggest_next_action(self, customer_strategy) -> dict:
        """Return a suggested next step for an active customer strategy."""
        return {
            "suggested_step_type": None,
            "suggested_text": "",
            "reasoning": "AI не підключений (Фаза 2)",
            "confidence": 0.0,
        }

    def analyze_response(self, step_log) -> dict:
        """Analyze a logged interaction and suggest follow-up."""
        return {
            "sentiment": "neutral",
            "suggested_follow_up": "",
            "reasoning": "AI не підключений (Фаза 2)",
            "confidence": 0.0,
        }
