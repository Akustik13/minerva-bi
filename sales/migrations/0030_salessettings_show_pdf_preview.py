from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0029_salessettings_media_priority'),
    ]

    operations = [
        migrations.AddField(
            model_name='salessettings',
            name='show_pdf_preview',
            field=models.BooleanField(
                default=True,
                help_text='У списку замовлень при наведенні на значок 📄 показує маленький попередній перегляд PDF.',
                verbose_name='Відображення PDF при наведенні',
            ),
        ),
    ]
