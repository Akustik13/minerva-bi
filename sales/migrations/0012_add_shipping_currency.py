# sales/migrations/0012_add_shipping_currency.py
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('sales', '0011_add_shipping_cost'),  # або остання міграція
    ]

    operations = [
        migrations.AddField(
            model_name='salesorder',
            name='shipping_currency',
            field=models.CharField(
                max_length=8, 
                blank=True, 
                default='EUR',
                verbose_name='Валюта доставки'
            ),
        ),
        # Змінюємо default currency на USD
        migrations.AlterField(
            model_name='salesorder',
            name='currency',
            field=models.CharField(
                max_length=8,
                blank=True,
                default='USD',  # Було EUR
                verbose_name='Валюта'
            ),
        ),
        migrations.AlterField(
            model_name='salesorderline',
            name='currency',
            field=models.CharField(
                max_length=8,
                blank=True,
                default='USD',  # Було EUR
                verbose_name='Валюта'
            ),
        ),
    ]
