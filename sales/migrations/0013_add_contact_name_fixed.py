# sales/migrations/0013_add_contact_name_fixed.py
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('sales', '0012_add_shipping_currency'),
    ]

    operations = [
        # Додаємо contact_name в SalesOrder
        migrations.AddField(
            model_name='salesorder',
            name='contact_name',
            field=models.CharField(
                max_length=255,
                blank=True,
                default='',
                verbose_name='Контактна особа'
            ),
        ),
        
        # currency в SalesOrderLine вже є! Просто змінюємо default
        migrations.AlterField(
            model_name='salesorderline',
            name='currency',
            field=models.CharField(
                max_length=8,
                blank=True,
                default='USD',
                verbose_name='Валюта'
            ),
        ),
        
        # Змінюємо default currency на USD в SalesOrder
        migrations.AlterField(
            model_name='salesorder',
            name='currency',
            field=models.CharField(
                max_length=8,
                blank=True,
                default='USD',
                verbose_name='Валюта продажу'
            ),
        ),
        
        # shipping_currency теж вже є з міграції 0012
        migrations.AlterField(
            model_name='salesorder',
            name='shipping_currency',
            field=models.CharField(
                max_length=8,
                blank=True,
                default='EUR',
                verbose_name='Валюта доставки'
            ),
        ),
    ]
