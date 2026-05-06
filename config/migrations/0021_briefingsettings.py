from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0020_notificationsettings_email_signature_template'),
    ]

    operations = [
        migrations.CreateModel(
            name='BriefingSettings',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('enabled', models.BooleanField(default=True, verbose_name='Увімкнено', help_text='Брифінг відправляється в Telegram кожному юзеру з Telegram ID.')),
                ('send_time', models.TimeField(default='08:00', verbose_name='Час відправки', help_text='Час по серверному поясу (Europe/Berlin). Налаштуйте cron на цей час.')),
                ('include_orders', models.BooleanField(default=True, verbose_name='📦 Замовлення місяця')),
                ('include_revenue', models.BooleanField(default=True, verbose_name='💰 Виручка місяця')),
                ('include_overdue', models.BooleanField(default=True, verbose_name='⏰ Прострочені дедлайни')),
                ('include_reminders', models.BooleanField(default=True, verbose_name='🔔 Нагадування на сьогодні')),
                ('include_stock_alerts', models.BooleanField(default=False, verbose_name='🔥 Критичний залишок')),
                ('include_new_emails', models.BooleanField(default=False, verbose_name='📬 Нові листи від клієнтів')),
                ('custom_instructions', models.TextField(blank=True, verbose_name='Додаткові інструкції для AI', help_text='AI може проявляти ініціативу і включати важливі речі незалежно від цих налаштувань. Тут можна вказати акценти: мова, стиль, додаткові метрики тощо.')),
            ],
            options={
                'verbose_name': 'Налаштування брифінгу',
                'verbose_name_plural': 'Налаштування брифінгу',
            },
        ),
    ]
