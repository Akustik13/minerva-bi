from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('email_assistant', '0009_scheduledmail'),
    ]

    operations = [
        migrations.AddField(
            model_name='emailsettings',
            name='auto_reply_enabled',
            field=models.BooleanField(
                default=False, verbose_name='Автовідповідь',
                help_text='Minerva AI автоматично генерує відповідь на вхідні листи'),
        ),
        migrations.AddField(
            model_name='emailsettings',
            name='auto_reply_mode',
            field=models.CharField(
                max_length=10, default='draft',
                choices=[('draft', 'Зберегти як чернетку'), ('send', 'Надіслати одразу')],
                verbose_name='Режим автовідповіді'),
        ),
        migrations.AddField(
            model_name='emailsettings',
            name='auto_reply_prompt',
            field=models.TextField(
                blank=True, verbose_name='Інструкція для AI (автовідповідь)',
                help_text='Порожньо — стандартна інструкція генерації відповіді'),
        ),
        migrations.AddField(
            model_name='emailsettings',
            name='order_trigger_enabled',
            field=models.BooleanField(
                default=False, verbose_name='Нове замовлення → чернетка листа',
                help_text='При кожному новому замовленні AI генерує чернетку листа клієнту'),
        ),
    ]
