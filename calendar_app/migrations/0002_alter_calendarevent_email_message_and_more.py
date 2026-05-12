import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('calendar_app', '0001_initial'),
        ('email_assistant', '0009_scheduledmail'),
    ]

    operations = [
        migrations.AlterField(
            model_name='calendarevent',
            name='email_message',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='calendar_events',
                to='email_assistant.emailmessage',
                verbose_name='Лист',
                help_text='Лист з якого витягнуто дедлайн',
            ),
        ),
    ]
