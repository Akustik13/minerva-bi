from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0024_error_log_fields'),
        ('inventory', '0027_incoming_shipment'),
    ]

    operations = [
        migrations.AlterField(
            model_name='digikeylisting',
            name='product',
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='dk_listing',
                to='inventory.product',
                verbose_name='Товар',
            ),
        ),
    ]
