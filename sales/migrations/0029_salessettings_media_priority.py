from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0028_salessettings_show_product_image_tooltip'),
    ]

    operations = [
        migrations.AddField(
            model_name='salessettings',
            name='datasheet_priority',
            field=models.CharField(
                choices=[('file', '📁 Локальний файл (пріоритет) → потім посилання'),
                         ('url', '🔗 Посилання (пріоритет) → потім локальний файл')],
                default='file',
                help_text='Якщо є і завантажений PDF, і URL — що показувати в таблиці/картках?',
                max_length=8,
                verbose_name='Пріоритет: Datasheet PDF',
            ),
        ),
        migrations.AddField(
            model_name='salessettings',
            name='image_priority',
            field=models.CharField(
                choices=[('file', '📁 Локальний файл (пріоритет) → потім посилання'),
                         ('url', '🔗 Посилання (пріоритет) → потім локальний файл')],
                default='file',
                help_text='Якщо є і завантажений файл, і URL — яке фото використовувати?',
                max_length=8,
                verbose_name='Пріоритет: Фото товару',
            ),
        ),
    ]
