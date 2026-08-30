from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jlcpcb', '0004_jlcconfig_telegram_personal_chat_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='jlcconfig',
            name='sync_days_back',
            field=models.PositiveSmallIntegerField(
                default=30,
                help_text='Скільки днів назад шукати нові замовлення при синхронізації. '
                          'Рекомендовано: 30–90 днів. Більше = повільніше.',
                verbose_name='Період пошуку нових замовлень (днів)',
            ),
        ),
    ]
