from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "api"
    verbose_name = "REST API"

    def ready(self):
        from .webhooks import connect_signals
        connect_signals()
