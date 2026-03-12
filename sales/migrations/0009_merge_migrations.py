"""
ІНСТРУКЦІЯ:
1. Видаліть файл:  sales\migrations\0005_add_price_fields.py
2. Скопіюйте цей файл як: sales\migrations\0009_merge_migrations.py
3. python manage.py migrate
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Додає price поля до SalesOrder і SalesOrderLine.
    Залежить тільки від 0008 (status і phone вже там є).
    """
    dependencies = [
        ('sales', '0008_salesorder_phone_salesorder_status_and_more'),
    ]

    operations = [
        # SalesOrder — загальна сума + валюта
        migrations.AddField(
            model_name='salesorder',
            name='total_price',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='salesorder',
            name='currency',
            field=models.CharField(blank=True, default='EUR', max_length=8),
        ),
        # SalesOrderLine — ціна одиниці + сума рядка + валюта
        migrations.AddField(
            model_name='salesorderline',
            name='unit_price',
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='salesorderline',
            name='total_price',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='salesorderline',
            name='currency',
            field=models.CharField(blank=True, default='EUR', max_length=8),
        ),
        # order_date/shipped_at вже DateField в 0008 - skip
    ]
