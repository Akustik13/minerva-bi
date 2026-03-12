from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0005_alter_packagingmaterial_options'),
    ]

    operations = [
        migrations.AddField(
            model_name='carrier',
            name='connection_uuid',
            field=models.CharField(
                blank=True, default='',
                help_text='Jumingo → Integrations → UUID вашої інтеграції',
                max_length=100,
                verbose_name='Connection UUID (Jumingo)',
            ),
        ),
        migrations.AddField(
            model_name='shipment',
            name='export_reason',
            field=models.CharField(
                choices=[
                    ('Commercial', 'Commercial — продаж'),
                    ('Gift', 'Gift — подарунок'),
                    ('Personal', 'Personal — особисте'),
                    ('Return', 'Return — повернення'),
                    ('Claim', 'Claim — рекламація'),
                ],
                default='Commercial',
                max_length=20,
                verbose_name='Причина експорту',
                help_text='Для митної декларації CN23',
            ),
        ),
    ]
