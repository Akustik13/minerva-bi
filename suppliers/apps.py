from django.apps import AppConfig


class SuppliersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'suppliers'
    verbose_name = '🏭 Постачальники'

    def ready(self):
        from . import signals  # noqa: F401 — відповіді на запити (RFQ) у пошті
