from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('autoimport', '0002_autoimportprofile_column_map'),
    ]

    operations = [
        migrations.AddField(
            model_name='autoimportprofile',
            name='sheet_name',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Назва вкладки Excel. Порожньо = перший лист. Для CSV не застосовується.',
                max_length=100,
                verbose_name='Вкладка (лист)',
            ),
            preserve_default=False,
        ),
    ]
