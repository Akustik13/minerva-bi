from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0006_notificationsettings'),
    ]

    operations = [
        migrations.AddField(
            model_name='notificationsettings',
            name='telegram_enabled',
            field=models.BooleanField(
                default=False,
                verbose_name='Надсилати Telegram-сповіщення',
                help_text='Увімкніть після налаштування Bot Token та Chat ID нижче.',
            ),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='telegram_bot_token',
            field=models.CharField(
                blank=True, max_length=200, verbose_name='Bot Token',
                help_text='Отримати у @BotFather: /newbot → скопіювати токен вигляду 123456:ABC-...',
            ),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='telegram_chat_id',
            field=models.CharField(
                blank=True, max_length=100, verbose_name='Chat ID',
                help_text=(
                    'ID чату, групи або каналу. '
                    'Для каналу: @mychannel або -100xxxxxxxxxx. '
                    'Для особистого чату: числовий ID (дізнатись через @userinfobot).'
                ),
            ),
        ),
    ]
