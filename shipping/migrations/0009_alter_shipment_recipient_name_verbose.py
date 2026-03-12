from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0008_alter_shipment_description'),
    ]

    operations = [
        migrations.AlterField(
            model_name='shipment',
            name='recipient_name',
            field=models.CharField(
                blank=True, default='', max_length=255,
                verbose_name='Контактна особа',
            ),
        ),
    ]
