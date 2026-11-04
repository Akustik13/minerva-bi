"""Claude Haiku 5.5 — модель за замовчуванням для завдань агенту й доручень «🧩 Завдання агенту». Доручення-відповіді
(їм модель завдання ще не потрібна) теж отримують Haiku — щоб після перемикання на «🧩» нічого не міняти."""
from django.db import migrations, models

CHOICES = [('claude-opus-5-5', 'Claude Opus 5.5 — найточніше · $4 / $20 за 1 млн токенів'),
           ('claude-sonnet-5-5', 'Claude Sonnet 5.5 — швидко й якісно · $2 / $10'),
           ('claude-haiku-5-5', 'Claude Haiku 5.5 — найдешевше · $0.10 / $0.50')]


def reply_rules_haiku(apps, schema_editor):
    AgentRule = apps.get_model('agent', 'AgentRule')
    AgentRule.objects.filter(kind='mail_reply', task_model='claude-sonnet-5-5').update(task_model='claude-haiku-5-5')


class Migration(migrations.Migration):

    dependencies = [
        ('agent', '0008_mail_tasks'),
    ]

    operations = [
        migrations.AlterField(
            model_name='agentrule',
            name='task_model',
            field=models.CharField(choices=CHOICES, default='claude-haiku-5-5', max_length=40,
                                   verbose_name='Модель AI завдання'),
        ),
        migrations.AlterField(
            model_name='agenttask',
            name='model',
            field=models.CharField(choices=CHOICES, default='claude-haiku-5-5', max_length=40, verbose_name='Модель AI'),
        ),
        migrations.RunPython(reply_rules_haiku, migrations.RunPython.noop),
    ]
