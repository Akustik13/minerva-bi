from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0002_systemsettings_units'),
    ]

    operations = [
        migrations.AddField(
            model_name='systemsettings',
            name='country_code_format',
            field=models.CharField(
                verbose_name='Формат коду країни',
                max_length=5,
                choices=[('iso2', 'ISO-2 (2 букви: DE, UA, PL)'), ('iso3', 'ISO-3 (3 букви: DEU, UKR, POL)')],
                default='iso2',
                help_text='ISO-2: DE, UA, PL — ISO-3: DEU, UKR, POL',
            ),
        ),
    ]
