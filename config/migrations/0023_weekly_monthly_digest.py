from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0022_notificationsettings_imap_last_fetched'),
    ]

    operations = [
        migrations.AddField(
            model_name='notificationsettings',
            name='weekly_digest_enabled',
            field=models.BooleanField(default=False, verbose_name='Тижневий звіт увімкнено'),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='weekly_digest_day',
            field=models.PositiveSmallIntegerField(default=0, verbose_name='День тижня (0=Пн, 1=Вт, …, 6=Нд)'),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='weekly_digest_time',
            field=models.TimeField(default='08:00', verbose_name='Час відправки'),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='weekly_digest_last_sent',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Тижневий звіт: останній'),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='monthly_digest_enabled',
            field=models.BooleanField(default=False, verbose_name='Місячний звіт увімкнено'),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='monthly_digest_day',
            field=models.PositiveSmallIntegerField(default=1, verbose_name='День місяця (1–28)'),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='monthly_digest_time',
            field=models.TimeField(default='08:00', verbose_name='Час відправки'),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='monthly_digest_last_sent',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Місячний звіт: останній'),
        ),
    ]
