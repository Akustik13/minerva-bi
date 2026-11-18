from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('meetings', '0007_agenda_translation'),
    ]

    operations = [
        migrations.AddField(
            model_name='meetingtranscript',
            name='brief',
            field=models.JSONField(blank=True, default=dict, help_text='📝 Підсумок наради й звірка з агендою (meetings/ai_brief.py): {about, sections, agenda {pk пункту: {status, result, lines, st}}, model, lang, at, cost}'),
        ),
    ]
