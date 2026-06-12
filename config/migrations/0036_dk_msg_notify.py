from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0035_dk_unconfirmed_alert'),
    ]

    operations = [
        migrations.AddField(
            model_name='notificationsettings',
            name='dk_msg_notify_telegram',
            field=models.BooleanField(default=True, help_text='Надсилати Telegram при новому повідомленні від покупця DigiKey Marketplace.', verbose_name='Telegram: нове повідомлення DigiKey'),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='dk_msg_notify_email',
            field=models.BooleanField(default=False, help_text='Надсилати Email при новому повідомленні від покупця DigiKey Marketplace.', verbose_name='Email: нове повідомлення DigiKey'),
        ),
        migrations.AddField(
            model_name='notificationsettings',
            name='dk_msg_notify_email_to',
            field=models.CharField(blank=True, help_text='Email-адреси через кому. Якщо порожньо — використовується основний список отримувачів вище.', max_length=500, verbose_name='Email-отримувачі (DigiKey повідомлення)'),
        ),
    ]
