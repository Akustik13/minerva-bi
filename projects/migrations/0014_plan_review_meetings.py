# 📅 Звірка плану з кількома нарадами: PlanReview.meetings (наявні звірки — зі своєю нарадою).

from django.db import migrations, models


def copy_meeting(apps, schema_editor):
    PlanReview = apps.get_model("projects", "PlanReview")
    for r in PlanReview.objects.exclude(meeting=None).only("pk", "meeting_id"):
        r.meetings.add(r.meeting_id)


class Migration(migrations.Migration):

    dependencies = [
        ('meetings', '0008_transcript_brief'),
        ('projects', '0013_plan_review_history'),
    ]

    operations = [
        migrations.AlterField(
            model_name='planreview',
            name='meeting',
            field=models.ForeignKey(blank=True, help_text='Остання за датою з вибраних нарад', null=True,
                                    on_delete=models.deletion.SET_NULL, related_name='+', to='meetings.meeting',
                                    verbose_name='Нарада'),
        ),
        migrations.AddField(
            model_name='planreview',
            name='meetings',
            field=models.ManyToManyField(blank=True, help_text='Усі наради звірки (кілька — у порядку дат, пізніше рішення важливіше)',
                                         related_name='+', to='meetings.meeting', verbose_name='Наради'),
        ),
        migrations.RunPython(copy_meeting, migrations.RunPython.noop),
    ]
