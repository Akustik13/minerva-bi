from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0032_cable_rfq_template_seed'),
    ]

    operations = [
        migrations.AddField(
            model_name='rfqemailtemplate',
            name='diagram_in_body',
            field=models.BooleanField(
                default=True,
                help_text="Якщо увімкнено — зображення буде вбудоване в HTML-листі. "
                          "Якщо вимкнено — показується тільки як прев'ю у панелі кошика.",
                verbose_name='Вставити схему в тіло листа',
            ),
        ),
    ]
