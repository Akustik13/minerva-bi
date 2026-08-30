from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jlcpcb', '0003_jlcconfig_app_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='jlcconfig',
            name='telegram_personal_chat_id',
            field=models.CharField(
                blank=True,
                default='',
                help_text=(
                    'Особистий Chat ID для JLCPCB сповіщень (наприклад: 123456789). '
                    'Отримайте у @userinfobot в Telegram. '
                    'Якщо порожньо — використовується Chat ID з загальних налаштувань (канал).'
                ),
                max_length=50,
                verbose_name='Telegram Personal Chat ID',
            ),
        ),
    ]
