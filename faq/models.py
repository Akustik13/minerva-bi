from django.db import models


class FaqPlaceholder(models.Model):
    """Placeholder — таблиця в БД не створюється."""
    class Meta:
        managed = False
        verbose_name = "FAQ та підтримка"
        verbose_name_plural = "❓ FAQ та підтримка"
