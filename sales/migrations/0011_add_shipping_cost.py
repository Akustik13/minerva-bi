from django.db import migrations, models


class Migration(migrations.Migration):
    """Додає shipping_cost до SalesOrder."""
    
    dependencies = [
        ('sales', '0010_add_price_fields_force'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE sales_salesorder 
                ADD COLUMN IF NOT EXISTS shipping_cost NUMERIC(10,2) DEFAULT 0;
            """,
            reverse_sql="-- No reverse"
        ),
    ]
