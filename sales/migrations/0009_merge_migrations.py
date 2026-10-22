"""
Ціни й валюта в SalesOrder і SalesOrderLine.

SalesOrderLine.unit_price і total_price уже створила 0006, тому для бази ця міграція лише додає нові стовпці
і змінює точність unit_price (2 → 4 знаки). Опис моделей (state) — як і раніше: робочі бази цю міграцію
вже пройшли, а нова база з нуля тепер не падає на «duplicate column unit_price».
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('sales', '0008_salesorder_phone_salesorder_status_and_more'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
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
            ],
            database_operations=[
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
                migrations.AlterField(
                    model_name='salesorderline',
                    name='unit_price',
                    field=models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True),
                ),
                migrations.AddField(
                    model_name='salesorderline',
                    name='currency',
                    field=models.CharField(blank=True, default='EUR', max_length=8),
                ),
            ],
        ),
    ]
