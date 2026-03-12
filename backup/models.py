from django.db import models


class BackupPlaceholder(models.Model):
    """Placeholder — таблиця в БД не створюється (sidebar item)."""
    class Meta:
        managed = False
        verbose_name = "Резервне копіювання"
        verbose_name_plural = "💾 Резервне копіювання"


class BackupSettings(models.Model):
    backup_dir = models.CharField(
        "Шлях для бекапів", max_length=500, default="",
        help_text="Порожнє → {BASE_DIR}/backups/"
    )
    include_media = models.BooleanField("Включати медіа файли у повний бекап", default=True)
    auto_enabled = models.BooleanField("Автобекап увімкнено", default=False)
    schedule = models.CharField(
        "Розклад", max_length=20,
        choices=[("daily", "Щодня"), ("weekly", "Щотижня")],
        default="daily",
    )
    retention = models.PositiveIntegerField(
        "Зберігати останніх N бекапів", default=10
    )

    class Meta:
        verbose_name = "Налаштування бекапу"
        verbose_name_plural = "Налаштування бекапу"

    @classmethod
    def get_settings(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class BackupLog(models.Model):
    TYPE_DB = "db"
    TYPE_MEDIA = "media"
    TYPE_FULL = "full"
    TYPE_SETTINGS = "settings"
    TYPE_CHOICES = [
        (TYPE_DB, "База даних"),
        (TYPE_MEDIA, "Медіа файли"),
        (TYPE_FULL, "Повний"),
        (TYPE_SETTINGS, "Налаштування"),
    ]

    STATUS_OK = "ok"
    STATUS_ERROR = "error"
    STATUS_RUNNING = "running"
    STATUS_CHOICES = [
        (STATUS_OK, "Успішно"),
        (STATUS_ERROR, "Помилка"),
        (STATUS_RUNNING, "Виконується"),
    ]

    created_at = models.DateTimeField(auto_now_add=True)
    backup_type = models.CharField("Тип", max_length=10, choices=TYPE_CHOICES)
    status = models.CharField(
        "Статус", max_length=10, choices=STATUS_CHOICES, default=STATUS_RUNNING
    )
    file_path = models.CharField("Файл", max_length=1000, blank=True)
    file_size = models.BigIntegerField("Розмір (байт)", default=0)
    duration = models.FloatField("Тривалість (с)", default=0)
    error_msg = models.TextField("Помилка", blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Лог бекапу"
        verbose_name_plural = "Логи бекапів"
