from django.db import migrations, models
import config.models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0013_themesettings_extra_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='themesettings',
            name='sb_border',
            field=models.CharField(
                blank=True,
                default='',
                max_length=20,
                validators=[config.models._validate_hex],
                verbose_name='Сайдбар розділювачі (--sb-border)',
            ),
        ),
        migrations.AddField(
            model_name='themesettings',
            name='sb_border_accent',
            field=models.CharField(
                blank=True,
                default='',
                max_length=20,
                validators=[config.models._validate_hex],
                verbose_name='Сайдбар акцент-лінія (--sb-border-accent)',
            ),
        ),
    ]
