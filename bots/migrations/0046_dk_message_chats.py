from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0045_digikey_vat_attach'),
    ]

    operations = [
        migrations.AddField(
            model_name='digikeyconfig',
            name='msg_notify_tg_chats',
            field=models.CharField(blank=True, default='', help_text='Chat ID через кому: спільна група / канал (@назва) або особистий чат співробітника з ботом. Порожньо = основний чат «Налаштування → Сповіщення».', max_length=500, verbose_name='Telegram-чати'),
        ),
        migrations.AlterField(
            model_name='digikeyconfig',
            name='msg_notify_telegram',
            field=models.BooleanField(default=True, help_text='Нове повідомлення покупця чи оператора DigiKey — у Telegram (бот — «Налаштування → Сповіщення», куди — «Telegram-чати» нижче)', verbose_name='Telegram — при новому повідомленні'),
        ),
        migrations.AlterField(
            model_name='digikeyconfig',
            name='msg_notify_email_to',
            field=models.CharField(blank=True, default='', help_text='Порожньо = «Кому (email)» загальних налаштувань сповіщень', max_length=500, verbose_name='Email одержувачів (через кому)'),
        ),
    ]
