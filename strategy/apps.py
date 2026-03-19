from django.apps import AppConfig


class StrategyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "strategy"
    verbose_name = "🎯 Стратегії CRM"

    def ready(self):
        import strategy.signals  # noqa: F401
