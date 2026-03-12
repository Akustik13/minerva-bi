from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0003_systemsettings_country_code_format'),
    ]

    operations = [
        migrations.AddField(
            model_name='systemsettings',
            name='default_vat_rate',
            field=models.DecimalField(
                decimal_places=2,
                default=19,
                help_text='Стандартна ставка ПДВ у % (наприклад 19, 7, 20). '
                          'Підставляється автоматично при створенні нового рахунку-фактури.',
                max_digits=5,
                verbose_name='ПДВ / MwSt за замовчуванням (%)',
            ),
        ),
    ]
