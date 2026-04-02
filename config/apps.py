from django.apps import AppConfig


class ConfigApp(AppConfig):
    name = "config"
    verbose_name = "Конфігурація"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from django.db.models.signals import post_save
        from django.dispatch import receiver

        @receiver(post_save, sender='config.SystemSettings')
        def sync_site_domain(sender, instance, **kwargs):
            """При збереженні SystemSettings — оновити django.contrib.sites."""
            try:
                from django.contrib.sites.models import Site
                site = Site.objects.get_current()
                if site.domain != instance.site_domain or site.name != instance.company_name:
                    site.domain = instance.site_domain
                    site.name   = instance.company_name or 'Minerva BI'
                    site.save()
            except Exception:
                pass  # БД ще не готова при першому запуску
