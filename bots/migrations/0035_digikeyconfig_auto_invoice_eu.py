from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0034_digikeyconfig_msg_notify_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='digikeyconfig',
            name='auto_invoice_eu',
            field=models.BooleanField(
                default=False,
                help_text='Якщо увімкнено — при підтвердженні відправки до ЄС система автоматично генерує інвойс (якщо ще не згенеровано). Якщо вимкнено — лише ручний режим.',
                verbose_name='Авто-генерація інвойсу для ЄС',
            ),
        ),
    ]
