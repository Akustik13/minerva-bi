from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0016_alter_shippingsettings_last_tracking_run_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipment',
            name='carrier_status_label',
            field=models.CharField(
                blank=True, default='',
                help_text="Текстовий статус від API перевізника (напр. «Unterwegs»)",
                max_length=200, verbose_name='Статус перевізника',
            ),
        ),
        migrations.AddField(
            model_name='shipment',
            name='carrier_delayed',
            field=models.BooleanField(
                default=False,
                help_text='Перевізник підтвердив затримку посилки',
                verbose_name='Затримка доставки',
            ),
        ),
        migrations.AddField(
            model_name='shipment',
            name='eta_from',
            field=models.DateField(
                blank=True, null=True,
                help_text='Початок вікна очікуваної доставки',
                verbose_name='Очікувана доставка від',
            ),
        ),
        migrations.AddField(
            model_name='shipment',
            name='eta_to',
            field=models.DateField(
                blank=True, null=True,
                help_text='Кінець вікна очікуваної доставки',
                verbose_name='Очікувана доставка до',
            ),
        ),
    ]
