from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('autoimport', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='autoimportprofile',
            name='column_map',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='Словник {поле_системи: назва_колонки_у_файлі}. Заповнюється автоматично через кнопку «Виявити колонки».',
                verbose_name='Маппінг колонок',
            ),
        ),
    ]
