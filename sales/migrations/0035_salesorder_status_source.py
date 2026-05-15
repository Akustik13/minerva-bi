from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0034_backfill_ship_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='salesorder',
            name='status_source',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Яке API або дія останньо змінила статус замовлення (заповнюється автоматично)',
                max_length=100,
                verbose_name='Джерело статусу',
            ),
        ),
    ]
