from django.apps import AppConfig


class MeetingsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "meetings"
    verbose_name = "🎙 Наради"

    def ready(self):
        from . import signals  # noqa: F401 — post_delete наради → документи бази знань і tombstone
