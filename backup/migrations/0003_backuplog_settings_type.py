from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("backup", "0002_backupplaceholder"),
    ]

    operations = [
        migrations.AlterField(
            model_name="backuplog",
            name="backup_type",
            field=models.CharField(
                choices=[
                    ("db", "База даних"),
                    ("media", "Медіа файли"),
                    ("full", "Повний"),
                    ("settings", "Налаштування"),
                ],
                max_length=10,
                verbose_name="Тип",
            ),
        ),
    ]
