from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0019_digikeyconfig_pull_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='digikeylisting',
            name='dk_price_min',
            field=models.FloatField(
                blank=True,
                db_index=True,
                help_text='Автоматично: ціна за 1 шт. з dk_prices (для сортування)',
                null=True,
                verbose_name='Ціна (1 шт.)',
            ),
        ),
    ]
