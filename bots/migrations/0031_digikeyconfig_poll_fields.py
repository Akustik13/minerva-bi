from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0030_digikeyconfig_create_product_if_missing'),
    ]

    operations = [
        migrations.AddField(
            model_name='digikeyconfig',
            name='poll_enabled',
            field=models.BooleanField(
                default=False,
                verbose_name='Авто-перевірка статусу (staged → published)',
                help_text="Автоматично перевіряти чи затвердив DigiKey лістинги у статусі 'Очікує затвердження'",
            ),
        ),
        migrations.AddField(
            model_name='digikeyconfig',
            name='poll_interval_minutes',
            field=models.PositiveSmallIntegerField(
                default=60,
                verbose_name='Інтервал перевірки (хвилин)',
                help_text='Рекомендовано: 30–120 хв. Cron: python manage.py poll_dk_status',
            ),
        ),
        migrations.AddField(
            model_name='digikeyconfig',
            name='last_polled_at',
            field=models.DateTimeField(
                null=True,
                blank=True,
                verbose_name='Остання авто-перевірка статусу',
            ),
        ),
    ]
