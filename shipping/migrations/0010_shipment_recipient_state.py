from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0009_alter_shipment_recipient_name_verbose'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipment',
            name='recipient_state',
            field=models.CharField(
                blank=True, default='',
                help_text='Тільки для США/Канади: CA, NY, TX, FL...',
                max_length=2, verbose_name='Штат / провінція (ISO 2)',
            ),
        ),
    ]
