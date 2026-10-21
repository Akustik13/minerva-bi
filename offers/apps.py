from django.apps import AppConfig


class OffersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "offers"
    verbose_name = "📝 Angebote"

    def ready(self):
        from . import signals  # noqa: F401  — запити пропозицій у новій пошті
