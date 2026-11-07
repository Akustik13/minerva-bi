from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0058_shipment_export_mrn'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipment',
            name='package_tracking',
            field=models.JSONField(blank=True, default=list, help_text='Кілька коробок в одному відправленні UPS: у кожної свій номер (на її етикетці); перший зазвичай збігається з трек-номером відправлення.', verbose_name='Трек-номери коробок'),
        ),
    ]
