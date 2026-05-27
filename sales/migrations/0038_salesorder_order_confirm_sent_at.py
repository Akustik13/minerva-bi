from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0037_salesorder_ship_notify_sent_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='salesorder',
            name='order_confirm_sent_at',
            field=models.DateTimeField(
                blank=True, null=True,
                verbose_name='📥 Підтвердження замовлення надіслано',
                help_text='Дата/час надсилання email-підтвердження отримання замовлення',
            ),
        ),
    ]
