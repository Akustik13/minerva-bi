from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0015_bottask'),
    ]

    operations = [
        migrations.AddField(
            model_name='digikeylisting',
            name='dk_quantity_available',
            field=models.IntegerField(
                blank=True, null=True,
                verbose_name='Залишок на DigiKey',
                help_text='Кількість, яку бачить покупець на DigiKey (оновлюється при імпорті)',
            ),
        ),
    ]
