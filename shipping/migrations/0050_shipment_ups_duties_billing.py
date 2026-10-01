from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0049_shipment_custom_invoice'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipment',
            name='ups_duties_billing',
            field=models.CharField(
                choices=[
                    ('receiver', 'Отримувач — стандарт (DAP)'),
                    ('shipper', 'Відправник (DDP) — наш UPS-акаунт'),
                    ('third_party', 'Третя сторона — інший UPS-акаунт'),
                ],
                default='receiver', max_length=15,
                verbose_name='UPS: платник мита/податків',
            ),
        ),
        migrations.AddField(
            model_name='shipment',
            name='ups_duties_account',
            field=models.CharField(blank=True, max_length=50, verbose_name='UPS: акаунт платника мита'),
        ),
        migrations.AddField(
            model_name='shipment',
            name='ups_duties_postal',
            field=models.CharField(blank=True, max_length=20, verbose_name='UPS: індекс платника мита'),
        ),
        migrations.AddField(
            model_name='shipment',
            name='ups_duties_country',
            field=models.CharField(blank=True, max_length=2, verbose_name='UPS: країна платника мита'),
        ),
    ]
