from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0018_price_delta_and_log'),
    ]

    operations = [
        migrations.AddField(
            model_name='digikeyconfig',
            name='pull_enabled',
            field=models.BooleanField(
                default=False,
                verbose_name='Авто-стягування лістингів',
                help_text='Автоматично оновлювати дані лістингів (ціни, назви, атрибути) з DigiKey за розкладом',
            ),
        ),
        migrations.AddField(
            model_name='digikeyconfig',
            name='pull_interval_hours',
            field=models.PositiveSmallIntegerField(
                default=24,
                verbose_name='Інтервал авто-стягування (годин)',
                help_text='Рекомендовано: 12–48 год. Запускати cron: python manage.py pull_dk_listings',
            ),
        ),
        migrations.AddField(
            model_name='digikeyconfig',
            name='last_pulled_at',
            field=models.DateTimeField(
                null=True,
                blank=True,
                verbose_name='Останнє авто-стягування',
            ),
        ),
    ]
