from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="BackupSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("backup_dir", models.CharField(default="", help_text="Порожнє → {BASE_DIR}/backups/", max_length=500, verbose_name="Шлях для бекапів")),
                ("include_media", models.BooleanField(default=True, verbose_name="Включати медіа файли у повний бекап")),
                ("auto_enabled", models.BooleanField(default=False, verbose_name="Автобекап увімкнено")),
                ("schedule", models.CharField(choices=[("daily", "Щодня"), ("weekly", "Щотижня")], default="daily", max_length=20, verbose_name="Розклад")),
                ("retention", models.PositiveIntegerField(default=10, verbose_name="Зберігати останніх N бекапів")),
            ],
            options={
                "verbose_name": "Налаштування бекапу",
                "verbose_name_plural": "Налаштування бекапу",
            },
        ),
        migrations.CreateModel(
            name="BackupLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("backup_type", models.CharField(choices=[("db", "База даних"), ("media", "Медіа файли"), ("full", "Повний")], max_length=10, verbose_name="Тип")),
                ("status", models.CharField(choices=[("ok", "Успішно"), ("error", "Помилка"), ("running", "Виконується")], default="running", max_length=10, verbose_name="Статус")),
                ("file_path", models.CharField(blank=True, max_length=1000, verbose_name="Файл")),
                ("file_size", models.BigIntegerField(default=0, verbose_name="Розмір (байт)")),
                ("duration", models.FloatField(default=0, verbose_name="Тривалість (с)")),
                ("error_msg", models.TextField(blank=True, verbose_name="Помилка")),
            ],
            options={
                "verbose_name": "Лог бекапу",
                "verbose_name_plural": "Логи бекапів",
                "ordering": ["-created_at"],
            },
        ),
    ]
