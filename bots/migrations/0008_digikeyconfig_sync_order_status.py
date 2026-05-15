from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0007_digikeyconfig_public_base_url'),
    ]

    operations = [
        migrations.AddField(
            model_name='digikeyconfig',
            name='sync_order_status',
            field=models.BooleanField(
                default=True,
                help_text=(
                    'Якщо увімкнено — статус замовлення оновлюється зі статусу DigiKey '
                    '(тільки якщо новий статус вищий за поточний). '
                    'Вимкніть якщо статус керується трекінгом перевізника (UPS/DHL) '
                    'або виставляється вручну.'
                ),
                verbose_name='Оновлювати статус замовлення при синхронізації',
            ),
        ),
    ]
