from django.apps import AppConfig


class RagAssistantConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "rag_assistant"
    verbose_name = "📚 Помічник по продукції (RAG)"

    def ready(self):
        import rag_assistant.signals  # noqa: F401  (журнал бази знань)
