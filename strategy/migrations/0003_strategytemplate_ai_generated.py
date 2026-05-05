from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('strategy', '0002_aisettings'),
    ]

    operations = [
        migrations.AddField(
            model_name='strategytemplate',
            name='is_ai_generated',
            field=models.BooleanField(
                default=False,
                verbose_name='AI-генерована',
                help_text='Шаблон згенерований AI адаптивно для конкретного клієнта',
            ),
        ),
    ]
