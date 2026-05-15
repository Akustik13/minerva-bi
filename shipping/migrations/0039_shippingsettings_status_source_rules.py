from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0038_shippingsettings_tracking_priority'),
    ]

    operations = [
        migrations.AddField(
            model_name='shippingsettings',
            name='trust_cached_tracking',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Якщо API перевізника недоступний — використати кешований статус '
                    'з попереднього запиту. '
                    'ВИМКНЕНО за замовчуванням: старий кеш «Доставлено» може хибно '
                    'закрити замовлення яке ще в дорозі.'
                ),
                verbose_name='Довіряти кешу при збої API',
            ),
        ),
        migrations.AddField(
            model_name='shippingsettings',
            name='order_status_lock_delivered',
            field=models.BooleanField(
                default=True,
                help_text=(
                    'Не дозволяти автоматичному трекінгу знижувати статус замовлення '
                    'з «Доставлено» назад до «Відправлено». '
                    'Рекомендовано: увімкнено.'
                ),
                verbose_name='Блокувати статус після «Доставлено»',
            ),
        ),
    ]
