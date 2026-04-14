from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0025_ups_log_max_entries'),
    ]

    operations = [
        migrations.AddField(
            model_name='shippingsettings',
            name='api_log_max_entries',
            field=models.PositiveSmallIntegerField(
                default=20,
                help_text='Скільки останніх записів зберігати для DHL, FedEx, Jumingo, DigiKey (1–500).',
                verbose_name='API лог — макс. записів (DHL/FedEx/Jumingo/DigiKey)',
            ),
        ),
    ]
