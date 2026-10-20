from django.apps import AppConfig


class ProjectsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "projects"
    verbose_name = "📁 Проєкти"

    def ready(self):
        import projects.signals  # noqa: F401
