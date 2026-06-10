from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0032_alter_notificationsettings_customer_notify_body_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='notificationsettings',
            name='dk_auto_confirm_email',
            field=models.BooleanField(
                default=False,
                verbose_name='Email: DigiKey авто-підтвердження',
                help_text='Надсилати email коли DigiKey замовлення підтверджено автоматично. '
                          'Окремо від «Нове замовлення» — щоб уникнути дублювання.',
            ),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='dk_auto_confirm_telegram',
            field=models.BooleanField(
                default=True,
                verbose_name='Telegram: DigiKey авто-підтвердження',
                help_text='Надсилати Telegram коли DigiKey замовлення підтверджено автоматично. '
                          'Окремо від «Нове замовлення» — щоб уникнути дублювання.',
            ),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='notify_include_crm_count',
            field=models.BooleanField(
                default=True,
                verbose_name='📊 Кількість замовлень клієнта (CRM)',
                help_text='Показувати скільки всього замовлень від цього клієнта в CRM.',
            ),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='notify_include_stock_info',
            field=models.BooleanField(
                default=True,
                verbose_name='🏪 Стан складу',
                help_text='Показувати наявність товарів на складі (✅ є / ❌ не вистачає).',
            ),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='notify_include_deadline',
            field=models.BooleanField(
                default=True,
                verbose_name='📦 Дедлайн відправки',
                help_text='Показувати дедлайн відправки та кількість днів, що залишились.',
            ),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='notify_include_datasheet',
            field=models.BooleanField(
                default=True,
                verbose_name='📄 Посилання на Datasheet',
                help_text='Додавати посилання на технічну документацію для кожного товару.',
            ),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='notify_include_total',
            field=models.BooleanField(
                default=True,
                verbose_name='💰 Сума замовлення',
                help_text='Показувати загальну суму замовлення.',
            ),
        ),
    ]
