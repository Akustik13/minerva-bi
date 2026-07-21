from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0033_digikeyconfig_listing_set_null'),
    ]

    operations = [
        migrations.AddField(
            model_name='digikeyconfig',
            name='msg_notify_telegram',
            field=models.BooleanField(
                default=False,
                verbose_name='Telegram — при новому повідомленні',
                help_text='Надіслати Telegram-сповіщення коли покупець надсилає нове повідомлення',
            ),
        ),
        migrations.AddField(
            model_name='digikeyconfig',
            name='msg_notify_email',
            field=models.BooleanField(
                default=False,
                verbose_name='Email — при новому повідомленні',
                help_text='Надіслати email-сповіщення коли покупець надсилає нове повідомлення',
            ),
        ),
        migrations.AddField(
            model_name='digikeyconfig',
            name='msg_notify_email_to',
            field=models.CharField(
                max_length=500,
                blank=True,
                default='',
                verbose_name='Email одержувачів (через кому)',
                help_text='Порожньо = використати адресу з загальних налаштувань сповіщень',
            ),
        ),
    ]
