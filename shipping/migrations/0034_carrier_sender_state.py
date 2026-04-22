from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0033_delivered_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='carrier',
            name='sender_state',
            field=models.CharField(
                blank=True,
                default='',
                help_text="Для США/Канади: дволітерний код (CA, NY, TX). Обов'язково для UPS з US-адресою.",
                max_length=100,
                verbose_name='Штат / провінція відправника',
            ),
        ),
    ]
