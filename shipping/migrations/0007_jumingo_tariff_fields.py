from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0006_jumingo_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipment',
            name='selected_tariff_id',
            field=models.CharField(
                blank=True, default='',
                max_length=50,
                verbose_name='ID тарифу Jumingo',
            ),
        ),
        migrations.AddField(
            model_name='shipment',
            name='jumingo_order_number',
            field=models.CharField(
                blank=True, default='',
                max_length=50,
                verbose_name='Номер замовлення Jumingo',
            ),
        ),
    ]
