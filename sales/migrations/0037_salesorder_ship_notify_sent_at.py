from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0036_eu_invoice_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='salesorder',
            name='ship_notify_sent_at',
            field=models.DateTimeField(
                blank=True, null=True,
                verbose_name='📧 Повідомлення клієнту надіслано',
                help_text='Дата/час надсилання email-сповіщення про відправку',
            ),
        ),
    ]
