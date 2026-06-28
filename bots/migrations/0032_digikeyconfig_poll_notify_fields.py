from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0031_digikeyconfig_poll_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='digikeyconfig',
            name='poll_notify_telegram',
            field=models.BooleanField(
                default=False,
                verbose_name='Telegram — при затвердженні',
                help_text='Надіслати Telegram-повідомлення коли DigiKey затвердить лістинг (staged → published)',
            ),
        ),
        migrations.AddField(
            model_name='digikeyconfig',
            name='poll_notify_email',
            field=models.BooleanField(
                default=False,
                verbose_name='Email — при затвердженні',
                help_text='Надіслати email-повідомлення коли DigiKey затвердить лістинг (staged → published)',
            ),
        ),
        migrations.AddField(
            model_name='digikeyconfig',
            name='poll_notify_email_to',
            field=models.CharField(
                max_length=500,
                blank=True,
                default='',
                verbose_name='Email одержувачів (через кому)',
                help_text='Порожньо = використати адресу з загальних налаштувань сповіщень',
            ),
        ),
    ]
