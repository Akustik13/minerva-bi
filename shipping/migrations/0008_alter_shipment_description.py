from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0007_jumingo_tariff_fields'),
    ]

    operations = [
        migrations.AlterField(
            model_name='shipment',
            name='description',
            field=models.CharField(
                blank=True,
                default='',
                help_text="Для митниці: напр. 'Electronic components'",
                max_length=300,
                verbose_name='Опис вмісту (макс. 35 символів)',
            ),
        ),
    ]
