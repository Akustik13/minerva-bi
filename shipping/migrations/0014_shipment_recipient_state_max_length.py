from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0013_carrier_track_api_key'),
    ]

    operations = [
        migrations.AlterField(
            model_name='shipment',
            name='recipient_state',
            field=models.CharField(
                blank=True,
                default='',
                help_text='США/Канада: дволітерний код (CA, NY, TX). Інші країни: повна назва регіону.',
                max_length=100,
                verbose_name='Штат / провінція',
            ),
        ),
    ]
