from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0050_shipment_ups_duties_billing'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipment',
            name='ups_payer_addresses',
            field=models.JSONField(
                blank=True, default=dict,
                help_text='{"billing": {...}, "duties": {...}} — name, company, street, city, state',
                verbose_name='UPS: адреси третіх сторін',
            ),
        ),
    ]
