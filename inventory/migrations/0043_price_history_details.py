from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0042_ukrainian_labels_low_stock_help'),
    ]

    operations = [
        migrations.AddField(
            model_name='productpricehistory',
            name='details',
            field=models.JSONField(blank=True, default=dict, verbose_name='Подробиці'),
        ),
    ]
