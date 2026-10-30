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
        # 🧩 Реєстр модулів: нові застосунки реєструються після кожного migrate (core/modules.py)
        from django.db.models.signals import post_migrate
        from core.modules import register_after_migrate
        post_migrate.connect(register_after_migrate, sender=self, dispatch_uid='core_register_modules')
