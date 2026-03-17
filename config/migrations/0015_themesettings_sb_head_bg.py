from django.db import migrations, models
import config.models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0014_themesettings_sb_border'),
    ]

    operations = [
        migrations.AddField(
            model_name='themesettings',
            name='sb_head_bg',
            field=models.CharField(
                blank=True,
                default='',
                max_length=20,
                validators=[config.models._validate_hex],
                verbose_name='Сайдбар кнопки груп (--sb-head-bg)',
            ),
        ),
    ]
