from django.db import migrations, models
import config.models

_FIELD = dict(blank=True, default='', max_length=20, validators=[config.models._validate_hex])


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0012_themesettings'),
    ]

    operations = [
        migrations.AddField(
            model_name='themesettings',
            name='header_bg',
            field=models.CharField(verbose_name='Верхня панель фон (--header-bg)', **_FIELD),
        ),
        migrations.AddField(
            model_name='themesettings',
            name='header_color',
            field=models.CharField(verbose_name='Верхня панель текст (--header-color)', **_FIELD),
        ),
        migrations.AddField(
            model_name='themesettings',
            name='sb_bg',
            field=models.CharField(verbose_name='Сайдбар фон (--sb-bg)', **_FIELD),
        ),
        migrations.AddField(
            model_name='themesettings',
            name='border_color',
            field=models.CharField(verbose_name='Бордюри/лінії (--border-strong)', **_FIELD),
        ),
        migrations.AddField(
            model_name='themesettings',
            name='btn_primary',
            field=models.CharField(verbose_name='Кнопка основна / Save (--default-button-bg)', **_FIELD),
        ),
        migrations.AddField(
            model_name='themesettings',
            name='btn_danger',
            field=models.CharField(verbose_name='Кнопка видалення (--delete-button-bg)', **_FIELD),
        ),
    ]
