from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0037_addressbook_owner'),
    ]

    operations = [
        migrations.AddField(
            model_name='shippingsettings',
            name='prefer_tracking_number',
            field=models.BooleanField(
                default=True,
                help_text='Якщо є трекінг-номер — спочатку запитати пряму API перевізника (DHL Tracking Unified, UPS), лише потім Jumingo. Допомагає коли Jumingo показує «Етикетка готова», а посилка вже в дорозі.',
                verbose_name='Пріоритет трекінг-номера',
            ),
        ),
        migrations.AddField(
            model_name='shippingsettings',
            name='status_upgrade_only',
            field=models.BooleanField(
                default=True,
                help_text='Не знижувати статус при синхронізації (наприклад «В дорозі» → «Етикетка готова» буде ігноровано). Рекомендовано: увімкнено.',
                verbose_name='Тільки підвищувати статус',
            ),
        ),
    ]
