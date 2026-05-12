from django.apps import AppConfig


class EmailAssistantConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'email_assistant'
    verbose_name = '📧 Email Асистент'

    def ready(self):
        import email_assistant.signals  # noqa: F401
