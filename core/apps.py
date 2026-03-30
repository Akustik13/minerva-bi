from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'
    verbose_name = '🔐 Ядро системи'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        import core.signals  # noqa: F401
        from django.contrib.auth.signals import user_logged_in, user_logged_out
        from core.signals import _on_login, _on_logout
        user_logged_in.connect(_on_login)
        user_logged_out.connect(_on_logout)
