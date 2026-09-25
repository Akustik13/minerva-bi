# Generated migration

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0047_ups_billing_and_export_reasons'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipmentpackage',
            name='items_distribution',
            field=models.JSONField(
                blank=True,
                help_text='JSON: які товари та кількість в цій коробці',
                null=True,
                verbose_name='Розподіл товарів в коробці'
            ),
        ),
    ]
